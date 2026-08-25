"""Tests for the settings-block structural contract in litellm_config.yaml.

The ``model_list`` contract is enforced by ``test_model_entry_contract.py``
(refs #80), but the three settings blocks — ``litellm_settings``,
``general_settings``, and ``router_settings`` — have no executable contract.
These blocks carry safety-critical configuration:

- ``drop_params`` / ``additional_drop_params`` silently strip parameters the
  upstream doesn't support. A typo (``drop_param``) would cause the upstream to
  reject every request, and CI would not catch it.
- ``general_settings.master_key`` controls the auth boundary. A malformed
  reference silently disables auth or breaks startup.
- ``callbacks`` registers the observability logger and the ``/health/version``
  endpoint. A stale module path silently drops both.
- ``router_settings.num_retries`` duplicates ``litellm_settings.num_retries``;
  divergence means the effective retry behavior differs from what CONTEXT.md
  documents.

This is an executable specification of "what a correct settings block looks
like" — it catches config errors at PR time instead of at runtime.

Refs #119
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "litellm_config.yaml"

# The only top-level keys LiteLLM's proxy config accepts.
KNOWN_TOP_LEVEL_KEYS = {
    "model_list",
    "litellm_settings",
    "general_settings",
    "router_settings",
    "environment_variables",
}

# Callback module paths must resolve to a Python file at the repo root.
CALLBACK_MODULE_PREFIX = "litellm_logger"
CALLBACK_MODULE_FILES = {
    "litellm_logger": REPO_ROOT / "litellm_logger.py",
    "health_version": REPO_ROOT / "health_version.py",
}


@pytest.fixture
def config():
    """Load and return the parsed litellm_config.yaml.

    Mirrors the fixture in test_model_entry_contract.py: skips when the file is
    absent, fails clearly when the YAML is empty or not a mapping.
    """
    if not CONFIG_PATH.exists():
        pytest.skip(f"litellm_config.yaml not found at {CONFIG_PATH}")
    with open(CONFIG_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        pytest.fail(
            f"litellm_config.yaml did not parse as a mapping (got {type(data).__name__})"
        )
    return data


def _litellm_settings(config):
    """Return litellm_settings, asserting it is a mapping.

    A structurally wrong config (e.g. litellm_settings is a string or list)
    would otherwise let the per-block assertions call .get() on a non-mapping
    and fail with an opaque AttributeError instead of a clear contract message
    (CONTEXT.md §1).
    """
    settings = config.get("litellm_settings", {})
    assert isinstance(settings, dict), (
        f"litellm_settings must be a mapping; got {type(settings).__name__} "
        f"({settings!r}) (CONTEXT.md §1)."
    )
    return settings


def _general_settings(config):
    """Return general_settings, asserting it is a mapping (CONTEXT.md §1)."""
    settings = config.get("general_settings", {})
    assert isinstance(settings, dict), (
        f"general_settings must be a mapping; got {type(settings).__name__} "
        f"({settings!r}) (CONTEXT.md §1)."
    )
    return settings


def _router_settings(config):
    """Return router_settings, asserting it is a mapping (CONTEXT.md §1)."""
    settings = config.get("router_settings", {})
    assert isinstance(settings, dict), (
        f"router_settings must be a mapping; got {type(settings).__name__} "
        f"({settings!r}) (CONTEXT.md §1)."
    )
    return settings


class TestTopLevelKeys:
    """The YAML must not contain unknown top-level keys."""

    def test_no_unknown_top_level_keys(self, config):
        """A typo like ``litellm_setting`` (missing 's') is caught immediately."""
        unknown = set(config.keys()) - KNOWN_TOP_LEVEL_KEYS
        assert not unknown, (
            f"Unknown top-level key(s) in litellm_config.yaml: {sorted(unknown)}. "
            f"Expected one of {sorted(KNOWN_TOP_LEVEL_KEYS)} (CONTEXT.md §1)."
        )


class TestLitellmSettings:
    """litellm_settings must carry the safety-critical param-stripping config."""

    def test_drop_params_is_true(self, config):
        """drop_params must be the boolean true (not a string or missing)."""
        settings = _litellm_settings(config)
        assert settings.get("drop_params") is True, (
            f"litellm_settings.drop_params must be true; got "
            f"{settings.get('drop_params')!r}. A typo here lets unsupported "
            f"params reach the upstream and cause 400s (CONTEXT.md §1)."
        )

    def test_additional_drop_params_is_list_of_strings(self, config):
        """additional_drop_params must be a list of strings."""
        settings = _litellm_settings(config)
        value = settings.get("additional_drop_params")
        assert isinstance(value, list) and value, (
            f"litellm_settings.additional_drop_params must be a non-empty list; "
            f"got {value!r} (CONTEXT.md §1)."
        )
        assert all(isinstance(item, str) for item in value), (
            f"litellm_settings.additional_drop_params must be a list of strings; "
            f"got {value!r} (CONTEXT.md §1)."
        )

    def test_json_logs_is_bool(self, config):
        """json_logs must be a bool (guards the key's presence/type).

        A typo (e.g. ``json_logs: "true"`` as a string, or a missing key)
        silently disables structured proxy logging without a signal.
        """
        settings = _litellm_settings(config)
        json_logs = settings.get("json_logs")
        assert isinstance(json_logs, bool), (
            f"litellm_settings.json_logs must be a bool; got {json_logs!r}. "
            f"A non-bool value silently disables structured proxy logs "
            f"(CONTEXT.md §1)."
        )

    def test_callbacks_is_non_empty_list(self, config):
        """callbacks must be a non-empty list (observability + health/version)."""
        settings = _litellm_settings(config)
        callbacks = settings.get("callbacks")
        assert isinstance(callbacks, list) and callbacks, (
            f"litellm_settings.callbacks must be a non-empty list; got "
            f"{callbacks!r}. Without it, PROXY_LOG and /health/version vanish "
            f"(CONTEXT.md §1)."
        )

    def test_callback_module_paths_are_importable(self, config):
        """Each callback's module path must resolve to an existing Python file.

        A stale module path (e.g. after a rename) silently drops observability
        and the /health/version endpoint while the proxy still starts.
        """
        settings = _litellm_settings(config)
        callbacks = settings.get("callbacks", [])
        for callback in callbacks:
            assert isinstance(callback, str), (
                f"callback {callback!r} is not a string (CONTEXT.md §1)."
            )
            module = callback.split(".")[0]
            assert module in CALLBACK_MODULE_FILES, (
                f"callback {callback!r} references unknown module {module!r}; "
                f"expected one of {sorted(CALLBACK_MODULE_FILES)} (CONTEXT.md §1)."
            )
            assert CALLBACK_MODULE_FILES[module].exists(), (
                f"callback {callback!r} references {CALLBACK_MODULE_FILES[module].name} "
                f"which does not exist at the repo root (CONTEXT.md §1)."
            )


class TestGeneralSettings:
    """general_settings must carry the auth boundary."""

    def test_master_key_uses_os_environ_pattern(self, config):
        """master_key must reference an env var via the os.environ/ pattern."""
        settings = _general_settings(config)
        master_key = settings.get("master_key")
        assert isinstance(master_key, str) and master_key.startswith("os.environ/"), (
            f"general_settings.master_key must be an 'os.environ/...' reference; "
            f"got {master_key!r}. A malformed reference silently disables auth "
            f"(CONTEXT.md §1)."
        )


class TestRouterSettings:
    """router_settings must be internally consistent."""

    def test_num_retries_is_positive_int(self, config):
        """router_settings.num_retries must be a positive integer."""
        settings = _router_settings(config)
        num_retries = settings.get("num_retries")
        assert isinstance(num_retries, int) and num_retries > 0, (
            f"router_settings.num_retries must be a positive int; got "
            f"{num_retries!r} (CONTEXT.md §1)."
        )

    def test_num_retries_consistent_across_blocks(self, config):
        """num_retries must match between litellm_settings and router_settings.

        If only one block is updated, the effective retry behavior diverges from
        what CONTEXT.md documents without any signal.
        """
        litellm_retries = _litellm_settings(config).get("num_retries")
        router_retries = _router_settings(config).get("num_retries")
        assert litellm_retries == router_retries, (
            f"num_retries diverges: litellm_settings={litellm_retries!r} vs "
            f"router_settings={router_retries!r}. They must match (CONTEXT.md §1)."
        )


class TestMalformedSettingsFailsLoud:
    """Structural breaks must fail with a clear contract message, not AttributeError.

    Mirrors ``TestMalformedConfigFailsLoud`` in test_model_entry_contract.py: the
    settings-block helpers assert types up front so a deliberately-broken config
    yields an actionable failure instead of an opaque AttributeError (from .get()
    on a non-mapping) or a confusing pass. Refs #137.
    """

    def test_drop_params_as_string_fails(self):
        """drop_params as a string (e.g. "true") must fail loudly."""
        with pytest.raises(AssertionError, match="drop_params must be true"):
            TestLitellmSettings().test_drop_params_is_true(
                {"litellm_settings": {"drop_params": "true"}}
            )

    def test_additional_drop_params_with_non_str_member_fails(self):
        """additional_drop_params with a non-str member must fail loudly."""
        with pytest.raises(AssertionError, match="list of strings"):
            TestLitellmSettings().test_additional_drop_params_is_list_of_strings(
                {"litellm_settings": {"additional_drop_params": ["response_format", 42]}}
            )

    def test_empty_callbacks_fails(self):
        """Empty callbacks must fail loudly (observability would vanish)."""
        with pytest.raises(AssertionError, match="callbacks must be a non-empty list"):
            TestLitellmSettings().test_callbacks_is_non_empty_list(
                {"litellm_settings": {"callbacks": []}}
            )

    def test_master_key_not_os_environ_fails(self):
        """master_key not matching the os.environ/ pattern must fail loudly."""
        with pytest.raises(AssertionError, match="os.environ"):
            TestGeneralSettings().test_master_key_uses_os_environ_pattern(
                {"general_settings": {"master_key": "LITELLM_MASTER_KEY"}}
            )

    def test_num_retries_mismatch_fails(self):
        """num_retries diverging across blocks must fail loudly."""
        with pytest.raises(AssertionError, match="num_retries diverges"):
            TestRouterSettings().test_num_retries_consistent_across_blocks(
                {
                    "litellm_settings": {"num_retries": 3},
                    "router_settings": {"num_retries": 5},
                }
            )

    def test_unknown_top_level_key_fails(self):
        """An unknown top-level key (e.g. litellm_setting typo) must fail loudly."""
        with pytest.raises(AssertionError, match="Unknown top-level key"):
            TestTopLevelKeys().test_no_unknown_top_level_keys(
                {"litellm_setting": {}}
            )

    def test_litellm_settings_must_be_a_mapping(self):
        """litellm_settings as a non-mapping must fail with a clear message."""
        with pytest.raises(AssertionError, match="litellm_settings must be a mapping"):
            _litellm_settings({"litellm_settings": "not-a-mapping"})
