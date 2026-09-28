"""配置中心 —— 加载、合并、校验项目配置，并提供服务商预设。

设计要点（对应 Design.md 第 6 节）：
1. 单一数据结构 Settings 承载所有配置，CLI / Web / Agent 核心共用；
2. 优先级：CLI 参数 > 环境变量(CRA_*) > config.yaml > 服务商预设；
3. 预设机制：切换 provider 后 base_url / model 自动带出，仍允许手工覆盖；
4. API Key 只在本地配置或环境变量中存在，对外展示一律脱敏。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------- 项目根目录
# 本文件位于 app/ 下，向上一级即仓库根目录（main.py 所在处）
ROOT = Path(__file__).resolve().parent.parent

# 实际配置与模板的默认路径
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = ROOT / "config.example.yaml"

# ---------------------------------------------------------------- 服务商预设
# DeepSeek 与小米 MiMo 均提供 OpenAI 兼容接口，因此同一套 SDK 即可接入。
# 预设值仅是「省事的默认值」，任何字段都可在配置中手工覆盖。
PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "mimo": {
        "label": "小米 MiMo",
        # 端点与模型名已按官方文档校准（2026-09-28 查询 /v1/models 验证）
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.6-pro",
    },
    "openai-compatible": {
        "label": "自定义（OpenAI 兼容）",
        "base_url": "",
        "model": "",
    },
}

# 环境变量前缀：CRA_PROVIDER / CRA_BASE_URL / CRA_API_KEY / CRA_MODEL
ENV_PREFIX = "CRA_"

# 允许环境变量覆盖的字段 → 环境变量名
_ENV_FIELDS: dict[str, str] = {
    "provider": f"{ENV_PREFIX}PROVIDER",
    "base_url": f"{ENV_PREFIX}BASE_URL",
    "api_key": f"{ENV_PREFIX}API_KEY",
    "model": f"{ENV_PREFIX}MODEL",
}


class ConfigError(ValueError):
    """配置错误（非法 provider、类型无法转换、模板缺失等）。"""


# ---------------------------------------------------------------- 配置数据类
@dataclass
class Settings:
    """合并全部来源后的最终配置。"""

    # ---- LLM 连接 ----
    provider: str = "deepseek"       # 服务商标识（见 PRESETS）
    base_url: str = ""               # 端点；空 → 用预设
    api_key: str = ""                # 密钥；空 → 尝试环境变量
    model: str = ""                  # 模型；空 → 用预设
    # ---- 运行参数 ----
    temperature: float = 0.2         # 采样温度
    max_iterations: int = 8          # Agent 循环轮数上限
    tool_timeout: int = 30           # 单工具超时（秒）
    auto_fix_enabled: bool = True    # 是否允许自动修复（仍需逐次确认）

    # ---------------- 派生属性：预设兜底 ----------------
    @property
    def preset(self) -> dict[str, str]:
        """当前服务商的预设（未知 provider 时退化为空预设）。"""
        return PRESETS.get(self.provider, {})

    @property
    def effective_base_url(self) -> str:
        """实际生效的端点：手工配置优先，其次预设。"""
        return self.base_url or self.preset.get("base_url", "")

    @property
    def effective_model(self) -> str:
        """实际生效的模型名：手工配置优先，其次预设。"""
        return self.model or self.preset.get("model", "")

    @property
    def is_configured(self) -> bool:
        """是否已具备发起 LLM 调用的最小条件（Key + 端点 + 模型）。"""
        return bool(self.api_key and self.effective_base_url and self.effective_model)

    # ---------------- 对外展示 ----------------
    def to_public_dict(self) -> dict[str, Any]:
        """转为可安全展示的字典：api_key 脱敏，附带生效值。"""
        data = asdict(self)
        data["api_key"] = mask_key(self.api_key)
        data["effective_base_url"] = self.effective_base_url
        data["effective_model"] = self.effective_model
        return data

    def to_yaml_dict(self) -> dict[str, Any]:
        """转为可写回 config.yaml 的字典（含全部字段，Key 明文仅存本地）。"""
        return asdict(self)


# ---------------------------------------------------------------- 工具函数
def mask_key(key: str) -> str:
    """脱敏 API Key，仅保留前 3 后 4 位，防止日志 / 界面泄漏。"""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}***{key[-4:]}"


def _coerce(name: str, value: Any) -> Any:
    """把来自 YAML / 环境变量的原始值转换为字段的正确类型。

    环境变量一律是字符串，YAML 可能给出 int/float，这里统一归一化；
    转换失败抛 ConfigError（而不是等到运行期才炸）。
    """
    try:
        if name in {"temperature"}:
            return float(value)
        if name in {"max_iterations", "tool_timeout"}:
            iv = int(value)
            if iv < 1:
                raise ValueError(f"{name} 必须 >= 1")
            return iv
        if name == "auto_fix_enabled":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}
        if name == "temperature" and not (0.0 <= float(value) <= 2.0):
            raise ValueError("temperature 必须在 0~2 之间")
        return str(value).strip() if value is not None else ""
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"配置项 {name!r} 的值非法：{value!r}（{exc}）") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    """读取 YAML 配置文件；文件不存在返回空字典（预设兜底）。"""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 {path.name} 不是合法的 YAML：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件 {path.name} 顶层必须是键值映射")
    return data


def _known_names() -> set[str]:
    """Settings 的合法字段名集合（用于过滤 YAML 中的未知键）。"""
    return {f.name for f in fields(Settings)}


def load_settings(
    overrides: Mapping[str, Any] | None = None,
    config_path: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Settings:
    """按优先级合并配置并返回校验后的 Settings。

    Args:
        overrides: 最高优先级覆盖（通常来自 CLI 参数）；值为 None 的键会被忽略
        config_path: 指定配置文件路径，默认项目根目录的 config.yaml
        env: 环境变量映射，默认读 os.environ（测试时可注入假环境）

    Raises:
        ConfigError: provider 非法或字段类型无法转换
    """
    path = config_path or CONFIG_PATH
    env = os.environ if env is None else env

    # 第 1 步：读 YAML（不存在则空）
    data: dict[str, Any] = dict(_read_yaml(path))

    # 第 2 步：环境变量覆盖 YAML（仅连接类四字段）
    for field_name, env_name in _ENV_FIELDS.items():
        if env.get(env_name):
            data[field_name] = env[env_name]

    # 第 3 步：CLI overrides 覆盖一切（忽略值为 None 的项）
    if overrides:
        data.update({k: v for k, v in overrides.items() if v is not None})

    # 第 4 步：过滤未知键（YAML 里可能有注释性残留字段，不因此报错）
    unknown = set(data) - _known_names()
    for key in unknown:
        data.pop(key, None)

    # 第 5 步：校验 provider
    provider = str(data.get("provider", "deepseek")).strip()
    if provider not in PRESETS:
        raise ConfigError(
            f"未知服务商 {provider!r}，可选：{', '.join(PRESETS)}"
        )
    data["provider"] = provider

    # 第 6 步：类型归一化 + 数值校验
    settings = Settings()
    for name in _known_names():
        if name in data:
            setattr(settings, name, _coerce(name, data[name]))

    # temperature 范围校验（_coerce 中的分支对 float 不生效，这里补上）
    if not (0.0 <= settings.temperature <= 2.0):
        raise ConfigError(f"temperature 必须在 0~2 之间，当前 {settings.temperature}")

    return settings


def save_settings(settings: Settings, config_path: Path | None = None) -> Path:
    """把 Settings 写回配置文件（明文仅存本地，该文件在 .gitignore 中）。

    Returns:
        实际写入的文件路径
    """
    path = config_path or CONFIG_PATH
    lines = [
        "# Code Review Agent 实际配置（由配置中心自动生成，勿提交到 git）",
        f"provider: {settings.provider}",
        f"base_url: {settings.base_url}",
        # 用双引号包裹，避免 Key 中的特殊字符破坏 YAML 结构
        f'api_key: "{settings.api_key}"',
        f"model: {settings.model}",
        f"temperature: {settings.temperature}",
        f"max_iterations: {settings.max_iterations}",
        f"tool_timeout: {settings.tool_timeout}",
        f"auto_fix_enabled: {'true' if settings.auto_fix_enabled else 'false'}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def provider_choices() -> list[tuple[str, str]]:
    """返回 (标识, 显示名) 列表，供 CLI / Web 下拉框渲染。"""
    return [(pid, p["label"]) for pid, p in PRESETS.items()]
