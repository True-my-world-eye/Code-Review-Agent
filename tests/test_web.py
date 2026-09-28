"""Web 层测试（fastapi TestClient 离线驱动，不发真实 LLM 请求）。

覆盖点（对应 docs/测试文档.md · 3.4）：
- 静态页面与资源托管
- 配置读写、Key 脱敏、连通性自检（假 LLMClient）
- 审查任务全链路：入参校验 / 越界拦截 / 后台执行 / 轮询终态
- 粘贴模式落盘与文件名消毒
- 修复接口：成功、失败回执、开关禁用、路径越界
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.config as config_mod
import app.web.server as server_mod
from app.core.agent import Agent
from app.llm.client import LLMReply, ToolCall

client = TestClient(server_mod.app)

# 含 1 个 error 且附带 fix 字段的标准报告
REPORT_JSON = (
    '```json\n{"summary": "发现 1 个严重问题", "issues": ['
    '{"severity": "error", "file": "app.py", "line": 1,'
    ' "message": "变量未使用", "suggestion": "删除该行",'
    ' "fix": {"old_code": "x = 1", "new_code": "x = 2"}}]}\n```'
)


class FakeLLM:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        return self._replies.pop(0)


# ---------------------------------------------------------------- 夹具
@pytest.fixture()
def web_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置文件隔离到临时目录。"""
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    return path


def write_config(path: Path, extra: str = "") -> None:
    path.write_text(
        "provider: deepseek\napi_key: sk-test-key\n" + extra, encoding="utf-8"
    )


@pytest.fixture()
def web_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把服务端的项目根隔离到临时目录（静态文件测试除外）。"""
    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(server_mod, "ROOT", root)
    return root


def make_fake_agent(replies):
    """构造一个绑定假 LLM 的 create_agent 替身。"""

    def factory(settings, registry, on_event=None):
        return Agent(
            settings, registry, llm=FakeLLM(replies), on_event=on_event
        )

    return factory


def wait_task(task_id: str, timeout: float = 5.0) -> dict:
    """轮询任务直到终态（测试专用）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/review/{task_id}/timeline")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] in {"done", "error"}:
            return data
        time.sleep(0.05)
    pytest.fail(f"任务 {timeout}s 内未完成")


# ================================================================ 静态页面
def test_index_served() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Code Review Agent" in resp.text
    assert "js/app.js" in resp.text


def test_static_assets_served() -> None:
    css = client.get("/css/style.css")
    assert css.status_code == 200 and "--accent" in css.text
    js = client.get("/js/app.js")
    assert js.status_code == 200 and "startReview" in js.text


# ================================================================ 配置
def test_get_config_masked(web_config: Path) -> None:
    write_config(web_config)
    data = client.get("/api/config").json()
    assert data["settings"]["api_key"] != "sk-test-key"  # 脱敏
    assert "***" in data["settings"]["api_key"]
    assert len(data["providers"]) == 3
    assert {p["id"] for p in data["providers"]} == {"deepseek", "mimo", "openai-compatible"}


def test_put_config_saves(web_config: Path) -> None:
    resp = client.put("/api/config", json={"provider": "mimo"})
    assert resp.status_code == 200
    assert resp.json()["settings"]["provider"] == "mimo"
    assert "provider: mimo" in web_config.read_text(encoding="utf-8")


def test_put_config_empty_key_keeps_old(web_config: Path) -> None:
    write_config(web_config)
    resp = client.put("/api/config", json={"api_key": ""})
    assert resp.status_code == 200
    # 空 Key 不覆盖：脱敏输出仍应是原 Key 的脱敏形态
    assert "***" in resp.json()["settings"]["api_key"]


def test_put_config_invalid_provider(web_config: Path) -> None:
    resp = client.put("/api/config", json={"provider": "nope"})
    assert resp.status_code == 400
    assert "配置无效" in resp.json()["detail"]


def test_config_test_endpoint(
    web_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(web_config)

    class FakeClient:
        def __init__(self, settings):
            pass

        def test_connection(self):
            return True, "连接成功 · model=fake"

    monkeypatch.setattr(server_mod, "LLMClient", FakeClient)
    data = client.post("/api/config/test").json()
    assert data["ok"] is True and "fake" in data["message"]


# ================================================================ 审查任务
def test_review_unconfigured(web_config: Path) -> None:
    """无 Key → 400 + 中文提示。"""
    resp = client.post("/api/review", json={"path": "."})
    assert resp.status_code == 400
    assert "配置不完整" in resp.json()["detail"]


def test_review_path_escape(
    web_config: Path, web_root: Path
) -> None:
    write_config(web_config)
    resp = client.post("/api/review", json={"path": "../etc"})
    assert resp.status_code == 400
    assert "越界" in resp.json()["detail"]


def test_review_missing_path(web_config: Path, web_root: Path) -> None:
    write_config(web_config)
    resp = client.post("/api/review", json={"path": "ghost-dir"})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_review_flow_offline(
    web_config: Path, web_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全链路：POST → 后台线程 → 轮询到 done → 事件/报告/统计齐全。"""
    write_config(web_config)
    (web_root / "sample.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        server_mod,
        "create_agent",
        make_fake_agent(
            [
                LLMReply(
                    content=None,
                    tool_calls=[ToolCall(id="c1", name="read_file",
                                          arguments={"path": "sample.py"})],
                    finish_reason="tool_calls",
                ),
                LLMReply(content=REPORT_JSON, model="fake", finish_reason="stop"),
            ]
        ),
    )

    resp = client.post("/api/review", json={"path": ".", "ask": "看安全"})
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]

    data = wait_task(task_id)
    assert data["status"] == "done"
    assert data["root_rel"] == "."
    kinds = [e["kind"] for e in data["events"]]
    assert "tool" in kinds and "final" in kinds
    assert data["report"]["parsed"] is True
    assert data["report"]["issues"][0]["message"] == "变量未使用"
    assert data["stats"]["tool_calls"] == 1
    assert data["stats"]["iterations"] == 2


def test_review_root_rel_nested(
    web_config: Path, web_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子目录审查：root_rel 反映相对路径（前端修复拼接依赖它）。"""
    write_config(web_config)
    (web_root / "src").mkdir()
    (web_root / "src" / "app.py").write_text("y = 2\n", encoding="utf-8")
    monkeypatch.setattr(
        server_mod,
        "create_agent",
        make_fake_agent([LLMReply(content=REPORT_JSON, finish_reason="stop")]),
    )
    task_id = client.post("/api/review", json={"path": "src"}).json()["task_id"]
    data = wait_task(task_id)
    assert data["status"] == "done"
    assert data["root_rel"] == "src"


def test_review_paste_code(
    web_config: Path,
    web_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """粘贴模式：文件名消毒（去掉目录穿越）、落盘到隔离目录。"""
    write_config(web_config)
    # 放在 web_root 内的 sessions/paste 下，与生产布局一致（root_rel 可正确计算）
    paste_dir = web_root / "sessions" / "paste"
    monkeypatch.setattr(server_mod, "PASTE_DIR", paste_dir)
    monkeypatch.setattr(
        server_mod,
        "create_agent",
        make_fake_agent([LLMReply(content=REPORT_JSON, finish_reason="stop")]),
    )

    resp = client.post(
        "/api/review",
        json={"code": "x = 1\n", "file_name": "../evil.py"},
    )
    assert resp.status_code == 200
    task_id = resp.json()["task_id"]
    # 文件名已消毒为基名 evil.py，写入 paste/<id>/
    written = list(paste_dir.glob("*/evil.py"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == "x = 1\n"

    data = wait_task(task_id)
    assert data["status"] == "done"
    assert "evil.py" in data["target"]
    assert data["root_rel"].startswith("sessions/paste/")


def test_timeline_unknown_task() -> None:
    resp = client.get("/api/review/does-not-exist/timeline")
    assert resp.status_code == 404


# ================================================================ 修复接口
def test_fix_success(web_config: Path, web_root: Path) -> None:
    write_config(web_config)
    (web_root / "app.py").write_text("x = 1\n", encoding="utf-8")
    resp = client.post(
        "/api/fix",
        json={"path": "app.py", "old_code": "x = 1", "new_code": "x = 2"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True and "已修复" in data["message"]
    assert (web_root / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    assert (web_root / "app.py.bak").exists()  # 自动备份


def test_fix_no_match(web_config: Path, web_root: Path) -> None:
    write_config(web_config)
    (web_root / "app.py").write_text("x = 1\n", encoding="utf-8")
    resp = client.post(
        "/api/fix",
        json={"path": "app.py", "old_code": "不存在的代码", "new_code": "y"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "Error" in resp.json()["message"]


def test_fix_path_escape(web_config: Path, web_root: Path) -> None:
    write_config(web_config)
    resp = client.post(
        "/api/fix",
        json={"path": "../secret.txt", "old_code": "a", "new_code": "b"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "越界" in resp.json()["message"]


def test_fix_disabled(web_config: Path, web_root: Path) -> None:
    write_config(web_config, extra="auto_fix_enabled: false\n")
    resp = client.post(
        "/api/fix",
        json={"path": "app.py", "old_code": "a", "new_code": "b"},
    )
    assert resp.status_code == 400
    assert "禁用" in resp.json()["detail"]
