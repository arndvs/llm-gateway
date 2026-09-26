#!/usr/bin/env python3
"""Probe-worthy alias selection for the proxy health probes (refs #174).

Owns the question "which aliases in litellm_config.yaml are real, probe-worthy
upstreams?" — the single source of truth for the wildcard/fallback exclusion
that model-health and canary must agree on (the lesson of refs #148).

Two concepts:

- ``is_probe_worthy(name)`` — the canonical predicate: concrete, non-wildcard,
  non-fallback (``name != "*" and not name.endswith("-fallback")``).
- ``select_probe_aliases(config_data)`` — ordered probe-worthy ``model_name``s
  from a parsed ``litellm_config.yaml``, raising ``ValueError`` when the parse
  yields zero (preserving the model-health false-green guard).

stdlib-only. No network, no subprocess.
"""

from __future__ import annotations

from typing import List

# The suffix generate_config.py uses to build fallback lanes. Imported from the
# generator so the selector and the generator can never disagree about what a
# fallback lane is named (refs #174).
try:
    from generate_config import FALLBACK_SUFFIX
except ImportError:  # pragma: no cover
    FALLBACK_SUFFIX = "-fallback"


def is_probe_worthy(name: str) -> bool:
    """True if ``name`` is a concrete, probe-worthy model alias.

    Excludes the wildcard ``*`` (routes to every model — not a concrete
    upstream) and any ``-fallback`` lane (routes to non-concrete Copilot
    models that return HTTP 400 and pollute model-health reports).
    """
    return name != "*" and not name.endswith(FALLBACK_SUFFIX)


def select_probe_aliases(config_data: dict) -> List[str]:
    """Return the ordered probe-worthy ``model_name``s from parsed config.

    ``config_data`` is a parsed ``litellm_config.yaml`` mapping (as returned by
    ``yaml.safe_load``). Raises ``ValueError`` when the parse yields zero
    probe-worthy aliases — the model-health false-green guard: an empty probe
    set must fail loudly, not sail through as "all healthy".
    """
    model_list = config_data.get("model_list", []) if isinstance(config_data, dict) else []
    aliases = [
        entry.get("model_name")
        for entry in model_list
        if isinstance(entry, dict) and entry.get("model_name")
    ]
    probe_worthy = [name for name in aliases if is_probe_worthy(name)]
    if not probe_worthy:
        raise ValueError(
            "no probe-worthy aliases found in model_list "
            "(all wildcard '*' or '-fallback' lanes, or empty model_list)"
        )
    return probe_worthy


def main(argv: List[str] | None = None) -> int:
    """CLI: print probe-worthy aliases from parsed config on stdin.

    Usage: python3 scripts/probe_selector.py < <parsed_config.json>
    Prints one alias per line. Exits 1 with a message on zero probe-worthy
    aliases (the false-green guard). The caller parses the YAML (the workflow
    already has PyYAML); this module stays stdlib-only.
    """
    import json
    import sys

    argv = argv if argv is not None else sys.argv
    try:
        config_data = json.load(sys.stdin)
    except Exception as exc:
        print(f"probe_selector: could not parse stdin as JSON: {exc}", file=sys.stderr)
        return 1
    try:
        aliases = select_probe_aliases(config_data)
    except ValueError as exc:
        print(f"probe_selector: {exc}", file=sys.stderr)
        return 1
    for alias in aliases:
        print(alias)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())