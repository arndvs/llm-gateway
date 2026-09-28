#!/usr/bin/env bash
# fetch_probe_from_hub.sh — fetch the probe engine from the hub at the pinned SHA.
#
# The proxy health probes (proxy-canary.yml, model-health.yml) are the boundary
# where this repo's CI measures its own production proxy. Their probe engine
# must ride the SAME drift gate as every other hub artifact — a pinned SHA from
# .sandcastle/hub-version.json — not a bare floating `main` URL (refs #194).
#
# Reads .sandcastle/hub-version.json → lastPinnedSha, fails loudly when the
# lock is missing/malformed, and fetches probe_completion.sh (+ probe_parser.py
# when requested) from the hub at that SHA. Records the fetched SHA in the step
# summary so a post-hoc audit can tie what ran to the reviewed pin.
#
# Usage:
#   bash scripts/fetch_probe_from_hub.sh [--with-parser] [dest_dir]
#
#   --with-parser  also fetch probe_parser.py (required by probe_completion.sh
#                  since refs #166)
#   dest_dir       where to write the scripts (default: $RUNNER_TEMP or /tmp)
#
# Refs #194
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

WITH_PARSER=0
DEST_DIR="${RUNNER_TEMP:-/tmp}"
for arg in "$@"; do
  case "$arg" in
    --with-parser) WITH_PARSER=1 ;;
    *) DEST_DIR="$arg" ;;
  esac
done

LOCK="$REPO_ROOT/.sandcastle/hub-version.json"
if [ ! -f "$LOCK" ]; then
  echo "❌ hub-version.json lock not found at $LOCK — run the drift gate first" >&2
  exit 1
fi

# Parse lastPinnedSha with python3 (stdlib json) — fails loudly on malformed.
# The lock path is passed via env (LOCK_PATH) so Windows Python can open the
# Git Bash /c/... path without path-translation issues.
PINNED_SHA="$(LOCK_PATH="$LOCK" python3 -c "
import json, os, sys
lock = os.environ['LOCK_PATH']
try:
    d = json.load(open(lock, encoding='utf-8'))
except Exception as exc:
    print(f'could not parse lock: {exc}', file=sys.stderr)
    sys.exit(1)
sha = (d.get('lastPinnedSha') or '').strip()
if not sha:
    print('lock has no lastPinnedSha', file=sys.stderr)
    sys.exit(1)
print(sha)
")" || exit 1

mkdir -p "$DEST_DIR"

curl -fsSL "https://raw.githubusercontent.com/arndvs/ctrlshft-hub/${PINNED_SHA}/templates/scripts/probe_completion.sh" \
  -o "$DEST_DIR/probe_completion.sh"
chmod +x "$DEST_DIR/probe_completion.sh"

if [ "$WITH_PARSER" = "1" ]; then
  curl -fsSL "https://raw.githubusercontent.com/arndvs/ctrlshft-hub/${PINNED_SHA}/templates/scripts/probe_parser.py" \
    -o "$DEST_DIR/probe_parser.py"
  chmod +x "$DEST_DIR/probe_parser.py"
fi

# Audit trail: record the fetched SHA + lock location in the step summary.
{
  echo "Fetched probe engine from ctrlshft-hub at pinned SHA \`${PINNED_SHA}\`"
  echo "Lock: \`.sandcastle/hub-version.json\` (lastPinnedSha)"
  echo "Scripts: \`probe_completion.sh\`$([ "$WITH_PARSER" = "1" ] && echo ", \`probe_parser.py\`")"
} >> "${GITHUB_STEP_SUMMARY:-/dev/null}" 2>/dev/null || true

echo "✅ Fetched probe engine from hub at ${PINNED_SHA} → $DEST_DIR" >&2