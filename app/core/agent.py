"""Agent 核心 —— ReAct 循环：推理 → 工具调用 → 结果回填 → 循环。

流程（对应 Design.md 第 4 节）：
    用户输入 → 组装消息 → LLM
        ├─ 返回 tool_calls → 注册表执行 → 结果回填为 tool 消息 → 下一轮
        └─ 返回纯文本     → 最终审查报告，结束
    终止条件（任一）：
        1. 模型返回纯文本（不再调用工具）
        2. 达到 settings.max_iterations → 注入「强制收尾」提示，
           关闭工具再调用一次，保证拿到部分报告而不是硬失败
        3. LLM 层不可恢复错误 → 捕获并转为带 error 的结果

事件（AgentEvent）贯穿全程：每轮推理、每次工具调用、重试、最终报告
都会推送给 on_event 回调 —— CLI 的滚动时间线与 Web 的执行时间线
都由它渲染，是「Agent 在想什么」的可视化基础。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from app.core.memory import SessionMemory
from app.core.prompt import build_system_prompt
from app.llm.client import LLMClient, LLMError
from app.tools.registry import ToolRegistry

# 单条事件文本里工具参数摘要的最大长度
ARGS_SUMMARY_LIMIT = 120


@dataclass
class AgentEvent:
    """一条执行时间线事件。"""

    kind: str  # llm | tool | tool_result | retry | info | final | error
    text: str  # 人类可读的一行描述
    ts: float = field(default_factory=time.time)
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    """一次 run() 的最终结果。"""

    content: str  # 最终文本（审查报告原文）
    iterations: int  # 实际 LLM 推理轮数
    tool_calls: int  # 工具调用总次数
    truncated: bool  # 是否因超轮数而强制收尾
    error: str | None  # 非 None 表示本次运行失败
    events: list[AgentEvent] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.error is None


# 事件回调签名：(事件) -> None
EventHook = Callable[[AgentEvent], None]


def _args_summary(arguments: dict[str, Any]) -> str:
    """把工具参数压缩成一行摘要，用于时间线展示。"""
    try:
        text = json.dumps(arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(arguments)
    text = text.replace("\n", " ")
    if len(text) > ARGS_SUMMARY_LIMIT:
        text = text[:ARGS_SUMMARY_LIMIT] + "…"
    return text


class Agent:
    """ReAct 循环执行器。

    Args:
        settings: 运行配置（max_iterations 等）
        registry: 已装载工具的注册表（其 ctx.root 决定审查边界与系统提示词）
        memory: 会话记忆；缺省按审查根目录新建
        llm: LLM 客户端；缺省按 settings 构造（真实调用）
        on_event: 时间线回调（CLI/Web 注册以实时渲染）
    """

    def __init__(
        self,
        settings: Settings,
        registry: ToolRegistry,
        *,
        memory: SessionMemory | None = None,
        llm: Any | None = None,
        on_event: EventHook | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._memory = memory or SessionMemory(
            build_system_prompt(str(registry.ctx.root))
        )
        self._on_event = on_event
        self._events: list[AgentEvent] = []
        if llm is not None:
            self._llm = llm
        else:
            # 真实客户端：把 LLM 层的重试转成时间线事件（CLI/Web 展示「重试中」）
            self._llm = LLMClient(settings, on_retry=self._on_llm_retry)

    # ---------------- 事件 ----------------
    def _emit(self, kind: str, text: str, **data: Any) -> AgentEvent:
        event = AgentEvent(kind=kind, text=text, data=data)
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)
        return event

    def _on_llm_retry(self, attempt: int, error: LLMError, delay: float) -> None:
        """LLMClient 重试回调 → 时间线事件。"""
        self._emit(
            "retry",
            f"LLM 第 {attempt} 次重试（{delay:.0f}s 后）：{error}",
            attempt=attempt,
            delay=delay,
        )

    # ---------------- 主循环 ----------------
    @property
    def memory(self) -> SessionMemory:
        """会话记忆（界面层用于持久化 / 展示历史）。"""
        return self._memory

    def run(self, user_input: str) -> AgentResult:
        """执行一次完整的 ReAct 循环，直到产出最终文本或触发终止条件。

        永不抛 LLM/工具异常 —— 失败信息进入结果的 error 字段与时间线。
        """
        self._events = []
        self._memory.add_user(user_input)
        iterations = 0
        tool_count = 0
        max_iter = self._settings.max_iterations

        try:
            for iterations in range(1, max_iter + 1):
                # ① 推理
                self._emit("llm", f"第 {iterations}/{max_iter} 轮推理", round=iterations)
                reply = self._llm.chat(self._memory.messages, tools=self._registry.schemas())

                # ② 无工具调用 → 最终报告，结束
                if not reply.tool_calls:
                    text = reply.content or ""
                    self._memory.add_assistant(reply)
                    preview = text.replace("\n", " ")[:100]
                    self._emit("final", f"产出最终结果：{preview}", content=text)
                    return AgentResult(
                        content=text,
                        iterations=iterations,
                        tool_calls=tool_count,
                        truncated=False,
                        error=None,
                        events=list(self._events),
                    )

                # ③ 有工具调用 → 回填助手消息，逐个执行并回填结果
                self._memory.add_assistant(reply)
                for tc in reply.tool_calls:
                    tool_count += 1
                    self._emit(
                        "tool",
                        f"调用 {tc.name}({_args_summary(tc.arguments)})",
                        name=tc.name,
                        args=tc.arguments,
                        index=tool_count,
                    )
                    # 注册表永不抛异常：失败以 "Error: ..." 文本回填
                    result = self._registry.execute(tc.name, tc.arguments)
                    failed = result.startswith("Error:")
                    self._emit(
                        "tool_result",
                        ("✗ " if failed else "✓ ")
                        + result.replace("\n", "↵")[:150],
                        ok=not failed,
                        result=result,
                    )
                    self._memory.add_tool_result(tc.id, result)

            # ④ 轮数耗尽 → 强制收尾：注入提示并关闭工具，保证拿到部分报告
            self._emit(
                "info",
                f"已达最大轮数（{max_iter}），基于已有信息强制生成部分报告",
                truncated=True,
            )
            self._memory.add_user(
                "（系统提示：已达到最大工具调用轮数，禁止再调用工具，"
                "请立即基于已获得的信息输出最终审查报告，遵循规定的 JSON 格式。）"
            )
            final = self._llm.chat(self._memory.messages, tools=None)
            self._memory.add_assistant(final)
            text = final.content or ""
            self._emit("final", "产出部分报告（已截断）", content=text)
            return AgentResult(
                content=text,
                iterations=max_iter + 1,
                tool_calls=tool_count,
                truncated=True,
                error=None,
                events=list(self._events),
            )

        except LLMError as exc:
            # ⑤ LLM 层不可恢复错误：转成带 error 的结果，绝不向上冒泡
            self._emit("error", f"运行失败：{exc}")
            return AgentResult(
                content=f"Agent 运行失败：{exc}",
                iterations=iterations,
                tool_calls=tool_count,
                truncated=False,
                error=str(exc),
                events=list(self._events),
            )
