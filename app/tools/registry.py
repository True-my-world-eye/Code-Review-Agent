"""工具注册表 —— 统一声明、校验、分发 Agent 工具。

设计要点（对应 Design.md 第 5 节）：
1. 每个工具是一个 ToolSpec：名称 + 描述 + JSON Schema + 处理函数，
   注册表据此自动生成发给 LLM 的 tools 参数；
2. 安全边界：所有工具的路径都必须落在审查根目录（ctx.root）内，
   路径穿越（../、绝对路径、符号链接逃逸）一律拒绝；
3. 执行永不抛异常：工具失败以 "Error: ..." 文本返回，由 Agent 回填给
   模型自行纠正 —— 这是 ReAct 循环稳定性的关键；
4. 结果统一截断，防止单个工具结果撑爆上下文。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# 单个工具结果返回给模型的最大字符数（超出截断，防止上下文爆炸）
MAX_RESULT_CHARS = 8000


class ToolError(Exception):
    """工具内部错误：消息为中文、可直接回填给模型。"""


@dataclass
class ToolContext:
    """工具执行所需的共享上下文（由 Agent/界面层构造后注入）。

    Attributes:
        root: 审查根目录 —— 所有相对路径的基准，也是安全边界
        tool_timeout: 子进程类工具的超时秒数
        auto_fix_enabled: 是否允许 apply_fix 执行写操作
        confirm_fn: 写操作确认回调 (路径, 变更详情) -> 是否执行；
                    未提供时安全默认为「拒绝」，由界面层负责提供
    """

    root: Path
    tool_timeout: int = 30
    auto_fix_enabled: bool = True
    confirm_fn: Callable[[str, dict[str, Any]], bool] | None = None


@dataclass
class ToolSpec:
    """单个工具的完整声明。"""

    name: str
    description: str
    # JSON Schema（OpenAI function parameters 格式）
    parameters: dict[str, Any]
    # 真正的执行体：参数字典 → 返回给模型的文本结果
    handler: Callable[[dict[str, Any], ToolContext], str]

    def to_schema(self) -> dict[str, Any]:
        """转换为 OpenAI 兼容的 tools 条目。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def resolve_in_root(ctx: ToolContext, raw_path: str) -> Path:
    """把用户/模型给出的路径解析为根目录内的真实路径。

    安全策略：解析后的绝对路径必须位于 root 之下，否则抛 ToolError。
    同时统一为 POSIX 风格字符串返回给调用方展示（相对 root）。
    """
    if not raw_path or not str(raw_path).strip():
        raise ToolError("路径不能为空")
    candidate = Path(raw_path)
    # 相对路径基于 root；绝对路径也先按 root 校验（绝对路径必须在 root 内）
    resolved = (ctx.root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    root_resolved = ctx.root.resolve()
    try:
        resolved.relative_to(root_resolved)  # 在 root 内则成功，否则抛 ValueError
    except ValueError as exc:
        raise ToolError(f"路径越界：{raw_path} 不在审查根目录内，已拒绝") from exc
    return resolved


def rel_display(ctx: ToolContext, path: Path) -> str:
    """把绝对路径转为相对审查根的展示形式（POSIX 分隔符，便于阅读）。"""
    try:
        return path.resolve().relative_to(ctx.root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def truncate_result(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    """超长结果截断：保头去尾并附提示，避免撑爆模型上下文。"""
    if len(text) <= limit:
        return text
    head = text[: limit - 200]
    return head + f"\n…（结果过长已截断，原始长度 {len(text)} 字符）"


@dataclass
class ToolRegistry:
    """工具注册表：声明、Schema 导出、统一分发执行。"""

    ctx: ToolContext
    _tools: dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        """注册一个工具（同名覆盖，便于测试替换实现）。"""
        self._tools[spec.name] = spec

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """导出全部工具的 OpenAI tools schema（发给 LLM 用）。"""
        return [spec.to_schema() for spec in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any] | None) -> str:
        """执行工具并返回文本结果。

        永不抛异常：
        - 未知工具 / 参数非法 / 工具内部异常 → "Error: ..." 文本，
          由 Agent 回填给模型，让模型有机会纠正后重试。
        """
        spec = self._tools.get(name)
        if spec is None:
            available = ", ".join(self.names()) or "(无)"
            return f"Error: 未知工具 {name!r}，可用工具：{available}"
        args = dict(arguments or {})
        # JSON Schema 之外的多余参数直接丢弃，防止模型幻觉参数进入 handler
        allowed = set(spec.parameters.get("properties", {}))
        args = {k: v for k, v in args.items() if k in allowed}
        try:
            result = spec.handler(args, self.ctx)
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # noqa: BLE001 —— 工具层兜底，绝不让循环崩溃
            return f"Error: 工具 {name} 执行异常（{type(exc).__name__}: {exc}）"
        return truncate_result(result)
