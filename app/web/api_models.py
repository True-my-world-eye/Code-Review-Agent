"""Web API 的请求体模型（pydantic，负责入参校验与文档化）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConfigUpdate(BaseModel):
    """PUT /api/config 的请求体：所有字段可选，只更新提供的项。"""

    provider: str | None = None
    base_url: str | None = None
    # 约定：空字符串表示「保持现有 Key 不变」（前端不回显明文 Key）
    api_key: str | None = None
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_iterations: int | None = Field(default=None, ge=1, le=50)
    tool_timeout: int | None = Field(default=None, ge=1, le=600)
    auto_fix_enabled: bool | None = None


class ReviewRequest(BaseModel):
    """POST /api/review 的请求体：路径模式与粘贴模式二选一。"""

    path: str = Field(default=".", description="相对项目的审查路径（path 模式）")
    ask: str | None = Field(default=None, description="补充审查要求")
    code: str | None = Field(default=None, description="粘贴的代码（code 模式）")
    file_name: str | None = Field(
        default=None, description="粘贴模式下的文件名，默认 snippet.py"
    )


class FixRequest(BaseModel):
    """POST /api/fix 的请求体：报告卡片「应用修复」按钮的载荷。"""

    path: str
    old_code: str
    new_code: str
