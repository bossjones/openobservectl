"""Tests for openobservectl.config — profile loading, resolution, and env overrides."""

from __future__ import annotations

from pathlib import Path

import pytest

from openobservectl.config import (
    Config,
    ConfigError,
    Profile,
    default_config_path,
    load_config,
    resolve_profile,
)


def test_default_config_path_is_under_home() -> None:
    path = default_config_path()
    assert path.name == "config.yaml"
    assert path.parent.name == ".openobservectl"


def test_load_config_parses_profiles(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert isinstance(cfg, Config)
    assert set(cfg.profiles) == {"default", "dev"}
    default = cfg.profiles["default"]
    assert isinstance(default, Profile)
    assert default.endpoint == "http://127.0.0.1:5080"
    assert default.organization == "default"
    assert default.username == "admin@example.com"
    assert default.password == "Complexpass#123"
    assert default.timeout == 10.0  # default
    assert default.verify is True  # default


def test_profile_optional_fields_parsed(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    dev = cfg.profiles["dev"]
    assert dev.timeout == 30.0
    assert dev.verify is False


def test_endpoint_trailing_slash_normalized(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    assert cfg.profiles["dev"].endpoint == "https://dev.openobserve.com"


def test_load_config_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.yaml")
    assert "not found" in str(exc.value).lower()


def test_load_config_empty_or_no_profiles_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.yaml"
    p.write_text("other: 1\n")
    with pytest.raises(ConfigError):
        load_config(p)


def test_resolve_profile_returns_named(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    prof = resolve_profile(cfg, "dev", env={})
    assert prof.organization == "dev-org"


def test_resolve_profile_unknown_lists_available(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    with pytest.raises(ConfigError) as exc:
        resolve_profile(cfg, "missing", env={})
    msg = str(exc.value)
    assert "missing" in msg
    assert "default" in msg and "dev" in msg  # available profiles listed


def test_env_overrides_win_over_profile(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    env = {
        "OPENOBSERVE_URL": "http://10.0.0.5:5080",
        "OPENOBSERVE_ORG": "override-org",
        "OPENOBSERVE_USER": "override@x.com",
        "OPENOBSERVE_PASSWORD": "override-pass",
    }
    prof = resolve_profile(cfg, "default", env=env)
    assert prof.endpoint == "http://10.0.0.5:5080"
    assert prof.organization == "override-org"
    assert prof.username == "override@x.com"
    assert prof.password == "override-pass"


def test_env_override_endpoint_only_keeps_other_fields(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    prof = resolve_profile(cfg, "default", env={"OPENOBSERVE_URL": "http://10.0.0.9:5080"})
    assert prof.endpoint == "http://10.0.0.9:5080"
    assert prof.username == "admin@example.com"  # unchanged
    assert prof.password == "Complexpass#123"


def test_env_override_endpoint_trailing_slash_normalized(sample_config_path: Path) -> None:
    cfg = load_config(sample_config_path)
    prof = resolve_profile(cfg, "default", env={"OPENOBSERVE_URL": "http://10.0.0.9:5080/"})
    assert prof.endpoint == "http://10.0.0.9:5080"
