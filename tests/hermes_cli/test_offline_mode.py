"""Tests for hermes_cli.config.offline_mode_enabled (agent.offline)."""

from hermes_constants import get_hermes_home

from hermes_cli.config import offline_mode_enabled


def _write_config(text: str) -> None:
    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_offline_mode_disabled_by_default():
    """No config.yaml at all -> offline mode off (default False)."""
    assert offline_mode_enabled() is False


def test_offline_mode_enabled_via_config():
    _write_config("agent:\n  offline: true\n")
    assert offline_mode_enabled() is True


def test_offline_mode_explicit_false():
    _write_config("agent:\n  offline: false\n")
    assert offline_mode_enabled() is False


def test_offline_mode_truthy_string_accepted():
    """YAML-unquoted strings like \"yes\" follow the shared truthy set."""
    _write_config("agent:\n  offline: \"yes\"\n")
    assert offline_mode_enabled() is True


def test_offline_mode_missing_agent_section():
    _write_config("model:\n  default: gpt-5\n")
    assert offline_mode_enabled() is False
