"""LLM 适配层单元测试（离线：注入假 call_fn，不发真实请求）。

覆盖点（对应 docs/测试文档.md · 2.2）：
- 成功路径不触发重试与休眠
- 可重试错误：退避序列 1s→2s→4s、最多 3 次重试、耗尽后抛错
- 不可重试错误：立即抛出，不做第二次尝试
- 配置不完整时的前置拦截
- 响应归一化 parse_completion（dict / tool_calls / 非法 JSON 参数）
- 连通性自检 test_connection 的成功与失败分支
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.client import (
    RETRY_DELAYS,
    LLMClient,
    LLMError,
    LLMNotConfiguredError,
    LLMReply,
    ToolCall,
    parse_completion,
)


def make_settings(**overrides) -> Settings:
    """构造一份已配置完整的测试用 Settings（deepseek 预设 + 假 Key）。"""
    base = {"provider": "deepseek", "api_key": "sk-test-key"}
    base.update(overrides)
    return Settings(**base)


class FakeClock:
    """记录 sleep 调用的假时钟：让退避瞬间完成并留下记录。"""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def ok_reply(text: str = "ok") -> LLMReply:
    return LLMReply(content=text, model="fake-model", finish_reason="stop")


# ---------------------------------------------------------------- chat 重试
def test_success_no_retry() -> None:
    """首次成功：只调用 1 次，不发生任何休眠。"""
    calls: list[int] = []
    clock = FakeClock()

    def call_fn(messages, tools):
        calls.append(1)
        return ok_reply("你好")

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=clock.sleep)
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "你好"
    assert len(calls) == 1
    assert clock.sleeps == []


def test_retry_then_success() -> None:
    """两次瞬时失败后成功：共 3 次调用，退避为 1s、2s。"""
    calls: list[int] = []
    clock = FakeClock()

    def call_fn(messages, tools):
        calls.append(1)
        if len(calls) < 3:
            raise LLMError("500 server error", retryable=True)
        return ok_reply("恢复了")

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=clock.sleep)
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert reply.content == "恢复了"
    assert len(calls) == 3
    assert clock.sleeps == [1.0, 2.0]


def test_retry_exhausted() -> None:
    """持续瞬时失败：1 + 3 次尝试后抛错，退避序列为 1/2/4。"""
    calls: list[int] = []
    clock = FakeClock()

    def call_fn(messages, tools):
        calls.append(1)
        raise LLMError("boom", retryable=True)

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=clock.sleep)
    with pytest.raises(LLMError, match="次尝试后仍失败"):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1 + len(RETRY_DELAYS)  # 4 次
    assert clock.sleeps == [1.0, 2.0, 4.0]


def test_non_retryable_fails_fast() -> None:
    """不可重试错误（如 401）：只调用 1 次，立即抛出，不休眠。"""
    calls: list[int] = []
    clock = FakeClock()

    def call_fn(messages, tools):
        calls.append(1)
        raise LLMError("401 认证失败", retryable=False)

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=clock.sleep)
    with pytest.raises(LLMError, match="401"):
        client.chat([{"role": "user", "content": "hi"}])
    assert len(calls) == 1
    assert clock.sleeps == []


def test_not_configured_blocks_call() -> None:
    """缺少 api_key：在发起任何请求之前就报配置错误。"""
    calls: list[int] = []

    def call_fn(messages, tools):
        calls.append(1)
        return ok_reply()

    client = LLMClient(
        Settings(provider="deepseek", api_key=""),  # 无 Key
        call_fn=call_fn,
        sleep=lambda s: None,
    )
    with pytest.raises(LLMNotConfiguredError, match="api_key"):
        client.chat([{"role": "user", "content": "hi"}])
    assert calls == []  # 一次都没发出去


def test_retry_hook_invoked() -> None:
    """重试回调应携带「第几次、异常、等待秒数」。"""
    events: list[tuple[int, str, float]] = []

    def call_fn(messages, tools):
        raise LLMError("429 限流", retryable=True)

    client = LLMClient(
        make_settings(),
        call_fn=call_fn,
        sleep=lambda s: None,
        on_retry=lambda n, err, delay: events.append((n, str(err), delay)),
    )
    with pytest.raises(LLMError):
        client.chat([{"role": "user", "content": "hi"}])
    assert [e[0] for e in events] == [1, 2, 3]  # 三次重试各回调一次
    assert [e[2] for e in events] == [1.0, 2.0, 4.0]
    assert "限流" in events[0][1]


def test_tools_passed_through() -> None:
    """工具 schema 应原样透传给底层调用。"""
    seen_tools: list = []
    tools_payload = [
        {"type": "function", "function": {"name": "read_file", "parameters": {}}}
    ]

    def call_fn(messages, tools):
        seen_tools.extend(tools or [])
        return ok_reply()

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=lambda s: None)
    client.chat([{"role": "user", "content": "hi"}], tools=tools_payload)
    assert seen_tools == tools_payload


# ---------------------------------------------------------------- 响应解析
def test_parse_dict_with_tool_calls() -> None:
    """dict 形态响应：content 与 tool_calls 均正确归一化。"""
    resp = {
        "model": "m1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "a.py"}',
                            },
                        }
                    ],
                },
            }
        ],
    }
    reply = parse_completion(resp)
    assert reply.model == "m1"
    assert reply.finish_reason == "tool_calls"
    assert len(reply.tool_calls) == 1
    tc = reply.tool_calls[0]
    assert isinstance(tc, ToolCall)
    assert tc.id == "call_1" and tc.name == "read_file"
    assert tc.arguments == {"path": "a.py"}


def test_parse_invalid_json_arguments() -> None:
    """非法 JSON 参数：不崩溃，原文保留在 _raw 中等待工具层回填。"""
    resp = {
        "model": "m1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "稍等",
                    "tool_calls": [
                        {
                            "id": "c",
                            "function": {"name": "x", "arguments": "{broken"},
                        }
                    ],
                },
            }
        ],
    }
    reply = parse_completion(resp)
    assert reply.content == "稍等"
    assert reply.tool_calls[0].arguments == {"_raw": "{broken"}


def test_parse_plain_text() -> None:
    """纯文本响应：tool_calls 为空列表。"""
    resp = {
        "model": "m1",
        "choices": [{"finish_reason": "stop", "message": {"content": "审查完毕"}}],
    }
    reply = parse_completion(resp)
    assert reply.content == "审查完毕"
    assert reply.tool_calls == []


# ---------------------------------------------------------------- 连通性自检
def test_connection_success() -> None:
    """配置完整 + 假 call_fn 成功 → (True, 包含模型名的说明)。"""
    client = LLMClient(
        make_settings(), call_fn=lambda m, t: ok_reply("连通"), sleep=lambda s: None
    )
    ok, msg = client.test_connection()
    assert ok is True
    assert "连接成功" in msg and "fake-model" in msg


def test_connection_reports_missing_key() -> None:
    """无 Key：返回配置不完整说明，而不是抛异常。"""
    client = LLMClient(
        Settings(provider="deepseek", api_key=""),
        call_fn=lambda m, t: ok_reply(),
        sleep=lambda s: None,
    )
    ok, msg = client.test_connection()
    assert ok is False
    assert "api_key" in msg


def test_connection_reports_llm_error() -> None:
    """调用失败：返回「连接失败 + 原因」，不抛异常。"""
    def call_fn(messages, tools):
        raise LLMError("认证失败", retryable=False)

    client = LLMClient(make_settings(), call_fn=call_fn, sleep=lambda s: None)
    ok, msg = client.test_connection()
    assert ok is False
    assert "连接失败" in msg and "认证失败" in msg
