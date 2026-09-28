"""上下文记忆单元测试：截断安全、配对不变量、持久化回环。"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.memory import SessionMemory, list_sessions
from app.llm.client import LLMReply, ToolCall


def make_mem(max_messages: int = 6, sid: str | None = None) -> SessionMemory:
    return SessionMemory("SYSTEM-PROMPT", max_messages=max_messages, session_id=sid)


def text_reply(text: str) -> LLMReply:
    return LLMReply(content=text)


def tool_reply(call_id: str, name: str = "read_file") -> LLMReply:
    return LLMReply(tool_calls=[ToolCall(id=call_id, name=name, arguments={"path": "x"})])


def assert_pairing(messages: list[dict]) -> None:
    """不变量：assistant(tool_calls) 必须紧跟对应的 tool 结果。"""
    for i, msg in enumerate(messages):
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            ids = {tc["id"] for tc in msg["tool_calls"]}
            nxt = messages[i + 1] if i + 1 < len(messages) else None
            assert nxt is not None and nxt["role"] == "tool"
            assert nxt.get("tool_call_id") in ids


def test_capacity_bounds_and_system_kept() -> None:
    """大量对话后：总条数不超容量，system 恒在首位。"""
    mem = make_mem(max_messages=6)
    for i in range(10):
        mem.add_user(f"问题 {i}")
        mem.add_assistant(text_reply(f"回答 {i}"))
    assert len(mem.messages) <= 6
    assert mem.messages[0] == {"role": "system", "content": "SYSTEM-PROMPT"}


def test_truncation_keeps_pairs_intact() -> None:
    """含工具调用的历史截断后，配对不变量仍然成立。"""
    mem = make_mem(max_messages=6)
    for i in range(5):
        mem.add_user(f"审查 {i}")
        mem.add_assistant(tool_reply(f"c{i}"))
        mem.add_tool_result(f"c{i}", f"结果 {i}")
        mem.add_assistant(text_reply(f"报告 {i}"))
    assert len(mem.messages) <= 6
    assert mem.messages[0]["role"] == "system"
    assert_pairing(mem.messages)


def test_no_truncation_without_user_boundary() -> None:
    """截断点找不到 user 边界（工具调用进行中）→ 放弃截断，保证配对。"""
    mem = make_mem(max_messages=4)
    mem.add_user("开始")
    # 之后全是工具往返，没有新的 user 消息
    for i in range(6):
        mem.add_assistant(tool_reply(f"c{i}"))
        mem.add_tool_result(f"c{i}", f"结果 {i}")
    # 容量超限但配对完整
    assert mem.messages[0]["role"] == "system"
    assert_pairing(mem.messages)
    # 每个 assistant(tool_calls) 都能对上 tool
    tool_call_assistants = [
        m for m in mem.messages if m["role"] == "assistant" and m.get("tool_calls")
    ]
    assert len(tool_call_assistants) == 6


def test_tool_arguments_serialized_as_json_string() -> None:
    """assistant.tool_calls[].function.arguments 必须是 JSON 字符串（API 要求）。"""
    mem = make_mem()
    mem.add_assistant(tool_reply("cid"))
    msg = mem.messages[-1]
    fn = msg["tool_calls"][0]["function"]
    assert isinstance(fn["arguments"], str)
    assert json.loads(fn["arguments"]) == {"path": "x"}


def test_roundtrip_to_from_dict() -> None:
    """to_dict → from_dict 保持消息与会话标识不变。"""
    mem = make_mem(sid="abc123def456")
    mem.add_user("你好")
    mem.add_assistant(text_reply("你好呀"))
    restored = SessionMemory.from_dict(mem.to_dict())
    assert restored.session_id == "abc123def456"
    assert restored.messages == mem.messages


def test_save_and_load(tmp_path: Path) -> None:
    """save() 写盘 → load() 读回，内容一致。"""
    mem = make_mem(sid="save-test")
    mem.add_user("持久化测试")
    mem.add_assistant(text_reply("已保存"))
    path = mem.save(tmp_path)
    assert path.exists()
    loaded = SessionMemory.load("save-test", tmp_path)
    assert loaded.messages == mem.messages


def test_list_sessions_preview(tmp_path: Path) -> None:
    """会话列表：含摘要预览且按时间倒序可用。"""
    mem = make_mem(sid="s1")
    mem.add_user("这是一个很长的问题" + "x" * 100)
    mem.save(tmp_path)
    sessions = list_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "s1"
    assert sessions[0]["preview"].startswith("这是一个很长的问题")
    assert sessions[0]["message_count"] == 2


def test_list_sessions_skips_corrupt_file(tmp_path: Path) -> None:
    """损坏的会话文件被跳过而不是崩溃。"""
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    assert list_sessions(tmp_path) == []
