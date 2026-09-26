#!/usr/bin/env bash
# test_context_md.sh — Validates CONTEXT.md structure and size budget
#
# Checks:
#   1. CONTEXT.md exists at repo root
#   2. Contains all four required architecture section headings
#   3. Is under ~5500 tokens (bytes/4 heuristic; see .sandcastle/scripts/check-file-tokens.sh)
#   4. Mentions key required facts (lightweight guard against drift)

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

FILE="CONTEXT.md"

# ── Test 1: File exists ───────────────────────────────────────
echo "Test 1: CONTEXT.md exists at repo root"
if [ -f "$FILE" ]; then
    pass "CONTEXT.md exists"
else
    fail "CONTEXT.md not found"
    echo "Results: $PASS passed, $FAIL failed"
    exit 1
fi

# ── Test 2: Four required sections ────────────────────────────
echo "Test 2: Contains all four architecture sections"

# Section 1: LiteLLM→Copilot routing (with OpenRouter fallback)
if grep -Eq '^##[[:space:]]+1\.[[:space:]]+LiteLLM.*routing' "$FILE"; then
    pass "Section: LiteLLM→Copilot routing present"
else
    fail "Section: LiteLLM→Copilot routing missing"
fi

# Section 2: DB-less default mode
if grep -Eq '^##[[:space:]]+2\.[[:space:]]+DB-less default mode' "$FILE"; then
    pass "Section: DB-less default mode present"
else
    fail "Section: DB-less default mode missing"
fi

# Section 3: PROXY_LOG observability
if grep -Eq '^##[[:space:]]+3\.[[:space:]]+Observability' "$FILE"; then
    pass "Section: PROXY_LOG observability present"
else
    fail "Section: PROXY_LOG observability missing"
fi

# Section 4: CI workflows
if grep -Eq '^##[[:space:]]+4\.[[:space:]]+CI workflows' "$FILE"; then
    pass "Section: CI workflows present"
else
    fail "Section: CI workflows missing"
fi

# ── Test 3: Token limit ──────────────────────────────────────
echo "Test 3: Under 5500 tokens"
bytes=$(wc -c < "$FILE")
tokens=$((bytes / 4))
if [ "$tokens" -le 5500 ]; then
    pass "Token count: ~${tokens} (limit 5500)"
else
    fail "Token count: ~${tokens} exceeds 5500 limit"
fi

# ── Test 4: Drift guard (phrase checks) ─────────────────────
echo "Test 4: Drift guard (phrase checks)"

# 4a: References the OpenRouter provider and fallback architecture
if grep -qi "openrouter" "$FILE" && grep -qi "fallback" "$FILE"; then
    pass "References OpenRouter provider and fallback"
else
    fail "Missing OpenRouter provider or fallback reference"
fi

# 4b: No wildcard — openrouter/* is not a concrete model (400 no_db_connection)
# and github_copilot/* silently passes unknown models through; every alias has an
# explicit concrete fallback, unknown models fail loudly.
if grep -Eq 'No wildcard|no wildcard|fail loudly by design' "$FILE"; then
    pass "Documents no-wildcard design (fail-loud for unknown models)"
else
    fail "Missing no-wildcard design statement"
fi

# 4c: Mentions master_key auth
if grep -qi "master.key" "$FILE"; then
    pass "References master_key auth"
else
    fail "Missing master_key auth reference"
fi

# 4d: Mentions localhost binding from docker-compose.yml
if grep -q "127.0.0.1" "$FILE" || grep -q "localhost" "$FILE"; then
    pass "References localhost binding"
else
    fail "Missing localhost binding reference"
fi

# 4e: Mentions DATABASE_URL warning
if grep -q "DATABASE_URL" "$FILE"; then
    pass "References DATABASE_URL rule"
else
    fail "Missing DATABASE_URL rule"
fi

# 4f: Mentions metadata-only / never logs content
if grep -Eqi 'metadata.only|never.*content|no.*message.*content' "$FILE"; then
    pass "References metadata-only logging"
else
    fail "Missing metadata-only logging reference"
fi

# 4g: Mentions upstream_empty signal
if grep -q "upstream_empty" "$FILE"; then
    pass "References upstream_empty signal"
else
    fail "Missing upstream_empty signal reference"
fi

# 4h: CI workflow triggers are accurate
if grep -Eq 'every 30 min|[*]/30' "$FILE"; then
    pass "Proxy-canary schedule referenced"
else
    fail "Missing proxy-canary schedule (every 30 min)"
fi

if grep -Eqi 'daily|13:00' "$FILE"; then
    pass "Model-health schedule referenced"
else
    fail "Missing model-health schedule (daily)"
fi

# 4i: Model name mapping — hyphenated aliases → provider model IDs
if grep -Eq 'hyphenated|claude-sonnet|claude-opus|provider/model|model ID' "$FILE"; then
    pass "References model name format (hyphenated aliases)"
else
    fail "Missing model name format reference"
fi

# ── Summary ───────────────────────────────────────────────────
echo ""
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
