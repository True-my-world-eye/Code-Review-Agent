"""Prompt 与结构化报告解析测试。"""

from __future__ import annotations

import json

from app.core.prompt import build_system_prompt, parse_review_report


def test_system_prompt_contains_root_and_rules() -> None:
    """系统提示词包含审查根目录与输出 Schema 关键约束。"""
    prompt = build_system_prompt("D:/code/demo")
    assert "D:/code/demo" in prompt
    assert "list_dir" in prompt and "run_lint" in prompt
    assert '"severity"' in prompt  # 输出格式约定存在
    assert "__ROOT__" not in prompt  # 占位符已被替换


def test_parse_valid_json_block() -> None:
    """标准 ```json 围栏输出 → 正确解析。"""
    content = (
        '审查完成。\n```json\n'
        + json.dumps(
            {
                "summary": "发现 1 个问题",
                "issues": [
                    {
                        "severity": "error",
                        "file": "app.py",
                        "line": 42,
                        "message": "SQL 拼接注入风险",
                        "suggestion": "使用参数化查询",
                    }
                ],
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    report = parse_review_report(content)
    assert report["summary"] == "发现 1 个问题"
    assert len(report["issues"]) == 1
    issue = report["issues"][0]
    assert issue["severity"] == "error"
    assert issue["line"] == 42
    assert "参数化" in issue["suggestion"]


def test_parse_nested_braces_with_fence() -> None:
    """JSON 内嵌花括号 + 围栏：贪婪匹配到正确边界。"""
    content = '```json\n{"summary": "含 {花括号} 的文本", "issues": []}\n```'
    report = parse_review_report(content)
    assert report["summary"] == "含 {花括号} 的文本"


def test_parse_without_fence() -> None:
    """无围栏：用首尾大括号提取。"""
    content = '结论如下 {"summary": "直接输出", "issues": []} 以上。'
    report = parse_review_report(content)
    assert report["summary"] == "直接输出"


def test_parse_invalid_json_degrades_to_raw() -> None:
    """非法 JSON：不抛异常，issues 为空且 raw 保留原文。"""
    content = "```json\n{broken json\n```"
    report = parse_review_report(content)
    assert report["issues"] == [] and report["summary"] == ""
    assert report["raw"] == content


def test_parse_empty_content() -> None:
    report = parse_review_report("")
    assert report == {"summary": "", "issues": [], "raw": "", "parsed": False}


def test_issue_normalization() -> None:
    """字段容错：非法 severity 降级、行号字符串转 int、空 message 丢弃。"""
    content = json.dumps(
        {
            "summary": "s",
            "issues": [
                {"severity": "CRITICAL", "file": "a.py", "line": "17", "message": "有描述"},
                {"severity": "warning", "file": "b.py", "line": "未知", "message": "行号非法"},
                {"severity": "error", "file": "c.py", "line": 1, "message": "   "},
            ],
        }
    )
    report = parse_review_report(content)
    assert len(report["issues"]) == 2  # 空 message 被丢弃
    first, second = report["issues"]
    assert first["severity"] == "suggestion" and first["line"] == 17
    assert second["line"] == 0  # 非法行号降为 0


def test_issues_not_a_list_degrades() -> None:
    """issues 字段类型错误时按空列表处理。"""
    content = json.dumps({"summary": "s", "issues": "不是数组"})
    report = parse_review_report(content)
    assert report["issues"] == [] and report["summary"] == "s"


def test_fix_field_normalized() -> None:
    """合法 fix 字段保留；结构不合法的 fix 丢弃，不影响 issue 本身。"""
    content = json.dumps(
        {
            "summary": "s",
            "issues": [
                {
                    "severity": "error",
                    "file": "a.py",
                    "line": 1,
                    "message": "问题A",
                    "fix": {"old_code": "x = 1", "new_code": "x = 2"},
                },
                {
                    "severity": "warning",
                    "file": "b.py",
                    "line": 2,
                    "message": "问题B",
                    "fix": {"old_code": "", "new_code": "y"},  # 空 old_code 非法
                },
                {
                    "severity": "suggestion",
                    "file": "c.py",
                    "line": 3,
                    "message": "问题C",
                    "fix": "不是字典",  # 类型非法
                },
            ],
        }
    )
    report = parse_review_report(content)
    assert report["issues"][0]["fix"] == {"old_code": "x = 1", "new_code": "x = 2"}
    assert report["issues"][1]["fix"] is None
    assert report["issues"][2]["fix"] is None
    assert report["parsed"] is True
