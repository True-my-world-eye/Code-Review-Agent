"""CLI 交互层 —— typer 命令定义 + rich 渲染（对应 Design.md 第 8 节）。

命令一览：
    python main.py review [路径] [-a 补充要求]   一次性审查（时间线 + 报告表格）
    python main.py chat [-s 会话ID]              交互式多轮对话（REPL）
    python main.py config show|set|test          查看 / 修改 / 自检配置
    python main.py version                       版本号

渲染约定：
- 时间线事件实时打印（Agent 每步动作可见）；
- 最终报告按严重级别排序上色（严重=红 / 警告=黄 / 建议=蓝）；
- JSON 解析失败时降级展示 Agent 原文，绝不吞输出。
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from app import __version__
from app.config import ConfigError, load_settings, save_settings
from app.core.agent import Agent, AgentEvent, AgentResult
from app.core.memory import SessionMemory, list_sessions
from app.core.prompt import parse_review_report
from app.tools.builtin import build_default_registry
from app.tools.registry import ToolContext

# 根应用：聚合全部子命令
app = typer.Typer(
    name="code-review-agent",
    help="Code Review Agent —— 基于 LLM 的代码审查助手",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(help="查看 / 修改配置", no_args_is_help=True)
app.add_typer(config_app, name="config")

console = Console()

# 严重级别 → (rich 样式, 展示名, 排序权重)
_SEVERITY_META: dict[str, tuple[str, str, int]] = {
    "error": ("red", "严重", 0),
    "warning": ("yellow", "警告", 1),
    "suggestion": ("blue", "建议", 2),
}


# ================================================================ 通用渲染
def _print_event(event: AgentEvent) -> None:
    """把一条 Agent 时间线事件渲染为彩色单行。"""
    kind = event.kind
    if kind == "llm":
        console.print(f"[dim]◉ {event.text}[/dim]")
    elif kind == "tool":
        console.print(f"[cyan]⚙ {event.text}[/cyan]")
    elif kind == "tool_result":
        style = "green" if event.data.get("ok") else "red"
        console.print(f"[{style}]{event.text}[/{style}]")
    elif kind == "retry":
        console.print(f"[yellow]⚠ {event.text}[/yellow]")
    elif kind == "info":
        console.print(f"[blue]ℹ {event.text}[/blue]")
    elif kind == "error":
        console.print(f"[red]✗ {event.text}[/red]")
    # final 事件由报告渲染统一处理，避免重复输出


def _render_report(content: str, *, title: str = "审查报告") -> None:
    """渲染最终文本：可解析为 JSON → 摘要 + 问题表格；否则降级展示原文。"""
    report = parse_review_report(content)
    if not report["parsed"]:
        # 降级：模型没按格式输出，原样展示（绝不吞掉内容）
        console.print(Panel(content, title="Agent 输出", border_style="yellow"))
        return

    issues = report["issues"]
    if not issues:
        console.print(f"[green]✓ {report['summary'] or '未发现需要报告的问题'}[/green]")
        return

    console.print(
        Panel(f"[bold]{report['summary']}[/bold]", title=title, border_style="blue")
    )
    table = Table(show_lines=False, header_style="bold", expand=False)
    table.add_column("级别", width=6, no_wrap=True)
    table.add_column("位置", ratio=1, no_wrap=True)
    table.add_column("问题", ratio=3)
    table.add_column("建议", ratio=3)

    # 严重 → 警告 → 建议 排序
    issues_sorted = sorted(
        issues, key=lambda x: _SEVERITY_META.get(x["severity"], ("", "", 3))[2]
    )
    counts = {"error": 0, "warning": 0, "suggestion": 0}
    for issue in issues_sorted:
        style, label, _ = _SEVERITY_META.get(issue["severity"], ("white", "?", 3))
        counts[issue["severity"]] = counts.get(issue["severity"], 0) + 1
        location = issue["file"] or "(未知文件)"
        if issue["line"]:
            location = f"{location}:{issue['line']}"
        table.add_row(
            f"[{style}]{label}[/{style}]",
            location,
            issue["message"],
            issue["suggestion"] or "—",
        )
    console.print(table)
    console.print(
        f"[dim]共 {len(issues)} 个问题："
        f"{counts['error']} 严重 / {counts['warning']} 警告 / {counts['suggestion']} 建议[/dim]"
    )


def _print_stats(result: AgentResult) -> None:
    """打印一次运行的统计行。"""
    if not result.ok:
        console.print(f"[red]运行失败：{result.error}[/red]")
        return
    flags = []
    if result.truncated:
        flags.append("[yellow]已达轮数上限（部分报告）[/yellow]")
    extra = (" · " + " · ".join(flags)) if flags else ""
    console.print(
        f"[dim]推理 {result.iterations} 轮 · 工具调用 {result.tool_calls} 次{extra}[/dim]"
    )


def make_confirm_fn():
    """构造 CLI 的修复确认回调：展示替换预览后询问用户。"""

    def _confirm(path: str, detail: dict) -> bool:
        console.print(f"[yellow]◆ {detail.get('description', path)}[/yellow]")
        # 替换预览：旧代码 - / 新代码 +（各最多 6 行）
        for tag, color, code in (
            ("-", "red", detail.get("old_code", "")),
            ("+", "green", detail.get("new_code", "")),
        ):
            for line in str(code).splitlines()[:6]:
                console.print(f"  [{color}]{tag} {line}[/{color}]")
        return Confirm.ask("是否执行此修复？", default=False)

    return _confirm


def _build_agent(settings, root: Path, *, memory: SessionMemory | None = None) -> Agent:
    """按审查根目录组装 Agent（CLI 各命令共用）。"""
    ctx = ToolContext(
        root=root,
        tool_timeout=settings.tool_timeout,
        auto_fix_enabled=settings.auto_fix_enabled,
        confirm_fn=make_confirm_fn(),
    )
    registry = build_default_registry(ctx)
    return Agent(settings, registry, memory=memory, on_event=_print_event)


def _require_configured():
    """加载配置并校验可用性；不满足则红字提示并退出（返回 Settings）。"""
    settings = load_settings()
    if not settings.is_configured:
        console.print(
            "[red]✗ LLM 配置不完整[/red]。请先执行：\n"
            "  python main.py config set --provider deepseek --api-key sk-xxx\n"
            "或设置环境变量 CRA_API_KEY 后重试。"
        )
        raise typer.Exit(code=1)
    return settings


# ================================================================ version
@app.command("version")
def version() -> None:
    """显示版本号。"""
    typer.echo(f"Code Review Agent v{__version__}")


# ================================================================ review
@app.command("review")
def review(
    path: str = typer.Argument(".", help="要审查的文件或目录（默认当前目录）"),
    ask: str | None = typer.Option(None, "--ask", "-a", help="附加审查要求"),
) -> None:
    """一次性审查：实时展示 Agent 时间线，最后输出结构化报告。"""
    settings = _require_configured()

    target = Path(path)
    if not target.exists():
        console.print(f"[red]✗ 路径不存在：{path}[/red]")
        raise typer.Exit(code=1)

    # 安全边界：目录 → 自身为根；文件 → 所在目录为根、目标为相对路径
    target = target.resolve()
    if target.is_dir():
        root, rel = target, "."
    else:
        root, rel = target.parent, target.name

    console.print(
        Panel(
            f"[bold]Code Review Agent[/bold]\n"
            f"目标：{rel}  ·  根目录：{root}\n"
            f"服务商：{settings.provider} · {settings.effective_model}",
            border_style="blue",
        )
    )

    agent = _build_agent(settings, root)
    user_input = f"请审查：{rel}"
    if ask:
        user_input += f"。补充要求：{ask}"

    result = agent.run(user_input)
    if not result.ok:
        _print_stats(result)
        raise typer.Exit(code=1)
    _render_report(result.content)
    _print_stats(result)


# ================================================================ chat
@app.command("chat")
def chat(
    session: str | None = typer.Option(
        None, "--session", "-s", help="恢复指定的历史会话 ID"
    ),
) -> None:
    """交互式多轮对话：支持追问、历史会话与逐条修复确认。"""
    settings = _require_configured()

    memory = None
    if session:
        try:
            memory = SessionMemory.load(session)
        except FileNotFoundError:
            console.print(f"[red]✗ 会话不存在：{session}[/red]")
            raise typer.Exit(code=1) from None
        console.print(f"[green]已恢复会话 {session}（{len(memory.messages)} 条消息）[/green]")

    # 审查边界 = 当前工作目录
    root = Path.cwd().resolve()
    agent = _build_agent(settings, root, memory=memory)

    console.print(
        Panel(
            "交互式审查（审查边界 = 当前目录）\n"
            "命令：/exit 退出 · /reset 重置会话 · /sessions 查看历史会话",
            title="chat",
            border_style="blue",
        )
    )
    try:
        while True:
            text = Prompt.ask("\n[bold]你[/bold]").strip()
            if not text:
                continue
            if text in {"/exit", "/quit"}:
                break
            if text == "/reset":
                agent = _build_agent(settings, root)  # 新 memory = 空会话
                console.print("[green]已重置为新会话[/green]")
                continue
            if text == "/sessions":
                sessions = list_sessions()
                if not sessions:
                    console.print("[dim]暂无历史会话[/dim]")
                for item in sessions:
                    console.print(
                        f"  [cyan]{item['session_id']}[/cyan] "
                        f"({item['message_count']} 条) {item['preview']}"
                    )
                continue

            result = agent.run(text)
            if not result.ok:
                _print_stats(result)
                continue
            _render_report(result.content, title="回复")
            _print_stats(result)
            saved = agent.memory.save()
            console.print(f"[dim]会话已保存 → {saved.name}（ID: {agent.memory.session_id}）[/dim]")
    except (KeyboardInterrupt, EOFError):
        pass  # Ctrl+C / Ctrl+D 视为正常退出
    console.print(f"[dim]已退出。会话 ID：{agent.memory.session_id}，"
                  f"可用 --session 恢复。[/dim]")


# ================================================================ config
@config_app.command("show")
def config_show() -> None:
    """显示当前生效配置（API Key 已脱敏）。"""
    settings = load_settings()
    for key, value in settings.to_public_dict().items():
        typer.echo(f"{key}: {value}")


@config_app.command("set")
def config_set(
    provider: str | None = typer.Option(
        None, "--provider", help="deepseek | mimo | openai-compatible"
    ),
    base_url: str | None = typer.Option(None, "--base-url", help="API 端点"),
    api_key: str | None = typer.Option(None, "--api-key", help="API Key"),
    model: str | None = typer.Option(None, "--model", help="模型名"),
    temperature: float | None = typer.Option(None, "--temperature", help="0~2"),
    max_iterations: int | None = typer.Option(None, "--max-iterations", help="循环上限"),
    tool_timeout: int | None = typer.Option(None, "--tool-timeout", help="工具超时秒"),
    auto_fix: bool | None = typer.Option(
        None, "--auto-fix/--no-auto-fix", help="是否允许自动修复"
    ),
) -> None:
    """修改配置并保存到 config.yaml（仅写本次指定的项）。"""
    provided = {
        "provider": provider,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": temperature,
        "max_iterations": max_iterations,
        "tool_timeout": tool_timeout,
        "auto_fix_enabled": auto_fix,
    }
    overrides = {k: v for k, v in provided.items() if v is not None}
    if not overrides:
        console.print(
            "[red]未提供任何修改项[/red]。示例：\n"
            "  python main.py config set --provider deepseek --api-key sk-xxx\n"
            "  python main.py config set --provider mimo"
        )
        raise typer.Exit(code=1)

    try:
        # env={}：只基于「文件 + 本次参数」合并，避免把环境变量烤进配置文件
        settings = load_settings(overrides=overrides, env={})
        save_settings(settings)
    except ConfigError as exc:
        console.print(f"[red]✗ 配置无效：{exc}[/red]")
        raise typer.Exit(code=1) from None

    console.print("[green]✓ 已保存到 config.yaml[/green]（API Key 脱敏展示）")
    for key, value in settings.to_public_dict().items():
        typer.echo(f"{key}: {value}")


@config_app.command("test")
def config_test() -> None:
    """测试 LLM 连通性（验证服务商 / api_key / model 是否可用）。"""
    from app.llm.client import LLMClient

    settings = load_settings()
    console.print(f"正在测试 {settings.provider}（{settings.effective_base_url}）...")
    ok, message = LLMClient(settings).test_connection()
    if ok:
        console.print(f"[green]✓ {message}[/green]")
    else:
        console.print(f"[red]✗ {message}[/red]")
        raise typer.Exit(code=1)
