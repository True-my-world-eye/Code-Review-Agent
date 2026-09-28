"""内置工具实现 —— list_dir / read_file / search_code / run_lint / apply_fix。

约定：
- handler 签名统一为 (args, ctx) -> str；
- 业务失败抛 ToolError（中文消息），由注册表转成 "Error: ..." 回填给模型；
- 路径一律经 resolve_in_root 校验，杜绝目录穿越。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.tools.registry import (
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolSpec,
    rel_display,
    resolve_in_root,
)

# 读取工具单次最大返回行数：超过则要求模型分段读取
READ_MAX_LINES = 400
# list_dir / search 的结果规模上限，防止超大仓库撑爆上下文
LIST_MAX_ENTRIES = 500
SEARCH_MAX_HITS = 100
# search 单行展示的最大字符数
SEARCH_LINE_LIMIT = 200

# 目录遍历时忽略的名称（版本库 / 依赖 / 缓存 / 构建产物）
IGNORED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build",
    "sessions", ".idea", ".vscode", "target", ".cache",
}

# 常见文本扩展名：list_dir 默认只展示这些 + 无扩展名文件
TEXT_SUFFIXES = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh", ".bat",
    ".html", ".css", ".scss", ".vue", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".txt", ".md", ".rst", ".sql", ".xml", ".csv", ".env",
}


# ---------------------------------------------------------------- 通用辅助
def _read_text(path: Path) -> str:
    """读取文本文件：utf-8 优先，失败回退 gbk（Windows 中文环境常见），再失败报错。"""
    data = path.read_bytes()
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ToolError(f"无法识别 {path.name} 的文本编码（已尝试 utf-8 / gbk）")


def _looks_binary(data: bytes) -> bool:
    """粗略二进制探测：前 8KB 含 NUL 字节即视为二进制。"""
    return b"\x00" in data[:8192]


def _iter_files(root: Path):
    """递归遍历 root 下的可读文本文件（跳过忽略目录与二进制文件）。"""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or entry.name in IGNORED_DIRS:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.is_file():
                try:
                    if _looks_binary(entry.read_bytes()):
                        continue
                except OSError:
                    continue
                yield entry


# ---------------------------------------------------------------- 1. list_dir
def _list_dir(args: dict[str, Any], ctx: ToolContext) -> str:
    """列出目录结构（默认仅一层；recursive=True 时最多 3 层）。"""
    target = resolve_in_root(ctx, str(args.get("path", ".")))
    if not target.exists():
        raise ToolError(f"目录不存在：{args.get('path')}")
    if not target.is_dir():
        raise ToolError(f"{args.get('path')} 不是目录，请改用 read_file")

    recursive = bool(args.get("recursive", False))
    # 要展示的条目层级数：非递归 = 仅第 1 层；递归 = 最多 3 层
    max_levels = 3 if recursive else 1
    lines: list[str] = []
    stack: list[tuple[Path, int]] = [(target, 0)]
    while stack:
        directory, depth = stack.pop()
        if depth >= max_levels:
            continue  # 超出展示层级：目录名已在上一层列出，不再下探
        try:
            entries = sorted(
                (p for p in directory.iterdir()
                 if not p.name.startswith(".") and p.name not in IGNORED_DIRS),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except OSError as exc:
            lines.append(f"  （无法读取 {directory.name}: {exc}）")
            continue
        for entry in entries:
            if len(lines) >= LIST_MAX_ENTRIES:
                lines.append("…（条目过多已截断）")
                return "\n".join(lines)
            indent = "  " * depth
            if entry.is_dir():
                lines.append(f"{indent}{entry.name}/")
                stack.append((entry, depth + 1))  # 是否真正展开由循环头部判断
            else:
                lines.append(f"{indent}{entry.name}")
    if not lines:
        return "（空目录）"
    shown = f"（目录 {rel_display(ctx, target)}，共 {len(lines)} 项）\n"
    return shown + "\n".join(lines)


# ---------------------------------------------------------------- 2. read_file
def _read_file(args: dict[str, Any], ctx: ToolContext) -> str:
    """读取文本文件（支持行号区间；超长文件强制分段）。"""
    raw_path = str(args.get("path", ""))
    target = resolve_in_root(ctx, raw_path)
    if not target.exists():
        raise ToolError(f"文件不存在：{raw_path}")
    if target.is_dir():
        raise ToolError(f"{raw_path} 是目录，请使用 list_dir 查看结构")
    if _looks_binary(target.read_bytes()):
        raise ToolError(f"{raw_path} 是二进制文件，无法按文本读取")
    content = _read_text(target)
    all_lines = content.splitlines()
    total = len(all_lines)

    start = args.get("start")
    end = args.get("end")
    try:
        start_i = int(start) if start is not None else 1
        end_i = int(end) if end is not None else start_i + READ_MAX_LINES - 1
    except (TypeError, ValueError) as exc:
        raise ToolError("start/end 必须是整数行号") from exc
    if start_i < 1:
        raise ToolError("行号从 1 开始")
    if end_i < start_i:
        raise ToolError("end 不能小于 start")
    # 未给区间且文件超长：强制只读前 READ_MAX_LINES 行，引导分段读取
    if start is None and end is None and total > READ_MAX_LINES:
        end_i = READ_MAX_LINES
    end_i = min(end_i, total)
    slice_lines = all_lines[start_i - 1 : end_i]

    header = f"（文件 {rel_display(ctx, target)}，共 {total} 行，显示第 {start_i}-{end_i} 行）"
    body = "\n".join(slice_lines)
    parts = [header, body]
    if end_i < total:
        parts.append(f"\n（未完：请用 start={end_i + 1} 继续读取剩余 {total - end_i} 行）")
    return "\n".join(parts)


# ---------------------------------------------------------------- 3. search_code
def _search_code(args: dict[str, Any], ctx: ToolContext) -> str:
    """在审查根目录内做正则搜索，返回「路径:行号: 内容」命中列表。"""
    pattern = str(args.get("pattern", ""))
    if not pattern:
        raise ToolError("pattern 不能为空")
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"非法正则表达式：{exc}") from exc

    target = resolve_in_root(ctx, str(args.get("path", ".")))
    if not target.exists():
        raise ToolError(f"路径不存在：{args.get('path')}")

    files = [target] if target.is_file() else list(_iter_files(target))
    hits: list[str] = []
    truncated = False
    for file in files:
        try:
            content = _read_text(file)
        except ToolError:
            continue  # 编码不明的文件直接跳过，不影响搜索
        for lineno, line in enumerate(content.splitlines(), start=1):
            if regex.search(line):
                snippet = line.strip()[:SEARCH_LINE_LIMIT]
                hits.append(f"{rel_display(ctx, file)}:{lineno}: {snippet}")
                if len(hits) >= SEARCH_MAX_HITS:
                    truncated = True
                    break
        if truncated:
            break
    if not hits:
        return f"未找到匹配 {pattern!r} 的内容"
    header = f"共 {len(hits)} 处命中" + ("（已达上限，结果截断）" if truncated else "")
    return header + "\n" + "\n".join(hits)


# ---------------------------------------------------------------- 4. run_lint
def _run_lint(args: dict[str, Any], ctx: ToolContext) -> str:
    """调用 ruff 执行真实静态检查，返回结构化问题列表。"""
    target = resolve_in_root(ctx, str(args.get("path", ".")))
    if not target.exists():
        raise ToolError(f"路径不存在：{args.get('path')}")

    cmd = [
        sys.executable, "-m", "ruff", "check", str(target),
        "--output-format", "json", "--no-cache",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            timeout=ctx.tool_timeout, cwd=str(ctx.root),
            check=False,  # 退出码自行判断（ruff: 0=通过 1=有问题）
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"ruff 执行超时（>{ctx.tool_timeout}s）") from exc

    stderr = (proc.stderr or "").strip()
    if "No module named ruff" in stderr or "No module named ruff" in (proc.stdout or ""):
        raise ToolError("ruff 未安装，请先执行：pip install ruff")
    if proc.returncode not in (0, 1):
        # ruff: 0=无问题 1=有问题；其余为执行异常
        raise ToolError(f"ruff 执行失败（exit={proc.returncode}）：{stderr[:300]}")

    try:
        items = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise ToolError(f"ruff 输出解析失败：{exc}") from exc
    if not items:
        return f"ruff 检查通过：{rel_display(ctx, target)} 未发现问题"

    out: list[str] = [f"ruff 共发现 {len(items)} 个问题："]
    for item in items:
        loc = item.get("location") or {}
        out.append(
            f"{rel_display(ctx, Path(item.get('filename', '')))}"
            f":{loc.get('row', '?')}:{loc.get('col', '?')} "
            f"[{item.get('code', '?')}] {item.get('message', '')}"
        )
    return "\n".join(out)


# ---------------------------------------------------------------- 5. apply_fix
def _apply_fix(args: dict[str, Any], ctx: ToolContext) -> str:
    """按「唯一匹配替换」方式修复文件（需启用开关 + 用户确认 + 自动备份）。"""
    raw_path = str(args.get("path", ""))
    old_code = args.get("old_code")
    new_code = args.get("new_code")
    if old_code is None or new_code is None:
        raise ToolError("old_code 与 new_code 均为必填")
    if old_code == new_code:
        raise ToolError("old_code 与 new_code 相同，无需修复")
    if not ctx.auto_fix_enabled:
        raise ToolError("自动修复功能已在配置中禁用（auto_fix_enabled=false）")

    target = resolve_in_root(ctx, raw_path)
    if not target.exists() or target.is_dir():
        raise ToolError(f"文件不存在：{raw_path}")
    content = _read_text(target)
    occurrences = content.count(old_code)
    if occurrences == 0:
        raise ToolError("old_code 在目标文件中未找到，请核对代码是否逐字符一致")
    if occurrences > 1:
        raise ToolError(f"old_code 在文件中出现 {occurrences} 次（要求唯一），请提供更大范围的上下文")

    # 用户确认：界面层注入 confirm_fn；未注入时安全默认为拒绝
    detail = {
        "old_code": old_code,
        "new_code": new_code,
        "description": f"将 {rel_display(ctx, target)} 中的 1 处代码替换为修复版本",
    }
    if ctx.confirm_fn is None or not ctx.confirm_fn(str(target), detail):
        return "用户未确认，已取消本次修复（old_code 保持不变）"

    # 写入前备份为 <原文件名>.bak（覆盖旧备份，保证始终是「修复前一刻」的状态）
    backup = target.parent / (target.name + ".bak")
    try:
        backup.write_bytes(target.read_bytes())
        target.write_text(content.replace(old_code, new_code, 1), encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"写入文件失败：{exc}") from exc
    return (
        f"已修复 {rel_display(ctx, target)}：替换 1 处 "
        f"（备份文件 {rel_display(ctx, backup)}）"
    )


# ---------------------------------------------------------------- 注册入口
def build_default_registry(ctx: ToolContext) -> ToolRegistry:
    """构造装载了全部 5 个内置工具的注册表（Agent 与界面层的统一入口）。"""
    registry = ToolRegistry(ctx=ctx)

    registry.register(ToolSpec(
        name="list_dir",
        description="列出目录结构。默认只看一层；recursive=true 时最多展开 3 层。审查新项目时建议先用它了解布局。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对审查根目录的路径，默认 '.'"},
                "recursive": {"type": "boolean", "description": "是否递归展开子目录（最多 3 层）"},
            },
            "required": [],
        },
        handler=_list_dir,
    ))

    registry.register(ToolSpec(
        name="read_file",
        description=f"读取文本文件内容。单次最多 {READ_MAX_LINES} 行，超长文件需用 start/end 分段读取。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对审查根目录的文件路径"},
                "start": {"type": "integer", "description": "起始行号（从 1 开始）"},
                "end": {"type": "integer", "description": "结束行号（含）"},
            },
            "required": ["path"],
        },
        handler=_read_file,
    ))

    registry.register(ToolSpec(
        name="search_code",
        description="用正则表达式搜索代码，返回「路径:行号: 内容」。可用于定位可疑模式（如 eval、TODO、密码硬编码）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python 正则表达式"},
                "path": {"type": "string", "description": "搜索范围，默认整个审查根目录"},
            },
            "required": ["pattern"],
        },
        handler=_search_code,
    ))

    registry.register(ToolSpec(
        name="run_lint",
        description="对指定文件/目录执行 ruff 静态检查，返回带行列号与规则码的真实 lint 结果。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对审查根目录的路径，默认 '.'"},
            },
            "required": [],
        },
        handler=_run_lint,
    ))

    registry.register(ToolSpec(
        name="apply_fix",
        description="修复文件中的某段代码：用 new_code 精确替换唯一匹配的 old_code。必须先 read_file 获取原文，old_code 需逐字符一致且在文件中唯一；执行前会请求用户确认并自动备份。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对审查根目录的文件路径"},
                "old_code": {"type": "string", "description": "要被替换的原文（逐字符精确匹配且唯一）"},
                "new_code": {"type": "string", "description": "修复后的代码"},
            },
            "required": ["path", "old_code", "new_code"],
        },
        handler=_apply_fix,
    ))

    return registry
