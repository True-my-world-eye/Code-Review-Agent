"""Code Review Agent 统一入口。

用法示例：
    python main.py --help                 # 查看全部命令
    python main.py review <路径>           # 审查文件 / 目录
    python main.py chat                   # 交互式多轮对话
    python main.py config show            # 查看当前配置（Key 脱敏）
    python main.py config set ...         # 修改配置
    python main.py config test            # 测试 LLM 连通性

命令实现位于 app/cli/app.py，本文件仅作为可执行入口。
"""

from __future__ import annotations

from app.cli.app import app

if __name__ == "__main__":
    app()
