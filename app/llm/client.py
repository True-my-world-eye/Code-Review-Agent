"""LLM 适配层 —— 统一的 OpenAI 兼容客户端。

职责（对应 Design.md 第 6 节）：
1. 把 DeepSeek / 小米 MiMo / 任意 OpenAI 兼容服务商收敛到一个调用入口；
2. 错误分类 + 指数退避重试：超时/连接失败/429/5xx → 重试 3 次（1s→2s→4s），
   认证失败/参数错误 → 立即透出，不浪费重试；
3. 把 SDK 的 ChatCompletion 响应归一化为轻量的 LLMReply，屏蔽 SDK 版本差异；
4. 提供 test_connection() 连通性自检，供 CLI / Web 设置面板使用。

可测试性设计：「带重试的 chat()」与「发一次请求的 call_fn」解耦，
单测注入假的 call_fn 即可离线驱动重试逻辑，不消耗真实 Token。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings

# 重试退避序列（秒）：第 n 次失败后等待 RETRY_DELAYS[n] 再重试，共 3 次重试机会
RETRY_DELAYS: tuple[float, ...] = (1.0, 2.0, 4.0)


# ---------------------------------------------------------------- 统一异常
class LLMError(Exception):
    """LLM 调用层统一异常。

    Attributes:
        retryable: True 表示瞬时错误（超时/429/5xx），值得退避重试；
                   False 表示确定性错误（认证/参数），重试没有意义。
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMNotConfiguredError(LLMError):
    """配置不完整（缺 api_key / 自定义服务商缺 base_url 等），永不可重试。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


# ---------------------------------------------------------------- 归一化数据
@dataclass
class ToolCall:
    """模型发起的一次工具调用请求。"""

    id: str  # 回填结果时用于关联的调用 ID
    name: str  # 工具名（须在工具注册表中存在）
    arguments: dict[str, Any]  # 已解析的参数字典


@dataclass
class LLMReply:
    """归一化后的模型回复（一次 chat.completions 的产物）。"""

    content: str | None = None  # 纯文本内容；有 tool_calls 时可能为空
    tool_calls: list[ToolCall] = field(default_factory=list)  # 本轮回请的工具
    model: str = ""  # 实际响应的模型名（回显用）
    finish_reason: str = ""  # stop / tool_calls / length ...


# 单次请求函数签名：(messages, tools) → LLMReply，失败抛 LLMError
CallFn = Callable[
    [Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]] | None],
    LLMReply,
]
# 重试回调签名：(第几次重试, 异常, 本次等待秒数) —— 供 CLI/Web 展示
RetryHook = Callable[[int, LLMError, float], None]


# ---------------------------------------------------------------- 错误分类
def classify_exception(exc: BaseException) -> LLMError:
    """把 SDK / 网络异常归类为 LLMError，标记是否可重试。

    分类原则（Design.md 表 6-4）：
    - 超时、连接中断、429 限流、5xx 服务端错误 → 可重试；
    - 401/403 认证授权、400 参数错误、其他 4xx → 不可重试，立即透出。
    """
    # 延迟导入：单测环境可能只测重试逻辑，不要求 openai 可用
    from openai import (
        APIConnectionError,
        APIStatusError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
    )

    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return LLMError(f"网络异常：{exc}", retryable=True)
    if isinstance(exc, AuthenticationError):
        return LLMError(f"认证失败，请检查 api_key / 服务商（{exc}）", retryable=False)
    if isinstance(exc, BadRequestError):
        return LLMError(f"请求参数错误：{exc}", retryable=False)
    if isinstance(exc, APIStatusError):
        status = int(getattr(exc, "status_code", 0) or 0)
        # 429 与 5xx 视为瞬时故障；其余状态码按确定性错误处理
        return LLMError(
            f"HTTP {status}：{exc}", retryable=(status == 429 or status >= 500)
        )
    # 未知异常一律不重试（避免放大真实 bug）
    return LLMError(f"未预期的 LLM 调用错误：{type(exc).__name__}: {exc}", retryable=False)


# ---------------------------------------------------------------- 响应解析
def parse_completion(resp: Any) -> LLMReply:
    """把 ChatCompletion 对象（或等价 dict）归一化为 LLMReply。

    同时支持对象与 dict 两种形态：生产走 SDK 对象，单测直接喂 dict。
    """
    if isinstance(resp, dict):
        choices = resp.get("choices") or []
        first = choices[0] if choices else {}
        msg = first.get("message") or {}
        model = str(resp.get("model", ""))
        finish = str(first.get("finish_reason", "") or "")
        get = msg.get  # dict 形态
        raw_calls = msg.get("tool_calls") or []
    else:
        choices = getattr(resp, "choices", []) or []
        first = choices[0] if choices else None
        if first is None:
            raise LLMError("LLM 响应缺少 choices，无法解析", retryable=False)
        msg = first.message
        model = str(getattr(resp, "model", ""))
        finish = str(getattr(first, "finish_reason", "") or "")
        get = lambda key, default=None: getattr(msg, key, default)
        raw_calls = get("tool_calls") or []

    content = get("content")

    # 逐个解析 tool_calls：function.arguments 是 JSON 字符串
    tool_calls: list[ToolCall] = []
    for tc in raw_calls:
        if isinstance(tc, dict):
            tc_id = str(tc.get("id", ""))
            fn = tc.get("function") or {}
        else:
            tc_id = str(getattr(tc, "id", ""))
            fn = getattr(tc, "function", None)
        if isinstance(fn, dict):
            name = str(fn.get("name", ""))
            args_raw = fn.get("arguments") or "{}"
        else:
            name = str(getattr(fn, "name", ""))
            args_raw = getattr(fn, "arguments", "") or "{}"
        # 模型偶尔会产出非法 JSON 参数：不中断，保留原文供工具层报错回填
        try:
            arguments = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
        except (json.JSONDecodeError, TypeError):
            arguments = {"_raw": str(args_raw)}
        tool_calls.append(ToolCall(id=tc_id, name=name, arguments=arguments))

    return LLMReply(
        content=content, tool_calls=tool_calls, model=model, finish_reason=finish
    )


# ---------------------------------------------------------------- 客户端
class LLMClient:
    """带重试与配置校验的 OpenAI 兼容客户端。

    Args:
        settings: 配置中心产出的 Settings（含预设兜底后的生效值）
        call_fn: 可注入的「单次请求」实现；默认走 openai SDK（生产路径）
        sleep: 可注入的休眠函数；单测传假函数以瞬间完成退避
        on_retry: 重试回调，供 CLI/Web 展示「第 n 次重试」
    """

    def __init__(
        self,
        settings: Settings,
        *,
        call_fn: CallFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
        on_retry: RetryHook | None = None,
    ) -> None:
        self._settings = settings
        self._call_fn = call_fn if call_fn is not None else self._sdk_call
        self._sleep = sleep
        self._on_retry = on_retry
        # 懒加载 SDK 客户端：注入 call_fn 的单测无需构造真实 HTTP 连接
        self._client: Any = None

    # ---------------- 对外接口 ----------------
    def chat(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> LLMReply:
        """发起一次带重试的对话调用。

        Raises:
            LLMNotConfiguredError: 配置不完整
            LLMError: 重试耗尽，或遇到不可重试的确定性错误
        """
        if not self._settings.is_configured:
            raise LLMNotConfiguredError(
                "LLM 配置不完整：请先设置 api_key（自定义服务商还需 base_url 与 model）。"
                "可用 `python main.py config show` 查看当前配置。"
            )

        last_err: LLMError | None = None
        # 1 次原始尝试 + len(RETRY_DELAYS) 次重试
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return self._call_fn(messages, tools)
            except LLMError as exc:
                if not exc.retryable:
                    raise  # 确定性错误：立即透出，不再尝试
                last_err = exc
                if attempt >= len(RETRY_DELAYS):
                    break  # 重试预算耗尽
                delay = RETRY_DELAYS[attempt]
                if self._on_retry is not None:
                    self._on_retry(attempt + 1, exc, delay)
                self._sleep(delay)

        raise LLMError(
            f"LLM 调用在 {len(RETRY_DELAYS) + 1} 次尝试后仍失败：{last_err}",
            retryable=True,
        ) from last_err

    def test_connection(self) -> tuple[bool, str]:
        """连通性自检：发一条极小请求验证 Key / 端点 / 模型是否可用。

        Returns:
            (是否成功, 人类可读的说明) —— 永不抛异常，方便 UI 直接展示
        """
        if not self._settings.is_configured:
            missing = []
            if not self._settings.api_key:
                missing.append("api_key")
            if not self._settings.effective_base_url:
                missing.append("base_url")
            if not self._settings.effective_model:
                missing.append("model")
            return False, f"配置不完整，缺少：{', '.join(missing)}"
        try:
            reply = self.chat([{"role": "user", "content": "请仅回复两个字：连通"}])
        except LLMError as exc:
            return False, f"连接失败：{exc}"
        snippet = (reply.content or "").strip()[:30]
        return True, (
            f"连接成功 · model={reply.model or self._settings.effective_model}"
            f" · 回复={snippet or '(空)'}"
        )

    # ---------------- 生产路径：openai SDK ----------------
    def _get_client(self) -> Any:
        """懒加载 OpenAI 客户端（max_retries=0：重试策略由本类统一管理）。"""
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=self._settings.effective_base_url,
                api_key=self._settings.api_key,
                max_retries=0,  # 关闭 SDK 内建重试，避免与本类的退避叠加
                timeout=float(self._settings.tool_timeout),
            )
        return self._client

    def _sdk_call(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]] | None,
    ) -> LLMReply:
        """经 openai SDK 发起一次请求，并把异常统一翻译为 LLMError。"""
        try:
            kwargs: dict[str, Any] = {
                "model": self._settings.effective_model,
                "messages": list(messages),
                "temperature": self._settings.temperature,
            }
            if tools:
                kwargs["tools"] = list(tools)
            resp = self._get_client().chat.completions.create(**kwargs)
            return parse_completion(resp)
        except LLMError:
            raise  # 已是统一异常，原样上抛
        except Exception as exc:
            raise classify_exception(exc) from exc
