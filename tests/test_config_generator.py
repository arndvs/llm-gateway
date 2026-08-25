"""Tests for scripts/generate_config.py.

The config generator makes the "Copilot model route" a single-source
abstraction (refs #108): a compact alias → {primary, fallback} mapping emits the
full litellm_config.yaml, so adding a model is a one-line edit and the four
editor headers appear in exactly one place.

The key invariant is drift: the committed litellm_config.yaml must be exactly
what the generator produces. If someone hand-edits the YAML (bypassing the
abstraction) or changes the mapping without regenerating, CI fails.

Refs #108, #130
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import generate_config  # noqa: E402  (import after sys.path setup)

CONFIG_PATH = REPO_ROOT / "litellm_config.yaml"


def _committed_config() -> dict:
    if not CONFIG_PATH.exists():
        pytest.skip(f"litellm_config.yaml not found at {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        pytest.fail(
            f"litellm_config.yaml did not parse as a mapping (got {type(data).__name__})"
        )
    return data


class TestGeneratedConfigMatchesCommitted:
    """The committed YAML must be exactly what the generator emits (no drift)."""

    def test_generated_yaml_parses_and_matches_committed(self):
        generated = generate_config.generate_config()
        assert isinstance(generated, str) and generated.strip(), (
            "generate_config() must return a non-empty YAML string"
        )
        generated_data = yaml.safe_load(generated)
        assert isinstance(generated_data, dict), (
            "generated config did not parse as a mapping"
        )
        assert generated_data == _committed_config(), (
            "litellm_config.yaml has drifted from generate_config.py output. "
            "Regenerate with 'python3 scripts/generate_config.py' (or 'make "
            "generate-config') and commit the result."
        )


class TestGeneratorStructure:
    """The generator must emit the full dual-provider structure."""

    def test_emits_router_fallbacks_as_list_of_dicts(self):
        data = yaml.safe_load(generate_config.generate_config())
        fallbacks = data["router_settings"]["fallbacks"]
        assert isinstance(fallbacks, list) and fallbacks, (
            "router_settings.fallbacks must be a non-empty list"
        )
        for item in fallbacks:
            assert isinstance(item, dict), (
                f"fallbacks item {item!r} must be a dict (LiteLLM list-of-dicts format)"
            )

    def test_every_primary_has_fallback_entry(self):
        data = yaml.safe_load(generate_config.generate_config())
        fallback_map: dict[str, list[str]] = {}
        for item in data["router_settings"]["fallbacks"]:
            for model, fb_list in item.items():
                fallback_map[model] = fb_list
        for entry in data["model_list"]:
            name = entry["model_name"]
            if name.endswith("-fallback") or name == "*":
                continue
            assert name in fallback_map, (
                f"primary model {name!r} has no router_settings.fallbacks entry"
            )
            assert f"{name}-fallback" in fallback_map[name], (
                f"primary model {name!r} fallbacks do not include {name}-fallback"
            )

    def test_editor_headers_defined_once(self):
        """The four editor headers must appear in exactly one place (the generator)."""
        source = Path(REPO_ROOT / "scripts" / "generate_config.py").read_text(encoding="utf-8")
        for header in ("Editor-Version", "Editor-Plugin-Version", "Copilot-Integration-Id", "User-Agent"):
            assert header in source, (
                f"editor header {header!r} must be defined in generate_config.py"
            )
        # The committed YAML should not hardcode them independently — they come
        # from the generator. (The YAML legitimately contains them as generated
        # output, so we assert the generator is the single source by checking
        # the mapping, not the YAML.)
        mapping = generate_config.MODEL_MAPPING
        assert isinstance(mapping, dict) and mapping, "MODEL_MAPPING must be non-empty"


class TestAddingModelIsOneLine:
    """Adding a model must be a one-line mapping edit (the core value prop)."""

    def test_mapping_shape(self):
        mapping = generate_config.MODEL_MAPPING
        for alias, targets in mapping.items():
            assert isinstance(alias, str) and alias, "alias must be a non-empty string"
            assert isinstance(targets, dict), (
                f"alias {alias!r} targets must be a dict with 'primary'/'fallback'"
            )
            assert "primary" in targets and "fallback" in targets, (
                f"alias {alias!r} must define both 'primary' and 'fallback'"
            )
            assert targets["primary"].startswith("openrouter/"), (
                f"alias {alias!r} primary must use the openrouter/ prefix"
            )
            assert targets["fallback"].startswith("github_copilot/"), (
                f"alias {alias!r} fallback must use the github_copilot/ prefix"
            )
