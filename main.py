"""Code Review Agent 统一入口。

用法示例：
    python main.py --help                 # 查看全部命令
    python main.py config show            # 查看当前配置（Key 脱敏）
    python main.py config set --provider deepseek --api-key sk-xxx
    python main.py config test            # 测试 LLM 连通性
    python main.py review <路径>           # 审查文件 / 目录
    python main.py chat                   # 交互式多轮对话
    python main.py web                    # 启动 Web 界面
"""

from __future__ import annotations

import typer

from app import __version__

# 根应用：聚合各子命令组
app = typer.Typer(
    name="code-review-agent",
    help="Code Review Agent —— 基于 LLM 的代码审查助手",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """根回调：仅承载帮助信息，无实际逻辑。"""


@app.command("version")
def version() -> None:
    """显示版本号。"""
    typer.echo(f"Code Review Agent v{__version__}")


# ---------------- 配置子命令（M1 先落地 show，M5 扩展 set/test） ----------------
config_app = typer.Typer(help="查看 / 修改配置", no_args_is_help=True)
app.add_typer(config_app, name="config")


@config_app.command("show")
def config_show() -> None:
    """显示当前生效配置（API Key 已脱敏）。"""
    from app.config import load_settings

    settings = load_settings()
    for key, value in settings.to_public_dict().items():
        typer.echo(f"{key}: {value}")


@config_app.command("test")
def config_test() -> None:
    """测试 LLM 连通性（验证服务商 / api_key / model 是否可用）。"""
    from app.config import load_settings
    from app.llm.client import LLMClient

    settings = load_settings()
    typer.echo(f"正在测试 {settings.provider} ({settings.effective_base_url}) ...")
    ok, message = LLMClient(settings).test_connection()
    if ok:
        typer.secho(f"✓ {message}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"✗ {message}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
