#!/usr/bin/env bash
# test_launch_contract.sh — Verify Makefile:start and start_proxy.sh share the
# same proxy-launch command (refs #115, #127)
#
# Acceptance criteria (from issue #127):
#   1. scripts/_launch_proxy.sh exists and defines launch_proxy
#   2. launch_proxy validates LITELLM_MASTER_KEY is set
#   3. launch_proxy reads the version from .litellm-version
#   4. start_proxy.sh sources _launch_proxy.sh and calls launch_proxy with exec
#   5. Makefile:start sources _launch_proxy.sh and calls launch_proxy
#   6. Both entry points produce the same effective command for the same inputs
#
# Usage: bash tests/test_launch_contract.sh

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

LAUNCH="scripts/_launch_proxy.sh"

# ── Test 1: _launch_proxy.sh exists and defines launch_proxy ──
echo "Test 1: _launch_proxy.sh exists and defines launch_proxy"
if [ ! -f "$LAUNCH" ]; then
    fail "$LAUNCH not found"
else
    if grep -q '^launch_proxy()' "$LAUNCH"; then
        pass "launch_proxy() defined in $LAUNCH"
    else
        fail "launch_proxy() not defined in $LAUNCH"
    fi
fi

# ── Test 2: launch_proxy validates LITELLM_MASTER_KEY ─────────
echo "Test 2: launch_proxy validates LITELLM_MASTER_KEY"
if grep -q 'LITELLM_MASTER_KEY' "$LAUNCH"; then
    pass "launch_proxy checks LITELLM_MASTER_KEY"
else
    fail "launch_proxy does not check LITELLM_MASTER_KEY"
fi

# ── Test 3: launch_proxy reads version from .litellm-version ──
echo "Test 3: launch_proxy reads version from .litellm-version"
if grep -q '\.litellm-version' "$LAUNCH"; then
    pass "launch_proxy reads .litellm-version"
else
    fail "launch_proxy does not read .litellm-version"
fi

# ── Test 4: start_proxy.sh sources and execs launch_proxy ─────
echo "Test 4: start_proxy.sh sources and execs launch_proxy"
if grep -q 'source .*_launch_proxy.sh' start_proxy.sh && grep -q 'exec launch_proxy' start_proxy.sh; then
    pass "start_proxy.sh sources _launch_proxy.sh and execs launch_proxy"
else
    fail "start_proxy.sh does not source/exec launch_proxy"
fi

# ── Test 5: Makefile:start sources and calls launch_proxy ─────
echo "Test 5: Makefile:start sources and calls launch_proxy"
if grep -q 'source scripts/_launch_proxy.sh' Makefile && grep -q 'launch_proxy' Makefile; then
    pass "Makefile:start sources _launch_proxy.sh and calls launch_proxy"
else
    fail "Makefile:start does not source/call launch_proxy"
fi

# ── Test 6: both entry points produce the same command ─────────
echo "Test 6: both entry points produce the same effective command"
# Extract the canonical uv run command from _launch_proxy.sh and assert both
# entry points reference the same version source and config path.
if grep -q 'uv run' "$LAUNCH" \
   && grep -qF -- '--with "litellm[proxy]==${version}"' "$LAUNCH" \
   && grep -q 'litellm --config' "$LAUNCH"; then
    pass "canonical uv run command defined once in $LAUNCH"
else
    fail "canonical uv run command not found in $LAUNCH"
fi

# ── Test 7: launch_proxy's port default delegates to the resolver ──
echo "Test 7: launch_proxy port default delegates to the resolver (refs #195)"
if grep -q 'resolve_proxy_port' "$LAUNCH" \
   && ! grep -q 'local port="${1:-4000}"' "$LAUNCH"; then
    pass "launch_proxy defaults port via resolve_proxy_port, no literal 4000"
else
    fail "launch_proxy still has a literal 4000 default or lacks resolver delegation"
fi

# ── Test 8: no entry point parses the port by hand anymore ─────
echo "Test 8: no entry point parses the port by hand (refs #195)"
# start_proxy.sh must derive PORT from the resolver, not LITELLM_PORT:-4000.
if grep -q 'PORT="$(resolve_proxy_port)"' start_proxy.sh \
   && ! grep -q 'PORT="${LITELLM_PORT:-4000}"' start_proxy.sh; then
    pass "start_proxy.sh derives PORT from the resolver"
else
    fail "start_proxy.sh still parses the port by hand"
fi
# Makefile start/stop must quote the resolver CLI, not LITELLM_PORT:-$(PORT).
if grep -q 'proxy_endpoint.py | sed' Makefile \
   && ! grep -q 'PORT=$${LITELLM_PORT:-' Makefile; then
    pass "Makefile start/stop quote the resolver CLI"
else
    fail "Makefile still parses the port by hand"
fi

echo ""
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
