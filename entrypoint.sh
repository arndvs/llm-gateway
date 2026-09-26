#!/usr/bin/env bash
# entrypoint.sh — container entrypoint for the llm-gateway proxy image.
#
# Enforces the DB-mode boundary (refs #169): the image default is DB-LESS. If
# DATABASE_URL is set, a reachable Postgres must exist — otherwise LiteLLM
# would enter DB mode and serve 400 "No connected db" on every request while
# looking fully healthy. Fail fast at container start instead.
#
# The check runs at START, never at build — the image build stays hermetic and
# the DB-less default image works exactly as before.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# DB-mode guard: exits non-zero with the canonical error when DATABASE_URL is
# set but no reachable database exists. DB-less (unset) always passes.
PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}" \
  python3 "$SCRIPT_DIR/scripts/db_mode_guard.py" || exit 1

# Pass through explicit commands (e.g. `docker run image printenv BUILD_SHA`)
# so the image stays introspectable; the guard above still runs first.
if [ "$#" -gt 0 ]; then
  exec "$@"
fi

# The canonical LiteLLM proxy invocation (mirrors the previous Dockerfile CMD).
exec litellm --config litellm_config.yaml --port 4000 --host 0.0.0.0