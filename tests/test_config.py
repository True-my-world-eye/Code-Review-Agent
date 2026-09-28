"""配置中心单元测试。

覆盖点（对应 docs/测试文档.md · 单测用例表）：
- 服务商预设填充与手工覆盖
- 配置优先级：overrides > env > yaml > 预设
- API Key 脱敏
- 非法 provider / 越界数值的校验
- 字符串类型归一化（环境变量与 CLI 传参场景）
- 保存 → 加载回环

所有用例均注入空/受控的 env 字典，避免机器上的真实 CRA_* 变量干扰。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.config import (
    PRESETS,
    ConfigError,
    Settings,
    load_settings,
    mask_key,
    provider_choices,
    save_settings,
)

# 测试专用的「干净环境」
EMPTY_ENV: dict[str, str] = {}


def write_config(tmp_path: Path, data: dict) -> Path:
    """在临时目录生成一份 YAML 配置，返回其路径。"""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------- 预设与覆盖
def test_preset_fallback(tmp_path: Path) -> None:
    """只指定 provider 时，端点与模型应回落到内置预设。"""
    path = write_config(tmp_path, {"provider": "deepseek"})
    s = load_settings(config_path=path, env=EMPTY_ENV)
    assert s.effective_base_url == PRESETS["deepseek"]["base_url"]
    assert s.effective_model == PRESETS["deepseek"]["model"]


def test_explicit_value_beats_preset(tmp_path: Path) -> None:
    """手工配置的 base_url / model 优先于预设。"""
    path = write_config(
        tmp_path,
        {"provider": "deepseek", "base_url": "http://custom/v1", "model": "my-model"},
    )
    s = load_settings(config_path=path, env=EMPTY_ENV)
    assert s.effective_base_url == "http://custom/v1"
    assert s.effective_model == "my-model"


def test_mimo_preset_exists() -> None:
    """内置预设必须包含作业要求的 deepseek 与 mimo 两个服务商。"""
    assert "deepseek" in PRESETS and "mimo" in PRESETS
    for pid, preset in PRESETS.items():
        assert "base_url" in preset and "model" in preset, pid


# ---------------------------------------------------------------- 优先级
def test_env_overrides_yaml(tmp_path: Path) -> None:
    """环境变量优先级高于 YAML。"""
    path = write_config(tmp_path, {"provider": "deepseek", "model": "yaml-model"})
    s = load_settings(config_path=path, env={"CRA_MODEL": "env-model"})
    assert s.effective_model == "env-model"


def test_cli_overrides_beat_env(tmp_path: Path) -> None:
    """CLI overrides 优先级最高，压过环境变量。"""
    path = write_config(tmp_path, {"provider": "deepseek", "model": "yaml-model"})
    s = load_settings(
        overrides={"model": "cli-model"},
        config_path=path,
        env={"CRA_MODEL": "env-model"},
    )
    assert s.effective_model == "cli-model"


def test_none_override_is_ignored(tmp_path: Path) -> None:
    """CLI 未传的参数（值为 None）不得覆盖已有配置。"""
    path = write_config(tmp_path, {"provider": "deepseek", "model": "keep-me"})
    s = load_settings(overrides={"model": None}, config_path=path, env=EMPTY_ENV)
    assert s.effective_model == "keep-me"


def test_missing_config_file_uses_defaults(tmp_path: Path) -> None:
    """配置文件不存在时不应报错，直接回落到默认值 + 预设。"""
    s = load_settings(config_path=tmp_path / "absent.yaml", env=EMPTY_ENV)
    assert s.provider == "deepseek"
    assert s.max_iterations == 8


# ---------------------------------------------------------------- 校验
def test_invalid_provider_rejected(tmp_path: Path) -> None:
    """未知服务商必须抛 ConfigError。"""
    path = write_config(tmp_path, {"provider": "not-a-provider"})
    with pytest.raises(ConfigError, match="not-a-provider"):
        load_settings(config_path=path, env=EMPTY_ENV)


def test_temperature_out_of_range(tmp_path: Path) -> None:
    """temperature 越界必须抛 ConfigError。"""
    path = write_config(tmp_path, {"provider": "deepseek", "temperature": 3.5})
    with pytest.raises(ConfigError, match="temperature"):
        load_settings(config_path=path, env=EMPTY_ENV)


def test_non_positive_iteration_rejected(tmp_path: Path) -> None:
    """max_iterations < 1 必须被拒绝。"""
    path = write_config(tmp_path, {"provider": "deepseek", "max_iterations": 0})
    with pytest.raises(ConfigError):
        load_settings(config_path=path, env=EMPTY_ENV)


def test_unknown_yaml_keys_ignored(tmp_path: Path) -> None:
    """YAML 中的未知键应被忽略，而不是导致加载失败。"""
    path = write_config(
        tmp_path, {"provider": "deepseek", "leftover_comment_field": "x"}
    )
    s = load_settings(config_path=path, env=EMPTY_ENV)
    assert s.provider == "deepseek"


# ---------------------------------------------------------------- 类型归一化
def test_string_values_coerced(tmp_path: Path) -> None:
    """YAML/环境变量传来的字符串应转换为正确类型。"""
    path = write_config(
        tmp_path,
        {"provider": "deepseek", "max_iterations": "12", "auto_fix_enabled": "false"},
    )
    s = load_settings(config_path=path, env=EMPTY_ENV)
    assert isinstance(s.max_iterations, int) and s.max_iterations == 12
    assert s.auto_fix_enabled is False


def test_env_api_key(tmp_path: Path) -> None:
    """api_key 可以完全来自环境变量。"""
    path = write_config(tmp_path, {"provider": "deepseek"})
    s = load_settings(config_path=path, env={"CRA_API_KEY": "sk-secret-abcdef"})
    assert s.api_key == "sk-secret-abcdef"
    assert s.is_configured is True  # Key + 预设端点 + 预设模型 → 可用


# ---------------------------------------------------------------- 脱敏与回环
def test_mask_key() -> None:
    """脱敏结果不得包含完整 Key。"""
    assert mask_key("") == ""
    assert mask_key("abc") == "***"
    masked = mask_key("sk-abcdefghijklmnop")
    assert masked.startswith("sk-") and "***" in masked
    assert "ghijklmnop" not in masked


def test_public_dict_masks_key(tmp_path: Path) -> None:
    """对外展示字典中的 api_key 必须是脱敏后的值。"""
    path = write_config(tmp_path, {"provider": "deepseek"})
    s = load_settings(
        overrides={"api_key": "sk-topsecret-123456"}, config_path=path, env=EMPTY_ENV
    )
    public = s.to_public_dict()
    assert "topsecret" not in public["api_key"]
    assert public["effective_model"] == PRESETS["deepseek"]["model"]


def test_save_load_roundtrip(tmp_path: Path) -> None:
    """save_settings 写出的文件应能被 load_settings 原样读回。"""
    original = Settings(
        provider="mimo",
        base_url="http://localhost:8000/v1",
        api_key="sk-roundtrip-key",
        model="round-model",
        temperature=0.5,
        max_iterations=5,
        tool_timeout=15,
        auto_fix_enabled=False,
    )
    out = save_settings(original, config_path=tmp_path / "rt.yaml")
    loaded = load_settings(config_path=out, env=EMPTY_ENV)
    assert loaded == original


def test_provider_choices() -> None:
    """服务商下拉数据应包含三项且标识与预设一致。"""
    ids = [pid for pid, _ in provider_choices()]
    assert ids == list(PRESETS.keys())
