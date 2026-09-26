#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory so .env and config are found regardless of cwd
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Load .env if present
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091  # .env is user-supplied at runtime; not available at lint time
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Shared proxy-launch function (refs #115, #127) — single source of truth for
# the canonical `uv run` command, version pin, and env requirements.
# shellcheck disable=SC1091  # sourced from the repo, not available at lint time
source "$SCRIPT_DIR/scripts/_launch_proxy.sh"

# Effective port comes from the canonical resolver (refs #195) — the same
# precedence chain claude_enable.py uses — not a hand-parsed LITELLM_PORT.
PORT="$(resolve_proxy_port)"

echo "Starting LiteLLM proxy (OpenRouter primary, GitHub Copilot fallback) on port ${PORT}..."
echo ""
echo "After the proxy starts, configure Claude Code:"
echo ""
echo "  make claude-enable"
echo ""
echo "  — or manually set these env vars:"
echo ""
echo "  ANTHROPIC_BASE_URL=http://localhost:${PORT}"
echo "  ANTHROPIC_AUTH_TOKEN=<set to your LITELLM_MASTER_KEY>"
echo "  CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"
echo ""

# exec so the shell is replaced by the proxy process (correct for Docker/systemd).
exec launch_proxy "$PORT" "$SCRIPT_DIR/litellm_config.yaml"
