"""Agent 循环单元测试（FakeLLM 离线驱动，不发真实请求）。

覆盖点（对应 docs/测试文档.md · 2.4）：
- 纯文本直接结束
- 多轮工具调用收敛：事件序列、消息配对、工具 schema 透传
- 工具错误回填后模型可见
- 轮数耗尽 → 强制收尾（关闭工具 + 注入提示 + truncated 标记）
- LLM 不可恢复错误 → 转为 error 结果，不向上抛异常
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.core.agent import Agent, AgentEvent
from app.core.prompt import parse_review_report
from app.llm.client import LLMError, LLMReply, ToolCall
from app.tools.builtin import build_default_registry
from app.tools.registry import ToolContext, ToolRegistry


# ---------------------------------------------------------------- 测试工具
class FakeLLM:
    """按脚本顺序返回回复的假 LLM，并记录每次调用现场。"""

    def __init__(self, replies: list) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    def chat(self, messages, tools=None):
        # 浅拷贝快照，避免后续 mutation 影响断言
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        item = self._replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def text_reply(text: str) -> LLMReply:
    return LLMReply(content=text, model="fake", finish_reason="stop")


def tool_reply(*calls: tuple[str, dict, str]) -> LLMReply:
    """(name, arguments, id) 三元组 → 带 tool_calls 的回复。"""
    return LLMReply(
        content=None,
        tool_calls=[ToolCall(id=cid, name=name, arguments=args) for name, args, cid in calls],
        finish_reason="tool_calls",
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def registry(root: Path) -> ToolRegistry:
    return build_default_registry(
        ToolContext(root=root, confirm_fn=lambda p, d: True)
    )


def make_settings(**kw) -> Settings:
    base = {"provider": "deepseek", "api_key": "sk-test", "max_iterations": 5}
    base.update(kw)
    return Settings(**base)


def assert_pairing(messages: list[dict]) -> None:
    """不变量：带 tool_calls 的 assistant 后面必须紧跟其 tool 结果。"""
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            ids = {tc["id"] for tc in msg["tool_calls"]}
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            assert nxt is not None, "assistant(tool_calls) 后缺少 tool 消息"
            assert nxt["role"] == "tool"
            assert nxt.get("tool_call_id") in ids


# ---------------------------------------------------------------- 循环主路径
def test_direct_text_reply(registry: ToolRegistry) -> None:
    """模型不调用工具 → 立即产出结果，只有 llm + final 两类事件。"""
    fake = FakeLLM([text_reply("审查完毕，无问题")])
    events: list[AgentEvent] = []
    agent = Agent(make_settings(), registry, llm=fake, on_event=events.append)
    result = agent.run("审查 app.py")

    assert result.ok and not result.truncated
    assert result.content == "审查完毕，无问题"
    assert result.iterations == 1 and result.tool_calls == 0
    assert [e.kind for e in events] == ["llm", "final"]
    # 第一轮仍然把工具 schema 交给了模型
    assert len(fake.calls[0]["tools"]) == 5


def test_tool_loop_happy_path(registry: ToolRegistry) -> None:
    """list_dir → read_file → 最终报告：事件、配对、schema 全部正确。"""
    fake = FakeLLM([
        tool_reply(("list_dir", {"path": "."}, "c1")),
        tool_reply(("read_file", {"path": "app.py"}, "c2")),
        text_reply('```json\n{"summary": "代码健康", "issues": []}\n```'),
    ])
    events: list[AgentEvent] = []
    agent = Agent(make_settings(), registry, llm=fake, on_event=events.append)
    result = agent.run("请审查这个目录")

    assert result.ok
    assert result.iterations == 3 and result.tool_calls == 2
    assert [e.kind for e in events] == [
        "llm", "tool", "tool_result", "llm", "tool", "tool_result", "llm", "final",
    ]
    # 每轮都带 5 个工具的 schema
    assert all(len(c["tools"]) == 5 for c in fake.calls)
    # 消息配对不变量 + system 恒在首位
    msgs = agent.memory.messages
    assert msgs[0]["role"] == "system"
    assert_pairing(msgs)
    # 工具结果是真实的（list_dir 看到了 app.py）
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert "app.py" in tool_msgs[0]["content"]
    # 报告可被解析
    report = parse_review_report(result.content)
    assert report["summary"] == "代码健康" and report["issues"] == []


def test_tool_error_backfilled(registry: ToolRegistry) -> None:
    """未知工具的 Error 文本回填给模型，下一轮调用时模型能看到它。"""
    fake = FakeLLM([
        tool_reply(("launch_nukes", {"target": "x"}, "c1")),
        text_reply("已知失败，改为文本建议"),
    ])
    agent = Agent(make_settings(), registry, llm=fake)
    result = agent.run("随便审查")

    assert result.ok
    second_call = fake.calls[1]["messages"]
    tool_msgs = [m for m in second_call if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"].startswith("Error:")
    assert "launch_nukes" in tool_msgs[0]["content"]
    # 时间线上也要标出失败
    failed_events = [e for e in result.events if e.kind == "tool_result" and not e.data.get("ok")]
    assert len(failed_events) == 1


def test_truncation_forces_final_report(registry: ToolRegistry) -> None:
    """轮数耗尽：关闭工具、注入强制收尾提示、结果标记 truncated。"""
    fake = FakeLLM([
        tool_reply(("list_dir", {"path": "."}, "c1")),
        tool_reply(("list_dir", {"path": "."}, "c2")),
        text_reply('{"summary": "部分报告", "issues": []}'),
    ])
    agent = Agent(make_settings(max_iterations=2), registry, llm=fake)
    result = agent.run("审查")

    assert result.truncated and result.ok
    assert result.iterations == 3  # 2 轮工具 + 1 次强制收尾
    assert result.content == '{"summary": "部分报告", "issues": []}'
    # 第三次调用：tools=None（禁用工具），且最后一条消息是强制提示
    last_call = fake.calls[2]
    assert last_call["tools"] is None
    assert "禁止再调用工具" in last_call["messages"][-1]["content"]
    # 时间线含 info 事件
    assert any(e.kind == "info" and e.data.get("truncated") for e in result.events)


def test_llm_error_captured_not_raised(registry: ToolRegistry) -> None:
    """LLM 不可恢复错误 → error 字段承载，不向调用方抛异常。"""
    fake = FakeLLM([LLMError("401 认证失败", retryable=False)])
    events: list[AgentEvent] = []
    agent = Agent(make_settings(), registry, llm=fake, on_event=events.append)
    result = agent.run("审查")

    assert not result.ok
    assert "401" in (result.error or "")
    assert result.content.startswith("Agent 运行失败")
    assert events[-1].kind == "error"


def test_retry_hook_emits_event(registry: ToolRegistry) -> None:
    """LLM 层重试回调会被转成时间线 retry 事件。"""
    events: list[AgentEvent] = []
    agent = Agent(make_settings(), registry, llm=FakeLLM([]), on_event=events.append)
    agent._on_llm_retry(2, LLMError("429", retryable=True), 2.0)
    assert events[-1].kind == "retry"
    assert "第 2 次重试" in events[-1].text


def test_multi_turn_memory_reused(registry: ToolRegistry) -> None:
    """同一个 Agent 跑两轮：第二轮的调用能看到第一轮的历史。"""
    fake = FakeLLM([
        text_reply("第一轮答复"),
        text_reply("第二轮答复"),
    ])
    agent = Agent(make_settings(), registry, llm=fake)
    agent.run("第一个问题")
    agent.run("第二个问题")

    assert len(fake.calls) == 2
    second_roles = [m["role"] for m in fake.calls[1]["messages"]]
    # 第二轮调用包含：system + user(第一问) + assistant(第一答) + user(第二问)
    assert second_roles == ["system", "user", "assistant", "user"]
    assert fake.calls[1]["messages"][1]["content"] == "第一个问题"
