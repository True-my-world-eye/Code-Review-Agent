"""工具层单元测试（全部离线：临时目录 + 真实 ruff 子进程）。

覆盖点（对应 docs/测试文档.md · 2.3）：
- 路径安全：目录穿越 / 越界绝对路径一律拒绝
- list_dir：层级展示与忽略目录
- read_file：普通读取、区间读取、超长分段、二进制与编码
- search_code：命中格式、非法正则、忽略目录、结果上限
- run_lint：真实执行 ruff（有问题 / 干净两种文件）
- apply_fix：成功+备份、非唯一拒绝、无匹配拒绝、确认取消、开关禁用
- 注册表：未知工具、处理器异常兜底、Schema 格式、多余参数丢弃、结果截断
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.tools.builtin import build_default_registry
from app.tools.registry import (
    MAX_RESULT_CHARS,
    ToolContext,
    ToolRegistry,
    ToolSpec,
)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    """构造一棵小型测试目录树（root 之外还放一个「越界」诱饵文件）。"""
    root_dir = tmp_path / "root"
    root_dir.mkdir()
    (root_dir / "app.py").write_text(
        "import os\n"
        "import sys\n"
        "\n"
        "def main():\n"
        "    print('hi')\n"
        "    eval('1+1')\n"
        "    return 0\n"
        "\n"
        "# TODO: 修复边界条件\n",
        encoding="utf-8",
    )
    src = root_dir / "src"
    src.mkdir()
    (src / "util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # 应被忽略的依赖目录
    dep = root_dir / "node_modules"
    dep.mkdir()
    (dep / "dep.js").write_text("junk code", encoding="utf-8")
    # 500 行超长文件
    (root_dir / "large.txt").write_text(
        "\n".join(f"line {i}" for i in range(1, 501)), encoding="utf-8"
    )
    # 二进制文件
    (root_dir / "binary.bin").write_bytes(b"\x00\x01\x02\x03")
    # GBK 中文文件
    (root_dir / "gbk.txt").write_bytes("中文内容测试".encode("gbk"))
    # root 之外的诱饵（用于越界测试）
    (tmp_path / "outside_secret.txt").write_text("top secret", encoding="utf-8")
    return root_dir


@pytest.fixture()
def registry(root: Path) -> ToolRegistry:
    """默认注册表：确认回调 = 永远同意（成功路径用）。"""
    return build_default_registry(
        ToolContext(root=root, tool_timeout=30, confirm_fn=lambda p, d: True)
    )


# ================================================================ 路径安全
def test_path_traversal_rejected(registry: ToolRegistry) -> None:
    """../ 越出审查根目录必须被拒绝。"""
    result = registry.execute("read_file", {"path": "../outside_secret.txt"})
    assert result.startswith("Error:")
    assert "越界" in result
    assert "top secret" not in result


def test_absolute_outside_path_rejected(registry: ToolRegistry, root: Path) -> None:
    """root 之外的绝对路径同样拒绝。"""
    outside = str(root.parent / "outside_secret.txt")
    result = registry.execute("read_file", {"path": outside})
    assert result.startswith("Error:")
    assert "越界" in result


# ================================================================ list_dir
def test_list_dir_flat(registry: ToolRegistry) -> None:
    """默认一层结构：可见源码与子目录，忽略 node_modules 与隐藏目录。"""
    result = registry.execute("list_dir", {"path": "."})
    assert "app.py" in result and "src/" in result
    assert "node_modules" not in result  # 依赖目录被忽略
    assert "util.py" not in result  # 非递归时不展示二层文件


def test_list_dir_recursive(registry: ToolRegistry) -> None:
    """recursive=true 展开子目录文件。"""
    result = registry.execute("list_dir", {"path": ".", "recursive": True})
    assert "util.py" in result


def test_list_dir_missing(registry: ToolRegistry) -> None:
    """不存在的目录 → Error 文本。"""
    result = registry.execute("list_dir", {"path": "nope"})
    assert result.startswith("Error:") and "不存在" in result


def test_list_dir_on_file(registry: ToolRegistry) -> None:
    """对文件调用 list_dir → 明确提示改用 read_file。"""
    result = registry.execute("list_dir", {"path": "app.py"})
    assert result.startswith("Error:") and "read_file" in result


# ================================================================ read_file
def test_read_file_basic(registry: ToolRegistry) -> None:
    """普通读取：返回头部信息与文件内容。"""
    result = registry.execute("read_file", {"path": "app.py"})
    assert "共 9 行" in result
    assert "def main():" in result
    assert "eval('1+1')" in result


def test_read_file_range(registry: ToolRegistry) -> None:
    """区间读取只返回指定行。"""
    result = registry.execute(
        "read_file", {"path": "large.txt", "start": 401, "end": 405}
    )
    assert "line 401" in result and "line 405" in result
    assert "line 1\n" not in result and "line 410" not in result


def test_read_file_overlong_segments(registry: ToolRegistry) -> None:
    """500 行文件无区间读取：强制只读前 400 行并提示继续读取。"""
    result = registry.execute("read_file", {"path": "large.txt"})
    assert "显示第 1-400 行" in result
    assert "start=401" in result  # 分段引导
    assert "line 450" not in result  # 超出部分未返回


def test_read_file_missing(registry: ToolRegistry) -> None:
    result = registry.execute("read_file", {"path": "ghost.py"})
    assert result.startswith("Error:") and "不存在" in result


def test_read_file_binary(registry: ToolRegistry) -> None:
    result = registry.execute("read_file", {"path": "binary.bin"})
    assert result.startswith("Error:") and "二进制" in result


def test_read_file_directory_hint(registry: ToolRegistry) -> None:
    """传入目录 → 提示改用 list_dir，而不是抛底层异常。"""
    result = registry.execute("read_file", {"path": "src"})
    assert result.startswith("Error:") and "list_dir" in result


def test_read_file_gbk_fallback(registry: ToolRegistry) -> None:
    """UTF-8 解码失败时回退 GBK。"""
    result = registry.execute("read_file", {"path": "gbk.txt"})
    assert "中文内容测试" in result


def test_read_file_bad_range(registry: ToolRegistry) -> None:
    """end < start → 参数错误。"""
    result = registry.execute(
        "read_file", {"path": "app.py", "start": 5, "end": 2}
    )
    assert result.startswith("Error:") and "end" in result


# ================================================================ search_code
def test_search_hit(registry: ToolRegistry) -> None:
    """命中格式为 路径:行号: 内容。"""
    result = registry.execute("search_code", {"pattern": "eval\\("})
    assert "命中" in result
    assert "app.py:6:" in result


def test_search_invalid_regex(registry: ToolRegistry) -> None:
    result = registry.execute("search_code", {"pattern": "[unclosed"})
    assert result.startswith("Error:") and "正则" in result


def test_search_no_hit_and_ignore_dep(registry: ToolRegistry) -> None:
    """无命中；且 node_modules 内容不参与搜索。"""
    result = registry.execute("search_code", {"pattern": "junk"})
    assert "未找到" in result


def test_search_result_cap(registry: ToolRegistry, root: Path) -> None:
    """命中 150 处时结果截断在 100。"""
    (root / "many.txt").write_text(
        "\n".join(["FIXME here"] * 150), encoding="utf-8"
    )
    result = registry.execute("search_code", {"pattern": "FIXME"})
    assert "已达上限" in result
    assert result.count("FIXME") == 100


# ================================================================ run_lint
def test_run_lint_finds_issues(registry: ToolRegistry) -> None:
    """真实执行 ruff：app.py 存在未使用 import → F401。"""
    result = registry.execute("run_lint", {"path": "app.py"})
    assert "ruff 共发现" in result
    assert "[F401]" in result
    assert "app.py:1:" in result  # 行列号定位


def test_run_lint_clean_file(registry: ToolRegistry) -> None:
    """干净文件 → 检查通过。"""
    result = registry.execute("run_lint", {"path": "src/util.py"})
    assert "未发现问题" in result


def test_run_lint_missing(registry: ToolRegistry) -> None:
    result = registry.execute("run_lint", {"path": "ghost.py"})
    assert result.startswith("Error:") and "不存在" in result


# ================================================================ apply_fix
def test_apply_fix_success(registry: ToolRegistry, root: Path) -> None:
    """成功修复：内容更新 + 生成 .bak 备份 + 明确回执。"""
    result = registry.execute("apply_fix", {
        "path": "src/util.py",
        "old_code": "return a + b",
        "new_code": "return a + b  # 修复完成",
    })
    assert "已修复" in result and "备份" in result
    content = (root / "src" / "util.py").read_text(encoding="utf-8")
    assert "# 修复完成" in content
    backup = root / "src" / "util.py.bak"
    assert backup.exists()
    assert "return a + b" in backup.read_text(encoding="utf-8")  # 备份是修复前状态


def test_apply_fix_not_unique(registry: ToolRegistry) -> None:
    """old_code 出现多次 → 拒绝（防止误改）。"""
    result = registry.execute("apply_fix", {
        "path": "app.py",
        "old_code": "import",  # 出现 2 次
        "new_code": "from __future__ import annotations",
    })
    assert result.startswith("Error:") and "2 次" in result


def test_apply_fix_no_match(registry: ToolRegistry) -> None:
    result = registry.execute("apply_fix", {
        "path": "app.py",
        "old_code": "this code does not exist",
        "new_code": "x",
    })
    assert result.startswith("Error:") and "未找到" in result


def test_apply_fix_confirm_denied(root: Path) -> None:
    """用户拒绝确认 → 返回取消回执，文件保持原状。"""
    registry = build_default_registry(
        ToolContext(root=root, confirm_fn=lambda p, d: False)
    )
    before = (root / "app.py").read_text(encoding="utf-8")
    result = registry.execute("apply_fix", {
        "path": "app.py",
        "old_code": "print('hi')",
        "new_code": "print('hello')",
    })
    assert "未确认" in result
    assert (root / "app.py").read_text(encoding="utf-8") == before


def test_apply_fix_without_confirm_fn(root: Path) -> None:
    """未注入确认回调 → 安全默认拒绝。"""
    registry = build_default_registry(ToolContext(root=root))  # confirm_fn=None
    result = registry.execute("apply_fix", {
        "path": "app.py",
        "old_code": "print('hi')",
        "new_code": "print('hello')",
    })
    assert "未确认" in result


def test_apply_fix_disabled(root: Path) -> None:
    """配置开关关闭 → 直接拒绝。"""
    registry = build_default_registry(
        ToolContext(root=root, auto_fix_enabled=False, confirm_fn=lambda p, d: True)
    )
    result = registry.execute("apply_fix", {
        "path": "app.py",
        "old_code": "print('hi')",
        "new_code": "print('hello')",
    })
    assert result.startswith("Error:") and "禁用" in result


def test_apply_fix_crlf_file(root: Path) -> None:
    """CRLF 文件 + 模型给出的 LF 风格代码：换行对齐后修复成功且不翻倍换行。"""
    target = root / "win.py"
    target.write_bytes(b"a = 1\r\nb = 2\r\n")  # Windows 风格换行
    registry = build_default_registry(
        ToolContext(root=root, confirm_fn=lambda p, d: True)
    )
    result = registry.execute("apply_fix", {
        "path": "win.py",
        "old_code": "a = 1\nb = 2",   # 模型 JSON 里通常是 LF
        "new_code": "a = 10\nb = 20",
    })
    assert "已修复" in result
    fixed = (root / "win.py").read_bytes()
    assert fixed == b"a = 10\r\nb = 20\r\n"  # 仍是单个 \r\n，没有 \r\r\n
    backup = (root / "win.py.bak").read_bytes()
    assert backup == b"a = 1\r\nb = 2\r\n"  # 备份保持修复前状态


# ================================================================ 注册表
def test_unknown_tool(registry: ToolRegistry) -> None:
    result = registry.execute("launch_missiles", {})
    assert result.startswith("Error:") and "未知工具" in result
    assert "list_dir" in result  # 提示可用工具


def test_handler_exception_captured(root: Path) -> None:
    """handler 抛出任意异常 → 转为 Error 文本，不向上冒泡。"""
    registry = ToolRegistry(ctx=ToolContext(root=root))

    def boom(args, ctx):
        raise ValueError("内部爆炸")

    registry.register(ToolSpec("boom", "d", {"type": "object", "properties": {}}, boom))
    result = registry.execute("boom", {})
    assert result.startswith("Error:") and "内部爆炸" in result


def test_schema_format(registry: ToolRegistry) -> None:
    """导出的 schema 必须是 OpenAI function 格式。"""
    schemas = registry.schemas()
    assert len(schemas) == 5
    first = schemas[0]
    assert first["type"] == "function"
    fn = first["function"]
    assert fn["name"] == "list_dir"
    assert "properties" in fn["parameters"]
    names = {s["function"]["name"] for s in schemas}
    assert names == {"list_dir", "read_file", "search_code", "run_lint", "apply_fix"}


def test_extra_arguments_dropped(root: Path) -> None:
    """模型幻觉出的多余参数不应传入 handler。"""
    registry = ToolRegistry(ctx=ToolContext(root=root))
    seen: dict = {}

    def handler(args, ctx):
        seen.update(args)
        return "ok"

    registry.register(ToolSpec(
        "echo", "d",
        {"type": "object", "properties": {"a": {"type": "integer"}}, "required": []},
        handler,
    ))
    registry.execute("echo", {"a": 1, "evil": "x"})
    assert seen == {"a": 1}


def test_result_truncated(root: Path) -> None:
    """超长结果被截断，防止撑爆上下文。"""
    registry = ToolRegistry(ctx=ToolContext(root=root))
    registry.register(ToolSpec(
        "huge", "d", {"type": "object", "properties": {}},
        lambda args, ctx: "x" * (MAX_RESULT_CHARS * 2),
    ))
    result = registry.execute("huge", {})
    assert len(result) < MAX_RESULT_CHARS + 200
    assert "截断" in result
