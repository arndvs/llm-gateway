#!/usr/bin/env bash
# test_probe_fetch_pin.sh — probe-fetch pin contract (refs #194)
#
# The proxy health probes must fetch their probe engine from the hub at the
# PINNED SHA (.sandcastle/hub-version.json lastPinnedSha) — the same drift gate
# every other hub artifact rides — never a bare floating `main` URL.
#
# Checks:
#   1. The lock file exists, is parseable, and has a non-empty lastPinnedSha
#   2. Both workflows invoke the shared fetch script (no inline curl to main)
#   3. No workflow references ctrlshft-hub/main anymore
#   4. The fetch script reads the lock and fails loudly when it's missing
#
# Usage: bash tests/test_probe_fetch_pin.sh
#
# Refs #194

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

LOCK=".sandcastle/hub-version.json"
FETCH="scripts/fetch_probe_engine.sh"

# ── Test 1: lock file is parseable with non-empty lastPinnedSha ─
echo "Test 1: hub-version.json lock is parseable with lastPinnedSha"
if python3 -c "
import json
d = json.load(open('$LOCK', encoding='utf-8'))
sha = (d.get('lastPinnedSha') or '').strip()
assert sha, 'lastPinnedSha is empty'
print(sha)
" >/dev/null 2>&1; then
    pass "lock file has non-empty lastPinnedSha"
else
    fail "lock file missing/malformed/empty lastPinnedSha"
fi

# ── Test 2: both workflows invoke the shared fetch script ──────
echo "Test 2: both workflows invoke the shared fetch script"
if grep -q 'fetch_probe_engine.sh' .github/workflows/proxy-canary.yml \
   && grep -q 'fetch_probe_engine.sh' .github/workflows/model-health.yml; then
    pass "both workflows call scripts/fetch_probe_engine.sh"
else
    fail "a workflow does not invoke the shared fetch script"
fi

# ── Test 3: no workflow references ctrlshft-hub/main ───────────
echo "Test 3: no workflow references ctrlshft-hub/main"
if ! grep -rn "ctrlshft-hub/main" .github/workflows/ 2>/dev/null; then
    pass "no bare main URL in workflows"
else
    fail "a workflow still references ctrlshft-hub/main"
fi

# ── Test 4: fetch script reads the lock and fails loudly ───────
echo "Test 4: fetch script reads the lock and fails loudly when missing"
if grep -q 'lastPinnedSha' "$FETCH" && grep -q 'hub-version.json' "$FETCH"; then
    pass "fetch script reads lastPinnedSha from the lock"
else
    fail "fetch script does not read the lock"
fi
# Missing lock → non-zero exit with a clear message. The fetch script resolves
# REPO_ROOT from its own location, so simulate a broken install by copying the
# script into a temp dir with no .sandcastle/hub-version.json sibling. The
# error line goes to stderr; capture both streams. NOTE: the fetch script
# exits 1 by design, so under pipefail the pipeline exit is 1 even when grep
# matches — assert on the grep output, not the pipeline status.
TMP_REPO=$(mktemp -d)
mkdir -p "$TMP_REPO/scripts"
cp "$FETCH" "$TMP_REPO/scripts/"
if (cd "$TMP_REPO" && bash scripts/fetch_probe_engine.sh "$TMP_REPO/out" 2>&1 || true) | grep -q "hub-version.json"; then
    pass "fetch script fails loudly on a missing lock"
else
    fail "fetch script does not fail loudly on a missing lock"
fi
rm -rf "$TMP_REPO"

# ── Test 5: fetch script resolves the pinned SHA from the lock ─
echo "Test 5: fetch script resolves the pinned SHA from the lock"
PINNED_SHA=$(python3 -c "
import json
print(json.load(open('$LOCK', encoding='utf-8'))['lastPinnedSha'])
")
if grep -q "raw.githubusercontent.com/arndvs/ctrlshft-hub/\${PINNED_SHA}" "$FETCH"; then
    pass "fetch script curls the hub at \${PINNED_SHA}"
else
    fail "fetch script does not curl the pinned SHA"
fi

echo ""
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]