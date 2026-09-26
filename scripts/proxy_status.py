#!/usr/bin/env python3
"""Proxy connection-state detection for ``make claude-status``.

Owns the URL resolution / validation / local-vs-hosted classification / health
probe that used to be inline ``python3 -c`` one-liners in the Makefile
``claude-status`` target — the same logic ``claude_enable.py`` expresses as
``validate_base_url`` / ``is_local_base_url``. Convention: the Makefile
orchestrates (file existence, redacted display, spacing); Python does the logic.

stdlib-only. Invoked by the Makefile as::

    python3 scripts/proxy_status.py <settings_file> [fallback_port]

which prints the routing label and proxy health line for a settings file the
Makefile has already displayed (redacted). The optional second argument is the
Makefile's ``$(PORT)`` default — the fallback port used when ANTHROPIC_BASE_URL
is empty and ``.env`` has no LITELLM_PORT. Exits 0 in all cases — a status
report never fails the target.

Refs #81
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from proxy_endpoint import _read_env_port as _read_env_port_shared
from proxy_endpoint import resolve_proxy_endpoint

# Loopback hosts that mean "the proxy runs on this machine".
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
DEFAULT_PORT = "4000"

# The single authoritative manifest of env-var keys that constitute "Claude Code
# is in proxy mode". Shared by claude_enable.py (writes) and claude_disable.py
# (removes) so a key added here is written AND removed together — preventing a
# half-disabled state. See CONTEXT.md §1.
#
# NOTE: OPENROUTER_API_KEY is a proxy-side credential (in .env, used by LiteLLM
# to reach OpenRouter), NOT a Claude Code proxy-mode settings key — it is
# intentionally absent from this manifest.
PROXY_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
)


def resolve_proxy_url(settings, fallback_port=DEFAULT_PORT):
    """Return the configured proxy URL (trailing slash stripped), or ``None``.

    ``None`` means "no proxy configured" (route to the Anthropic API directly):
    either ``settings`` has no dict ``env``, or ``env`` has no
    ``ANTHROPIC_BASE_URL`` key, and no ``PROXY_BASE_URL`` override is set. An
    empty ``ANTHROPIC_BASE_URL`` value falls back to
    ``http://localhost:<fallback_port>`` (mirrors the Makefile's .env-port
    fallback).

    Endpoint resolution delegates to the canonical resolver in
    proxy_endpoint.py (refs #110, #128, #187) so ``PROXY_BASE_URL`` is honored
    exactly as claude_enable.py honors it — ``make claude-status`` and
    ``make claude-enable`` can never disagree about the endpoint. The
    ``None``/coercion mapping below is the only logic kept local: it adapts the
    resolver's always-a-URL output to the status contract (``None`` = direct,
    non-string/whitespace values coerced so validate_proxy_url rejects them
    instead of silently suggesting a local proxy).
    """
    env = settings.get("env") if isinstance(settings, dict) else None
    has_settings_url = isinstance(env, dict) and "ANTHROPIC_BASE_URL" in env
    proxy_base = os.environ.get("PROXY_BASE_URL", "").strip()
    if not has_settings_url and not proxy_base:
        return None
    if has_settings_url:
        raw = env.get("ANTHROPIC_BASE_URL")
        if raw == "":
            # Only an explicit empty string falls back to the local proxy.
            return f"http://localhost:{fallback_port}"
        if not isinstance(raw, str) or not raw.strip():
            # Non-string or whitespace-only values are coerced so
            # validate_proxy_url surfaces them as invalid instead of silently
            # suggesting a local proxy (the canonical resolver would treat
            # them as unset and fall to the localhost default).
            return str(raw).rstrip("/")
    endpoint = resolve_proxy_endpoint(settings=settings, env_file=".env")
    return endpoint.url


def validate_proxy_url(url):
    """True if ``url`` is an absolute http(s) URL with a netloc and hostname."""
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and bool(parsed.hostname)


def classify_proxy(url):
    """Classify a proxy URL as ``local`` (loopback), ``hosted``, or ``direct``.

    ``direct`` is returned for a falsy URL (no proxy configured).
    """
    if not url:
        return "direct"
    host = urlparse(url).hostname
    return "local" if host in LOCAL_HOSTS else "hosted"


def probe_health(url, timeout=3):
    """True if ``GET <url>/health/readiness`` succeeds (``curl -sf``), else False.

    Any failure — non-zero exit, missing curl, or timeout — yields False so a
    status report never raises.
    """
    try:
        result = subprocess.run(
            ["curl", "-sf", f"{url}/health/readiness"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def read_fallback_port(env_path=".env", default=DEFAULT_PORT):
    """Read ``LITELLM_PORT`` from a .env file, defaulting when absent/unreadable.

    Delegates to the canonical resolver in proxy_endpoint.py (refs #110, #128)
    so the .env port-parsing logic lives in exactly one place. Tolerates a
    non-UTF-8 .env and matches only the exact ``LITELLM_PORT`` assignment.
    """
    return _read_env_port_shared(env_path, default=default)


def render_status(settings, fallback_port=DEFAULT_PORT, probe=probe_health):
    """Return the routing + health lines for a parsed settings mapping.

    ``probe`` is injectable so tests exercise the rendering without a network
    call. Returns a list of lines (no trailing newlines).
    """
    url = resolve_proxy_url(settings, fallback_port)
    if url is None:
        return ["🌐 Routing: Anthropic API directly"]
    if not validate_proxy_url(url):
        return [f"❌ Proxy URL in settings is invalid: {url}"]

    if classify_proxy(url) == "local":
        lines = ["🔗 Routing: local proxy"]
        hint = "run 'make start'"
    else:
        lines = ["🔗 Routing: hosted proxy"]
        hint = "check the hosted proxy endpoint"

    if probe(url):
        lines.append(f"✅ Proxy: running at {url}")
    else:
        lines.append(f"❌ Proxy: not running at {url} — {hint}")
    return lines


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        return 0  # No settings file provided — nothing to report.
    # Optional argv[2] = the Makefile's $(PORT) default, so `make claude-status
    # PORT=XXXX` still controls the fallback when .env lacks LITELLM_PORT.
    make_default_port = argv[2] if len(argv) > 2 and argv[2] else DEFAULT_PORT
    try:
        with open(argv[1]) as f:
            settings = json.load(f)
    except (OSError, json.JSONDecodeError):
        # The Makefile handles parse/read errors before calling us; stay silent
        # and successful rather than double-reporting.
        return 0
    for line in render_status(settings, read_fallback_port(default=make_default_port)):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
