"""Prompt 构造与结构化报告解析。

- build_system_prompt()：生成发给模型的系统提示词（含审查根目录、
  工具使用流程、输出 JSON Schema 约定）；
- parse_review_report()：把模型最终文本解析为结构化审查报告，
  容错处理：合法 JSON 块 → 首尾大括号提取 → 全文降级。
"""

from __future__ import annotations

import json
import re
from typing import Any

# 系统提示词模板：__ROOT__ 占位符在构建时替换（避免 f-string 与 JSON 花括号冲突）
_SYSTEM_TEMPLATE = """你是一名资深代码审查 Agent，运行在用户的本地环境中。
审查根目录：__ROOT__

## 工作方式（必须使用工具，禁止凭空猜测代码内容）
1. 用户给出目录时，先用 list_dir 了解结构；给出文件则直接 read_file。
2. 用 read_file 分段阅读代码（单次最多 400 行，注意返回中的 start/end 提示）。
3. 调用 run_lint 获取真实静态检查结果，并补充 lint 发现不了的逻辑/安全问题。
4. 需要定位可疑模式（eval、硬编码密钥、SQL 拼接等）时用 search_code。

## 审查关注点（按优先级）
- 正确性：逻辑错误、边界条件、空值与异常处理缺陷
- 安全：注入、硬编码凭据、路径穿越、不安全反序列化
- 质量：重复代码、过长函数、命名、缺失的错误处理
- 性能：明显的低效循环、资源泄漏

## 最终输出（必须严格遵守）
先用一句话给出总结，然后输出一个 JSON 代码块，格式如下：

```json
{{"summary": "一句话总结", "issues": [{{"severity": "error", "file": "相对路径", "line": 1, "message": "问题描述", "suggestion": "修复建议"}}]}}
```

- severity 取值：error（必须修复的缺陷/漏洞）/ warning（大概率有问题）/ suggestion（改进建议）
- line 为从 1 开始的行号；不确定时给出最接近的行并在 message 中说明
- 只报告你实际读过的代码中的问题；没有问题就返回空 issues 数组

## 其他规则
- 工具返回 "Error: ..." 时，先阅读原因再调整策略，不要重复相同的失败调用
- 所有路径必须位于审查根目录之内
- apply_fix 需要用户确认；若未被确认，不要重试同一修复，改为在报告中给出建议
"""


def build_system_prompt(root: str) -> str:
    """按审查根目录生成系统提示词。"""
    return _SYSTEM_TEMPLATE.replace("__ROOT__", root)


# ---------------------------------------------------------------- 报告解析
_SEVERITIES = {"error", "warning", "suggestion"}


def _extract_json_str(content: str) -> str | None:
    """从模型文本中提取 JSON 字符串。

    优先取 ```json 代码块（贪婪匹配到成对的结束围栏）；
    没有围栏则退化为「第一个 { 到最后一个 }」。
    """
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", content, re.DOTALL)
    if fence:
        return fence.group(1).strip()
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end > start:
        return content[start : end + 1]
    return None


def _normalize_issue(raw: Any) -> dict[str, Any] | None:
    """把单条 issue 归一化为统一结构（字段缺失时尽量容错）。"""
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity", "suggestion")).strip().lower()
    if severity not in _SEVERITIES:
        severity = "suggestion"
    # 行号容错：字符串数字转 int，非法值降为 0（0 表示「未知行」）
    try:
        line = int(raw.get("line", 0) or 0)
    except (TypeError, ValueError):
        line = 0
    message = str(raw.get("message", "")).strip()
    if not message:
        return None  # 没有任何描述的问题没有价值，丢弃
    return {
        "severity": severity,
        "file": str(raw.get("file", "")).strip(),
        "line": max(line, 0),
        "message": message,
        "suggestion": str(raw.get("suggestion", "")).strip(),
    }


def parse_review_report(content: str) -> dict[str, Any]:
    """解析模型输出为结构化报告，永不抛异常。

    Returns:
        {"summary": str, "issues": [...], "raw": 原文}
        解析失败时 issues 为空、raw 保留全文，由界面层降级展示。
    """
    text = content or ""
    candidate = _extract_json_str(text)
    data: Any = None
    if candidate:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            data = None

    if isinstance(data, dict):
        raw_issues = data.get("issues")
        issues: list[dict[str, Any]] = []
        if isinstance(raw_issues, list):
            for item in raw_issues:
                normalized = _normalize_issue(item)
                if normalized is not None:
                    issues.append(normalized)
        return {
            "summary": str(data.get("summary", "")).strip(),
            "issues": issues,
            "raw": text,
        }
    return {"summary": "", "issues": [], "raw": text}
