"""Web 服务端 —— FastAPI 路由、审查任务管理与静态页面托管。

API 一览（对应 Design.md 第 9.1 节）：
    GET  /                     → index.html
    GET  /css/*、/js/*         → 静态资源
    GET  /api/config           → 配置（Key 脱敏）+ 服务商列表
    PUT  /api/config           → 保存配置（api_key 为空表示不修改）
    POST /api/config/test      → LLM 连通性自检
    POST /api/review           → 发起审查，返回 task_id（后台线程执行）
    GET  /api/review/{id}/timeline → 轮询：状态 + 事件时间线 + 结果
    POST /api/fix              → 应用报告中的修复（按钮即确认）

安全边界：Web 审查目标必须位于项目根目录（ROOT）之内；
粘贴代码写入 sessions/paste/（已 gitignore）。
"""

from __future__ import annotations

import os
import string
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import ROOT, ConfigError, load_settings, provider_choices, save_settings
from app.core.agent import Agent, AgentEvent
from app.core.prompt import parse_review_report
from app.llm.client import LLMClient
from app.tools.builtin import build_default_registry
from app.tools.registry import ToolContext
from app.web.api_models import ConfigUpdate, FixRequest, ReviewRequest

app = FastAPI(title="Code Review Agent", version="1.0.0")

# 粘贴代码的落盘目录（sessions/ 已被 gitignore）
PASTE_DIR = ROOT / "sessions" / "paste"


# ================================================================ 审查任务
@dataclass
class ReviewTask:
    """一次 Web 审查的运行时状态（供前端轮询）。"""

    task_id: str
    target: str  # 展示用目标（相对路径或 文件名）
    root_rel: str = "."  # 审查根相对所选基准目录的路径（展示用）
    root_abs: Path = ROOT  # 审查根的绝对路径（修复接口据此定位）
    status: str = "running"  # running | done | error
    events: list[dict[str, Any]] = field(default_factory=list)
    content: str | None = None  # 最终原文
    report: dict[str, Any] | None = None  # 解析后的报告
    stats: dict[str, Any] | None = None  # 轮数 / 工具次数 / 是否截断
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def to_public(self) -> dict[str, Any]:
        """转为 JSON 安全的响应体。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "target": self.target,
                "root_rel": self.root_rel,
                "status": self.status,
                "events": list(self.events),
                "content": self.content,
                "report": self.report,
                "stats": self.stats,
                "error": self.error,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
            }


# 任务表：id → ReviewTask（单进程内存态，重启即清空）
REVIEW_TASKS: dict[str, ReviewTask] = {}
_TASKS_LOCK = threading.Lock()


def create_agent(settings, registry, on_event=None) -> Agent:
    """构造真实 Agent（测试时可 monkeypatch 本函数注入假 Agent）。"""
    return Agent(settings, registry, on_event=on_event)


def _event_to_dict(event: AgentEvent) -> dict[str, Any]:
    """AgentEvent → JSON 字典。"""
    return {"kind": event.kind, "text": event.text, "ts": event.ts, "data": event.data}


def _list_drives() -> list[str]:
    """列出可用的磁盘根（Windows 盘符 / 其他系统的 /），供目录选择器起步。"""
    if os.name == "nt":
        return [f"{d}:\\" for d in string.ascii_uppercase if Path(f"{d}:\\").exists()]
    return ["/"]


def _resolve_target(base: Path, path_str: str) -> tuple[Path, str]:
    """把相对路径解析为 (审查根目录, 相对目标)，目标必须位于 base 之内。

    Raises:
        HTTPException 400：路径不存在或越出所选根目录。
    """
    raw = Path(path_str)
    candidate = raw.resolve() if raw.is_absolute() else (base / raw).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="路径越界：目标不在所选根目录内"
        ) from exc
    if not candidate.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在：{path_str}")
    if candidate.is_dir():
        return candidate, "."
    return candidate.parent, candidate.name


def _run_review_task(
    task: ReviewTask,
    settings,
    root: Path,
    rel: str,
    ask: str | None,
) -> None:
    """后台线程：执行一次 Agent 审查并把事件写入任务状态。"""
    try:
        # Web 无法在循环中弹确认框：confirm_fn 留空（安全默认拒绝），
        # 用户改用报告卡片上的「应用修复」按钮（POST /api/fix）执行修复。
        ctx = ToolContext(
            root=root,
            tool_timeout=settings.tool_timeout,
            auto_fix_enabled=settings.auto_fix_enabled,
            confirm_fn=None,
        )
        registry = build_default_registry(ctx)

        def _on_event(event: AgentEvent) -> None:
            with task.lock:
                task.events.append(_event_to_dict(event))

        agent = create_agent(settings, registry, on_event=_on_event)
        user_input = f"请审查：{rel}"
        if ask:
            user_input += f"。补充要求：{ask}"
        result = agent.run(user_input)

        with task.lock:
            task.stats = {
                "iterations": result.iterations,
                "tool_calls": result.tool_calls,
                "truncated": result.truncated,
            }
            task.content = result.content
            task.report = parse_review_report(result.content)
            if result.ok:
                task.status = "done"
            else:
                task.status = "error"
                task.error = result.error
    except Exception as exc:  # noqa: BLE001 —— 后台线程兜底，状态转 error
        with task.lock:
            task.status = "error"
            task.error = f"{type(exc).__name__}: {exc}"
    finally:
        task.finished_at = time.time()


# ================================================================ 静态页面
@app.get("/", include_in_schema=False)
def index_page() -> FileResponse:
    """Web 单页入口。"""
    return FileResponse(ROOT / "index.html")


# /css 与 /js 静态资源（挂载点在路由注册前匹配，前缀独立不冲突）
app.mount("/css", StaticFiles(directory=ROOT / "css"), name="css")
app.mount("/js", StaticFiles(directory=ROOT / "js"), name="js")


# ================================================================ 配置
@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """读取配置（Key 脱敏）+ 服务商预设列表。"""
    settings = load_settings()
    return {
        "settings": settings.to_public_dict(),
        "providers": [{"id": pid, "label": label} for pid, label in provider_choices()],
    }


@app.put("/api/config")
def update_config(body: ConfigUpdate) -> dict[str, Any]:
    """保存配置。api_key 传空字符串表示保持原值不变。"""
    provided = body.model_dump(exclude_none=True)
    if not provided:
        raise HTTPException(status_code=400, detail="没有需要保存的配置项")
    overrides = {
        k: v
        for k, v in provided.items()
        if not (k == "api_key" and v == "")  # 空 Key = 不修改
    }
    try:
        if overrides:
            # env={}：只合并「文件 + 本次请求」，避免把环境变量烤进配置文件
            settings = load_settings(overrides=overrides, env={})
            save_settings(settings)
        else:
            settings = load_settings(env={})  # 全是空操作（如只传了空 Key）
    except ConfigError as exc:
        raise HTTPException(status_code=400, detail=f"配置无效：{exc}") from None
    return {"settings": settings.to_public_dict()}


@app.post("/api/config/test")
def test_config() -> dict[str, Any]:
    """LLM 连通性自检（测试时可 monkeypatch 模块级 LLMClient）。"""
    settings = load_settings()
    ok, message = LLMClient(settings).test_connection()
    return {"ok": ok, "message": message}


# ================================================================ 目录浏览
@app.get("/api/fs/list")
def fs_list(path: str = "") -> dict[str, Any]:
    """目录浏览：供前端「选择目录」弹窗导航。

    path 为空 → 返回盘符列表（导航起点）；否则返回该目录的子目录。
    服务仅监听 127.0.0.1（本地单用户工具），范围为当前用户可读目录。
    """
    drives = _list_drives()
    if not path:
        return {"path": "", "parent": None, "drives": drives, "dirs": []}
    target = Path(path).expanduser()
    if not target.exists():
        raise HTTPException(status_code=400, detail=f"路径不存在：{path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"不是目录：{path}")
    try:
        dirs = sorted(
            (e.name for e in target.iterdir() if e.is_dir()),
            key=str.lower,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"无权限读取：{path}") from None
    parent = str(target.parent) if target.parent != target else None
    return {"path": str(target), "parent": parent, "drives": drives, "dirs": dirs}


# ================================================================ 审查任务
@app.post("/api/review")
def start_review(body: ReviewRequest) -> dict[str, str]:
    """发起审查：完成入参校验后交给后台线程执行，立即返回 task_id。"""
    settings = load_settings()
    if not settings.is_configured:
        raise HTTPException(
            status_code=400,
            detail="LLM 配置不完整：请先在设置中填写服务商与 API Key",
        )

    task_id = uuid.uuid4().hex[:12]

    # 审查根：默认项目目录；前端「选择目录」可指定任意本地目录
    # （代码不必位于本项目之下，这正是目录选择器存在的意义）
    if body.root:
        base = Path(body.root).expanduser().resolve()
        if not base.is_dir():
            raise HTTPException(
                status_code=400, detail=f"根目录不存在或不是目录：{body.root}"
            )
    else:
        base = ROOT.resolve()

    # 粘贴模式：代码落盘到 sessions/paste/<task_id>/ 后按普通文件审查
    if body.code is not None and body.code.strip():
        file_name = (body.file_name or "snippet.py").strip()
        # 防目录穿越：文件名只保留基名
        file_name = Path(file_name).name or "snippet.py"
        paste_root = PASTE_DIR / task_id
        paste_root.mkdir(parents=True, exist_ok=True)
        (paste_root / file_name).write_text(body.code, encoding="utf-8")
        root, rel = paste_root, file_name
        target_display = f"粘贴代码 → {file_name}"
    else:
        root, rel = _resolve_target(base, body.path)
        target_display = str(root) if rel == "." else str(root / rel)

    task = ReviewTask(task_id=task_id, target=target_display)
    task.root_abs = root
    # root_rel：审查根相对基准目录的展示路径（前端展示 / 修复定位兜底）
    try:
        task.root_rel = root.resolve().relative_to(base).as_posix()
    except ValueError:
        task.root_rel = "."
    with _TASKS_LOCK:
        REVIEW_TASKS[task_id] = task

    thread = threading.Thread(
        target=_run_review_task,
        args=(task, settings, root, rel, body.ask),
        daemon=True,
    )
    thread.start()
    return {"task_id": task_id, "target": target_display}


@app.get("/api/review/{task_id}/timeline")
def review_timeline(task_id: str) -> dict[str, Any]:
    """轮询审查状态：事件时间线与最终结果。"""
    with _TASKS_LOCK:
        task = REVIEW_TASKS.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    return task.to_public()


# ================================================================ 应用修复
@app.post("/api/fix")
def apply_fix(body: FixRequest) -> dict[str, Any]:
    """执行报告卡片上的修复（点击按钮即视为用户确认）。

    优先用 task_id 定位该次审查的根目录（支持项目外的任意目录），
    无 task_id 时回退到项目根目录。
    """
    settings = load_settings()
    if not settings.auto_fix_enabled:
        raise HTTPException(status_code=400, detail="自动修复已在配置中禁用")

    root = ROOT
    if body.task_id:
        with _TASKS_LOCK:
            task = REVIEW_TASKS.get(body.task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在或已过期")
        root = task.root_abs

    # 「用户点击了按钮」就是确认回调
    ctx = ToolContext(root=root, tool_timeout=settings.tool_timeout,
                      auto_fix_enabled=True, confirm_fn=lambda p, d: True)
    registry = build_default_registry(ctx)
    message = registry.execute(
        "apply_fix",
        {"path": body.path, "old_code": body.old_code, "new_code": body.new_code},
    )
    ok = not message.startswith("Error:")
    return {"ok": ok, "message": message}
