"""CLI 层测试（typer CliRunner 离线驱动，不发真实 LLM 请求）。

覆盖点（对应 docs/测试文档.md · 3.3）：
- version / config show / config set 基本行为与配置文件隔离
- review：未配置的友好退出、路径不存在、假 Agent 渲染报告主链路
- chat：历史会话不存在时的友好退出
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import app.cli.app as cli_mod
import app.config as config_mod
from app.core.agent import Agent
from app.llm.client import LLMReply

runner = CliRunner()

# 带一个 error 级问题的标准报告
REPORT_JSON = (
    '```json\n{"summary": "发现 1 个严重问题", "issues": ['
    '{"severity": "error", "file": "app.py", "line": 42,'
    ' "message": "SQL 拼接注入风险", "suggestion": "改用参数化查询"}]}\n```'
)


class FakeLLM:
    """固定返回一串回复的假 LLM。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return self._replies.pop(0)


@pytest.fixture()
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置文件隔离到临时目录（默认指向不存在 → 未配置状态）。"""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    return path


def write_config(path: Path, content: str = "provider: deepseek\napi_key: sk-test-key\n") -> None:
    path.write_text(content, encoding="utf-8")


# ================================================================ 基础命令
def test_version() -> None:
    result = runner.invoke(cli_mod.app, ["version"])
    assert result.exit_code == 0
    assert "Code Review Agent v" in result.stdout


def test_help_lists_commands() -> None:
    result = runner.invoke(cli_mod.app, ["--help"])
    assert result.exit_code == 0
    for name in ("review", "chat", "config", "version"):
        assert name in result.stdout


def test_config_show_masked(isolated_config: Path) -> None:
    write_config(isolated_config)
    result = runner.invoke(cli_mod.app, ["config", "show"])
    assert result.exit_code == 0
    assert "provider: deepseek" in result.stdout
    assert "sk-test-key" not in result.stdout  # Key 已脱敏
    assert "sk-***-key" in result.stdout or "sk-*" in result.stdout


def test_config_set_provider(isolated_config: Path) -> None:
    """config set 只写指定项到隔离的配置文件。"""
    result = runner.invoke(cli_mod.app, ["config", "set", "--provider", "mimo"])
    assert result.exit_code == 0
    assert "已保存" in result.stdout
    content = isolated_config.read_text(encoding="utf-8")
    assert "provider: mimo" in content


def test_config_set_no_args(isolated_config: Path) -> None:
    result = runner.invoke(cli_mod.app, ["config", "set"])
    assert result.exit_code == 1
    assert "未提供任何修改项" in result.stdout


def test_config_set_invalid_provider(isolated_config: Path) -> None:
    result = runner.invoke(
        cli_mod.app, ["config", "set", "--provider", "not-a-provider"]
    )
    assert result.exit_code == 1
    assert "配置无效" in result.stdout


# ================================================================ review
def test_review_without_config(isolated_config: Path) -> None:
    """无配置文件 → 友好提示 + 退出码 1，不抛栈。"""
    result = runner.invoke(cli_mod.app, ["review", "."])
    assert result.exit_code == 1
    assert "配置不完整" in result.stdout
    assert "config set" in result.stdout


def test_review_missing_path(isolated_config: Path) -> None:
    write_config(isolated_config)
    result = runner.invoke(cli_mod.app, ["review", "no-such-path-xyz"])
    assert result.exit_code == 1
    assert "路径不存在" in result.stdout


def test_review_renders_report(
    isolated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假 Agent 主链路：输出包含面板、表格、问题与统计行。"""
    write_config(isolated_config)
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")

    # 用假 LLM 替换真实 Agent（审查结果完全离线可控）
    def fake_agent_factory(settings, registry, memory=None, on_event=None):
        fake_llm = FakeLLM(
            [LLMReply(content=REPORT_JSON, model="fake", finish_reason="stop")]
        )
        return Agent(
            settings, registry, memory=memory, llm=fake_llm, on_event=on_event
        )

    monkeypatch.setattr(cli_mod, "Agent", fake_agent_factory)
    result = runner.invoke(cli_mod.app, ["review", str(target), "-a", "重点看安全"])
    assert result.exit_code == 0, result.stdout
    assert "审查报告" in result.stdout
    assert "SQL 拼接注入风险" in result.stdout
    assert "改用参数化查询" in result.stdout
    assert "严重" in result.stdout
    assert "推理 1 轮" in result.stdout


def test_review_time_line_events(
    isolated_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """假 Agent 带工具调用时，时间线事件出现在输出中。"""
    from app.llm.client import ToolCall

    write_config(isolated_config)
    target = tmp_path / "sample.py"
    target.write_text("x = 1\n", encoding="utf-8")

    def fake_agent_factory(settings, registry, memory=None, on_event=None):
        fake_llm = FakeLLM(
            [
                LLMReply(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="read_file",
                                          arguments={"path": "sample.py"})],
                    finish_reason="tool_calls",
                ),
                LLMReply(content=REPORT_JSON, model="fake", finish_reason="stop"),
            ]
        )
        return Agent(
            settings, registry, memory=memory, llm=fake_llm, on_event=on_event
        )

    monkeypatch.setattr(cli_mod, "Agent", fake_agent_factory)
    result = runner.invoke(cli_mod.app, ["review", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "第 1/8 轮推理" in result.stdout      # llm 事件（默认上限 8 轮）
    assert "调用 read_file" in result.stdout      # tool 事件
    assert "推理 2 轮 · 工具调用 1 次" in result.stdout


# ================================================================ chat
def test_chat_missing_session(isolated_config: Path) -> None:
    write_config(isolated_config)
    result = runner.invoke(cli_mod.app, ["chat", "-s", "no-such-session"])
    assert result.exit_code == 1
    assert "会话不存在" in result.stdout
    # 进入命令后应先展示艺术字横幅（空白归一化后匹配 figlet 特征行）
    from app.cli.banner import BANNER

    normalized = " ".join(result.stdout.split())
    banner_line = " ".join(BANNER.splitlines()[4].split())
    assert banner_line in normalized


def test_chat_invalid_root(isolated_config: Path) -> None:
    """--root 指向不存在的目录 → 友好报错 + 退出码 1。"""
    write_config(isolated_config)
    result = runner.invoke(cli_mod.app, ["chat", "-r", "no-such-root-xyz"])
    assert result.exit_code == 1
    assert "审查根目录不存在" in result.stdout


def test_review_help_documents_external_paths() -> None:
    """review 帮助须指引绝对路径与拖拽用法（可审查任意磁盘目录）。"""
    result = runner.invoke(cli_mod.app, ["review", "--help"])
    assert result.exit_code == 0
    assert "绝对路径" in result.stdout
    assert "拖到" in result.stdout


def test_chat_prompt_shows_current_root(isolated_config: Path) -> None:
    """每一轮输入提示符都要带当前审查根目录名（常驻可见）。"""
    write_config(isolated_config)
    from pathlib import Path as _Path

    root_name = _Path.cwd().name  # pytest 从项目根目录启动
    result = runner.invoke(cli_mod.app, ["chat"], input="/exit\n")
    assert result.exit_code == 0
    normalized = " ".join(result.stdout.split())
    assert f"{root_name} ›" in normalized
