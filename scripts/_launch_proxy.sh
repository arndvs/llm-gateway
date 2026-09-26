#!/usr/bin/env bash
# _launch_proxy.sh — shared proxy-launch function (refs #115, #127)
#
# Single source of truth for assembling the canonical `uv run` command that
# starts the LiteLLM proxy. Both `Makefile:start` and `start_proxy.sh` source
# this file and call `launch_proxy`, so the command assembly, env requirements,
# and version pin live in exactly one place.
#
# This file is meant to be SOURCED, not executed. It defines `launch_proxy`
# and does not run anything on its own.

# Repo root is the parent of this file's directory (scripts/), resolved from the
# file's own location so it works regardless of cwd or how it is sourced.
_LAUNCH_PROXY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_PROXY_ROOT="$(cd "$_LAUNCH_PROXY_DIR/.." && pwd)"

# resolve_proxy_port — quote the canonical endpoint resolver (refs #195).
#
# The proxy's own port question has one owner: scripts/proxy_endpoint.py
# (precedence PROXY_BASE_URL → settings ANTHROPIC_BASE_URL → LITELLM_PORT/.env
# → localhost:4000). Every launcher delegates here instead of parsing the port
# by hand, so what gets launched and what claude-enable configures derive from
# the same chain. PYTHONPATH is set so the import resolves from the repo root
# regardless of cwd.
resolve_proxy_port() {
  PYTHONPATH="$LAUNCH_PROXY_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 -c 'from proxy_endpoint import resolve_proxy_endpoint; print(resolve_proxy_endpoint(env_file=".env").port)'
}

# assert_db_mode_ok — enforce the DB-mode boundary (refs #169).
#
# DATABASE_URL set requires a reachable Postgres; otherwise LiteLLM enters DB
# mode and serves 400 "No connected db" on every request while looking healthy.
# Local launch (make start / start_proxy.sh) fails fast with the same canonical
# error as the container entrypoint.
assert_db_mode_ok() {
  PYTHONPATH="$LAUNCH_PROXY_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$LAUNCH_PROXY_ROOT/scripts/db_mode_guard.py" || return 1
}

# launch_proxy [port] [config_path]
#
# Validates LITELLM_MASTER_KEY is set, reads the LiteLLM version from
# .litellm-version, and runs the canonical `uv run` command. Callers choose
# exec-vs-subshell semantics by how they invoke it:
#   - start_proxy.sh:  exec launch_proxy "$PORT" "$CONFIG"   (replaces shell)
#   - Makefile:start:  launch_proxy "$PORT" "$CONFIG"        (stays in subshell)
#
# The port defaults to the canonical resolver's answer (refs #195) so the
# launch path quotes the same precedence chain as claude_enable.py — the port
# the proxy starts on and the URL claude-enable writes can never diverge.
launch_proxy() {
  local port="${1:-$(resolve_proxy_port)}"
  local config_path="${2:-$LAUNCH_PROXY_ROOT/litellm_config.yaml}"

  if [[ -z "${LITELLM_MASTER_KEY:-}" ]]; then
    echo "❌ LITELLM_MASTER_KEY not set. Run 'make setup' or create .env first." >&2
    return 1
  fi

  # DB-mode boundary (refs #169): fail fast on a stray DATABASE_URL with no
  # reachable Postgres, instead of surfacing confusing 400s on the first test.
  if ! assert_db_mode_ok; then
    return 1
  fi

  local version
  version="$(cat "$LAUNCH_PROXY_ROOT/.litellm-version" 2>/dev/null || echo '')"
  if [[ -z "$version" ]]; then
    echo "❌ Could not read LiteLLM version from $LAUNCH_PROXY_ROOT/.litellm-version" >&2
    echo "   Create it (e.g. 'echo 1.89.1 > .litellm-version') before starting." >&2
    return 1
  fi

  echo "Starting LiteLLM proxy (OpenRouter primary, GitHub Copilot fallback) on port ${port}..."
  echo ""

  UV_NATIVE_TLS="${UV_NATIVE_TLS:-true}" \
    PYTHONPATH="$LAUNCH_PROXY_ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    uv run \
    --with "litellm[proxy]==${version}" \
    litellm --config "${config_path}" --port "${port}"
}
