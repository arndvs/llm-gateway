#!/usr/bin/env python3
"""Generate litellm_config.yaml from a compact model-mapping definition.

Makes the "Copilot model route" a single-source abstraction (refs #108): the
four editor headers, the `stream: true` flag, and the provider prefixes are
defined once here, and each model alias is a one-line mapping edit. The
generated YAML is committed (not generated at runtime) so the proxy works
without running this script first.

The committed litellm_config.yaml must match this generator's output exactly —
`tests/test_config_generator.py` fails CI on drift (refs #130).

Usage:
    python3 scripts/generate_config.py            # print to stdout
    python3 scripts/generate_config.py -o litellm_config.yaml   # write in place
    make generate-config                          # regenerate + verify no drift

Refs #108, #130
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

# ── Single-source shared properties ──────────────────────────────────────────
# The four editor headers Copilot validates. Defined once; every Copilot
# fallback route inherits them. Bumping a header version is a one-line edit here.
EDITOR_HEADERS = {
    "Editor-Version": "vscode/1.106.3",
    "Editor-Plugin-Version": "copilot/1.388.0",
    "Copilot-Integration-Id": "vscode-chat",
    "User-Agent": "GithubCopilot/1.388.0",
}

# Provider prefixes for the dual-provider structure.
COPILOT_PREFIX = "github_copilot/"
OPENROUTER_PREFIX = "openrouter/"

# Primary entries carry the OpenRouter key from the environment.
OPENROUTER_API_KEY_REF = "os.environ/OPENROUTER_API_KEY"

# Fallback model_names are the primary name + this suffix.
FALLBACK_SUFFIX = "-fallback"

# ── Model mapping (the only thing that varies per route) ─────────────────────
# alias → {primary: openrouter/<model>, fallback: github_copilot/<model>}
# OpenRouter is the default upstream: it serves real completions reliably,
# while Copilot's Claude models return empty-200 ('no choices') responses that
# LiteLLM treats as success (no fallback). Copilot remains the automatic
# fallback lane per alias. All Copilot fallbacks route to claude-sonnet-5 —
# the current, available Copilot model (claude-opus-4.6 was removed from the
# vscode-chat integrator's model list on 2026-08-24).
# Adding a model is a one-line edit here; regenerate and commit.
MODEL_MAPPING = {
    "claude-sonnet-4-6": {
        "primary": f"{OPENROUTER_PREFIX}deepseek/deepseek-v4-flash-0731",
        "fallback": f"{COPILOT_PREFIX}claude-sonnet-5",
    },
    "claude-haiku-4-5-20251001": {
        "primary": f"{OPENROUTER_PREFIX}deepseek/deepseek-v4-flash-0731",
        "fallback": f"{COPILOT_PREFIX}claude-sonnet-5",  # Copilot has no Haiku
    },
    "claude-opus-4-6": {
        "primary": f"{OPENROUTER_PREFIX}deepseek/deepseek-v4-flash-0731",
        "fallback": f"{COPILOT_PREFIX}claude-sonnet-5",
    },
    "claude-opus-4-7": {
        "primary": f"{OPENROUTER_PREFIX}deepseek/deepseek-v4-flash-0731",
        "fallback": f"{COPILOT_PREFIX}claude-sonnet-5",
    },
}


def _primary_entry(alias: str, primary: str) -> dict:
    """Build a primary (OpenRouter) model_list entry with the API key."""
    return {
        "model_name": alias,
        "litellm_params": {
            "model": primary,
            "api_key": OPENROUTER_API_KEY_REF,
            "stream": True,
        },
    }


def _fallback_entry(alias: str, fallback: str) -> dict:
    """Build a fallback (Copilot) model_list entry with the editor headers."""
    return {
        "model_name": f"{alias}{FALLBACK_SUFFIX}",
        "litellm_params": {
            "model": fallback,
            "stream": True,
            "extra_headers": dict(EDITOR_HEADERS),
        },
    }


def _fallbacks(mapping: dict) -> list[dict]:
    """Build router_settings.fallbacks as a list of dicts (LiteLLM format)."""
    return [
        {alias: [f"{alias}{FALLBACK_SUFFIX}"]}
        for alias in mapping
    ]


def build_config() -> dict:
    """Assemble the full litellm_config.yaml data structure."""
    model_list: list[dict] = []
    for alias, targets in MODEL_MAPPING.items():
        model_list.append(_primary_entry(alias, targets["primary"]))
        model_list.append(_fallback_entry(alias, targets["fallback"]))

    return {
        "litellm_settings": {
            "drop_params": True,
            "num_retries": 3,
            "request_timeout": 120,
            "additional_drop_params": ["response_format", "thinking"],
            "json_logs": True,
            "callbacks": [
                "litellm_logger.proxy_handler_instance",
                "health_version.version_callback_instance",
            ],
        },
        "model_list": model_list,
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY",
        },
        "router_settings": {
            "num_retries": 3,
            "retry_after": 2,
            "allowed_fails": 3,
            "cooldown_time": 30,
            "fallbacks": _fallbacks(MODEL_MAPPING),
        },
    }


def generate_config() -> str:
    """Return the full litellm_config.yaml as a YAML string."""
    return yaml.safe_dump(
        build_config(),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate litellm_config.yaml from the model mapping."
    )
    parser.add_argument(
        "-o",
        "--output",
        metavar="PATH",
        help="write to PATH instead of stdout (default: stdout)",
    )
    args = parser.parse_args(argv)

    text = generate_config()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
