"""Unit tests for scripts/probe_selector.py (refs #174).

The probe-worthy alias selector is the single source of truth for which
aliases in litellm_config.yaml are real upstreams vs. degenerate entries
(wildcard '*' and '-fallback' lanes) that would pollute a health report.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import probe_selector  # noqa: E402


class TestIsProbeWorthy:
    def test_primary_alias_is_probe_worthy(self):
        assert probe_selector.is_probe_worthy("claude-sonnet-4-6") is True

    def test_wildcard_is_not_probe_worthy(self):
        assert probe_selector.is_probe_worthy("*") is False

    def test_fallback_lane_is_not_probe_worthy(self):
        assert probe_selector.is_probe_worthy("claude-sonnet-4-6-fallback") is False

    def test_empty_string_is_probe_worthy_by_predicate(self):
        # The predicate itself only excludes '*' and '-fallback'; empty names
        # are filtered by select_probe_aliases (entry.get('model_name') truthy).
        assert probe_selector.is_probe_worthy("") is True


class TestSelectProbeAliases:
    def test_returns_ordered_primary_aliases(self):
        config = {
            "model_list": [
                {"model_name": "claude-sonnet-4-6"},
                {"model_name": "*"},
                {"model_name": "claude-opus-4-6"},
                {"model_name": "claude-sonnet-4-6-fallback"},
            ]
        }
        assert probe_selector.select_probe_aliases(config) == [
            "claude-sonnet-4-6",
            "claude-opus-4-6",
        ]

    def test_wildcard_excluded(self):
        config = {"model_list": [{"model_name": "*"}, {"model_name": "claude-sonnet-4-6"}]}
        assert probe_selector.select_probe_aliases(config) == ["claude-sonnet-4-6"]

    def test_fallback_excluded(self):
        config = {
            "model_list": [
                {"model_name": "claude-sonnet-4-6-fallback"},
                {"model_name": "claude-sonnet-4-6"},
            ]
        }
        assert probe_selector.select_probe_aliases(config) == ["claude-sonnet-4-6"]

    def test_zero_aliases_raises(self):
        # The false-green guard: an empty probe set must fail loudly.
        with pytest.raises(ValueError):
            probe_selector.select_probe_aliases({"model_list": []})

    def test_all_fallback_raises(self):
        with pytest.raises(ValueError):
            probe_selector.select_probe_aliases(
                {"model_list": [{"model_name": "x-fallback"}]}
            )

    def test_missing_model_list_raises(self):
        with pytest.raises(ValueError):
            probe_selector.select_probe_aliases({})

    def test_non_dict_config_raises(self):
        with pytest.raises(ValueError):
            probe_selector.select_probe_aliases([])

    def test_entries_without_model_name_are_skipped(self):
        config = {
            "model_list": [
                {"litellm_params": {}},
                {"model_name": "claude-sonnet-4-6"},
            ]
        }
        assert probe_selector.select_probe_aliases(config) == ["claude-sonnet-4-6"]


class TestMain:
    def test_prints_aliases_from_stdin(self, capsys, monkeypatch):
        import json

        config = {
            "model_list": [
                {"model_name": "claude-sonnet-4-6"},
                {"model_name": "claude-sonnet-4-6-fallback"},
            ]
        }
        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": lambda self: json.dumps(config)})(),
        )
        assert probe_selector.main([]) == 0
        out = capsys.readouterr().out
        assert out.strip() == "claude-sonnet-4-6"

    def test_zero_aliases_exits_1(self, capsys, monkeypatch):
        import json

        monkeypatch.setattr(
            "sys.stdin",
            type("S", (), {"read": lambda self: json.dumps({"model_list": []})})(),
        )
        assert probe_selector.main([]) == 1
        assert "no probe-worthy aliases" in capsys.readouterr().err