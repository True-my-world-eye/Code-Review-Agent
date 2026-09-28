"""上下文记忆 —— 会话消息历史、截断策略与持久化。

设计要点（对应 Design.md 第 7 节）：
1. 消息按 OpenAI 格式保存（system / user / assistant / tool），
   可直接传给 LLMClient.chat()；
2. 截断策略：超过 max_messages 时丢弃最旧的消息前缀，且切分点
   必须落在 user 消息边界上 —— 保证「assistant(tool_calls) + tool」
   的配对永不被拆散（否则 API 会报错）；system 永不丢弃；
3. 会话可序列化为 JSON 存入 sessions/，CLI 与 Web 均可恢复。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import ROOT
from app.llm.client import LLMReply

# 默认消息容量（含 system）：设计文档约定保留最近 20 条
DEFAULT_MAX_MESSAGES = 20
# 会话持久化目录
SESSIONS_DIR = ROOT / "sessions"


class SessionMemory:
    """单个会话的消息历史。"""

    def __init__(
        self,
        system_prompt: str,
        max_messages: int = DEFAULT_MAX_MESSAGES,
        session_id: str | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.max_messages = max(3, max_messages)  # 至少容纳 system + user + assistant
        self.created_at = time.time()
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    # ---------------- 写入 ----------------
    def add_user(self, content: str) -> None:
        """追加一条用户消息。"""
        self.messages.append({"role": "user", "content": content})
        self._truncate()

    def add_assistant(self, reply: LLMReply) -> None:
        """追加一条助手回复（纯文本，或带 tool_calls 的调用请求）。

        注意：带 tool_calls 时 arguments 必须序列化回 JSON 字符串，
        这是 OpenAI 消息格式的硬性要求。
        """
        msg: dict[str, Any] = {"role": "assistant", "content": reply.content}
        if reply.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        # 保证即使模型给出的是 dict 也转成字符串
                        "arguments": json.dumps(
                            tc.arguments, ensure_ascii=False
                        ),
                    },
                }
                for tc in reply.tool_calls
            ]
        self.messages.append(msg)
        self._truncate()

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        """追加一条工具结果消息（按 tool_call_id 关联回助手请求）。"""
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )
        self._truncate()

    # ---------------- 截断 ----------------
    def _truncate(self) -> None:
        """超容量时丢弃最旧的消息前缀（system 保留，切分点对齐 user 边界）。

        安全约束：
        1. 切分点必须是 user 消息 —— 保证 assistant(tool_calls) 与其
           tool 结果成对保留，否则 OpenAI 接口会报消息配对错误；
        2. 若尾部找不到 user（正处于一轮工具调用中间），放弃本次截断，
           宁可临时超容量也不能拆散配对。
        """
        if len(self.messages) <= self.max_messages:
            return
        # 粗切分点：容量含 system，因此后缀预算 = max_messages - 1
        start = max(1, len(self.messages) - (self.max_messages - 1))
        cut: int | None = None
        for i in range(start, len(self.messages)):
            if self.messages[i]["role"] == "user":
                cut = i
                break
        if cut is None or cut <= 1:
            # cut<=1 表示切完等于没切（system 之后全保留），直接返回
            return  # 没有安全切分点：保持原样（system 在 index 0 恒保留）
        # messages[0] 是 system，恒保留；cut ≥ 1 保证前缀不含 system
        self.messages = [self.messages[0]] + self.messages[cut:]

    # ---------------- 持久化 ----------------
    def to_dict(self) -> dict[str, Any]:
        """序列化为可存 JSON 的字典。"""
        return {
            "session_id": self.session_id,
            "max_messages": self.max_messages,
            "created_at": self.created_at,
            "messages": self.messages,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionMemory:
        """从字典恢复会话（用于 CLI/Web 继续历史会话）。"""
        mem = cls(
            system_prompt="",
            max_messages=int(data.get("max_messages", DEFAULT_MAX_MESSAGES)),
            session_id=str(data.get("session_id") or ""),
        )
        mem.created_at = float(data.get("created_at", time.time()))
        msgs = data.get("messages")
        if isinstance(msgs, list) and msgs:
            # 恢复时以存档中的 system 为准（提示词可能已随版本更新）
            mem.messages = msgs
        return mem

    def save(self, sessions_dir: Path | None = None) -> Path:
        """把会话写入 sessions/<id>.json。"""
        directory = sessions_dir or SESSIONS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.session_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    @classmethod
    def load(cls, session_id: str, sessions_dir: Path | None = None) -> SessionMemory:
        """按 session_id 恢复会话；不存在则抛 FileNotFoundError。"""
        directory = sessions_dir or SESSIONS_DIR
        path = directory / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)


def list_sessions(sessions_dir: Path | None = None) -> list[dict[str, Any]]:
    """列出全部已保存会话的摘要（按修改时间倒序）。"""
    directory = sessions_dir or SESSIONS_DIR
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # 损坏的会话文件跳过，不影响其它会话
        messages = data.get("messages") or []
        # 摘要取最后一条 user 消息的前 80 字
        preview = ""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                preview = str(msg.get("content", ""))[:80]
                break
        out.append(
            {
                "session_id": data.get("session_id", path.stem),
                "created_at": data.get("created_at", 0),
                "message_count": len(messages),
                "preview": preview,
            }
        )
    return out
