"""Code Review Agent 应用包。

包结构（分层依赖方向：cli/web → core → llm/tools → config）：
- app.config  : 配置中心（本模块所在）
- app.llm     : LLM 适配层（OpenAI 兼容 + 重试）
- app.core    : Agent 循环、上下文记忆、Prompt 构造
- app.tools   : 工具注册表与内置工具
- app.cli     : 命令行界面
- app.web     : Web 界面（FastAPI 服务）
"""

__version__ = "1.0.0"
