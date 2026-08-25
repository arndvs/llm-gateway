"""Tests for the model-entry structural contract in litellm_config.yaml.

Every ``model_list`` entry must satisfy the contract documented in CONTEXT.md §1.
The proxy is dual-provider: each alias has a PRIMARY (OpenRouter — the default
upstream) and a FALLBACK (GitHub Copilot) deployment. OpenRouter serves real
completions reliably; Copilot is the automatic fallback lane per alias. This is
an executable specification of "what a correct model entry looks like" — it
catches config errors at PR time instead of at runtime (the daily
``model-health.yml`` probe, which needs secrets and can't tell "provider changed
availability" from "the config is structurally wrong").

Refs #80
"""

from __future__ import annotations

import yaml
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "litellm_config.yaml"

# Primary deployments route through the OpenRouter provider (default upstream).
OPENROUTER_MODEL_PREFIX = "openrouter/"

# Fallback deployments route through the GitHub Copilot provider.
COPILOT_MODEL_PREFIX = "github_copilot/"

# Primary entries carry an api_key read from the environment.
API_KEY_REF = "os.environ/OPENROUTER_API_KEY"

# Fallback model_names are the primary name + this suffix.
FALLBACK_SUFFIX = "-fallback"


@pytest.fixture
def config():
    """Load and return the parsed litellm_config.yaml.

    Mirrors the fixture in test_streaming_config.py: skips when the file is
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


def _model_list(config):
    """Return a non-empty model_list or fail with a clear message.

    Asserts the value is a list up front: a structurally wrong config (e.g.
    model_list is a mapping or string) would otherwise let the per-entry loops
    iterate unexpected values and fail confusingly, instead of surfacing a clear
    contract violation (CONTEXT.md §1).
    """
    model_list = config.get("model_list", [])
    assert isinstance(model_list, list) and model_list, (
        f"model_list must be a non-empty list; got {type(model_list).__name__} "
        f"({model_list!r}). The proxy has no routes to serve (CONTEXT.md §1)."
    )
    return model_list


def _litellm_params(entry):
    """Return an entry's litellm_params mapping, asserting it is a mapping.

    A structural contract test must fail with a clear message when
    litellm_params is present but not a mapping, rather than raising
    AttributeError from a .get() call on a non-mapping value (CONTEXT.md §1).
    """
    name = entry.get("model_name", "<unnamed>")
    params = entry.get("litellm_params", {})
    assert isinstance(params, dict), (
        f"Model '{name}' has litellm_params of type {type(params).__name__}; "
        f"every entry's litellm_params must be a mapping (CONTEXT.md §1)."
    )
    return params


def _is_fallback(name: str) -> bool:
    """A fallback entry's model_name ends with the fallback suffix."""
    return isinstance(name, str) and name.endswith(FALLBACK_SUFFIX)


class TestModelEntryContract:
    """Every model_list entry must satisfy the CONTEXT.md §1 structural contract."""

    def test_every_entry_has_non_empty_model_name(self, config):
        """A missing/empty model_name means LiteLLM cannot route to the entry."""
        for i, entry in enumerate(_model_list(config)):
            name = entry.get("model_name")
            assert isinstance(name, str) and name, (
                f"model_list[{i}] has model_name={name!r}; every entry must have a "
                f"non-empty model_name (CONTEXT.md §1)."
            )

    def test_primaries_target_openrouter(self, config):
        """Primary (non-fallback) entries must use the openrouter/ prefix.

        OpenRouter is the default upstream. A typo in the prefix routes to the
        wrong LiteLLM provider and fails at runtime with a confusing error
        instead of at PR time.
        """
        for entry in _model_list(config):
            name = entry.get("model_name", "<unnamed>")
            if _is_fallback(name):
                continue
            model = _litellm_params(entry).get("model")
            assert isinstance(model, str) and model.startswith(OPENROUTER_MODEL_PREFIX), (
                f"Primary model '{name}' routes to {model!r}; expected a value "
                f"starting with {OPENROUTER_MODEL_PREFIX!r} (CONTEXT.md §1)."
            )

    def test_fallbacks_target_copilot_with_headers(self, config):
        """Fallback entries must route to github_copilot/ and carry the editor headers."""
        for entry in _model_list(config):
            name = entry.get("model_name", "<unnamed>")
            if not _is_fallback(name):
                continue
            params = _litellm_params(entry)
            model = params.get("model")
            assert isinstance(model, str) and model.startswith(COPILOT_MODEL_PREFIX), (
                f"Fallback model '{name}' routes to {model!r}; expected a value "
                f"starting with {COPILOT_MODEL_PREFIX!r} (CONTEXT.md §1)."
            )
            headers = params.get("extra_headers", {})
            assert isinstance(headers, dict) and headers, (
                f"Fallback model '{name}' has extra_headers={headers!r}; "
                f"every Copilot fallback must carry the four editor headers (CONTEXT.md §1)."
            )

    def test_every_primary_has_a_fallback(self, config):
        """Every primary alias must have a matching fallback wired in router_settings.

        The whole point of the dual-provider setup is resilience: if OpenRouter
        fails, the router falls back to Copilot. A primary without a fallback
        silently loses that resilience.

        LiteLLM expects ``router_settings.fallbacks`` as a LIST of dicts, e.g.
        ``[{"claude-sonnet-4-6": ["claude-sonnet-4-6-fallback"]}]``.
        """
        model_list = _model_list(config)
        fallbacks = config.get("router_settings", {}).get("fallbacks", [])
        assert isinstance(fallbacks, list), (
            "router_settings.fallbacks must be a list of dicts "
            "(LiteLLM format: [{model: [fallback, ...]}])"
        )

        # Flatten the list-of-dicts into a single model -> [fallbacks] map.
        fallback_map: dict[str, list[str]] = {}
        for item in fallbacks:
            assert isinstance(item, dict), (
                f"router_settings.fallbacks item {item!r} is not a dict; "
                f"expected LiteLLM format [{model: [fallback, ...]}]"
            )
            for model, fb_list in item.items():
                fallback_map[model] = fb_list

        for entry in model_list:
            name = entry.get("model_name", "<unnamed>")
            if _is_fallback(name) or name == "*":
                continue
            assert name in fallback_map, (
                f"Primary model '{name}' has no entry in router_settings.fallbacks; "
                f"it will not fall back to Copilot on OpenRouter failure (CONTEXT.md §1)."
            )
            fb_list = fallback_map[name]
            assert isinstance(fb_list, list) and fb_list, (
                f"Primary model '{name}' has an empty fallbacks list (CONTEXT.md §1)."
            )
            expected = f"{name}{FALLBACK_SUFFIX}"
            assert expected in fb_list, (
                f"Primary model '{name}' fallbacks {fb_list!r} do not include the "
                f"expected fallback deployment {expected!r} (CONTEXT.md §1)."
            )


class TestMalformedConfigFailsLoud:
    """Structural breaks must fail with a clear contract message, not AttributeError.

    Guards the round-3 review asks on #83: the helpers assert types up front so a
    malformed litellm_config.yaml yields an actionable failure instead of an
    opaque AttributeError (from .get() on a non-mapping) or a confusing iteration
    over unexpected values.
    """

    @pytest.mark.parametrize("bad", ["not-a-list", {"model_name": "x"}, 42, None, []])
    def test_model_list_must_be_a_non_empty_list(self, bad):
        with pytest.raises(AssertionError, match="model_list must be a non-empty list"):
            _model_list({"model_list": bad})

    def test_litellm_params_must_be_a_mapping(self):
        with pytest.raises(AssertionError, match="must be a mapping"):
            _litellm_params({"model_name": "m", "litellm_params": "oops"})
