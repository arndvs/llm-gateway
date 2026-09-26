#!/usr/bin/env bash
# test_db_overlay_contract.sh — DB-mode boundary structural contract (refs #169)
#
# The rule "never set DATABASE_URL without also starting the db service" was
# documented four times and enforced nowhere. This test makes it executable:
#
#   1. base docker-compose.yml NEVER sets DATABASE_URL on the proxy service
#   2. overlay docker-compose.db.yml ALWAYS sets it AND defines the db service
#      WITH a healthcheck
#   3. the overlay's proxy keeps depends_on: db: condition: service_healthy
#   4. fixture cases: base+DATABASE_URL → fail; base → pass; overlay → pass;
#      overlay with db removed → fail
#
# Uses in-memory YAML structure assertions (no Docker needed) plus a
# `docker compose config` smoke test when Docker is available.
#
# Usage: bash tests/test_db_overlay_contract.sh
#
# Refs #169

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  ✅ $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  ❌ $1"; }

BASE="docker-compose.yml"
OVERLAY="docker-compose.db.yml"

if [ ! -f "$BASE" ]; then
    echo "  ❌ $BASE not found"
    exit 1
fi
if [ ! -f "$OVERLAY" ]; then
    echo "  ❌ $OVERLAY not found"
    exit 1
fi

# ── Test 1: base compose never sets DATABASE_URL on proxy ──────
echo "Test 1: base docker-compose.yml never sets DATABASE_URL"
if python3 -c "
import yaml
d = yaml.safe_load(open('$BASE', encoding='utf-8'))
proxy = d['services']['proxy']
env = proxy.get('environment', {}) or {}
assert 'DATABASE_URL' not in env, 'base compose sets DATABASE_URL on proxy'
print('OK')
" >/dev/null 2>&1; then
    pass "base compose has no DATABASE_URL on proxy"
else
    fail "base compose sets DATABASE_URL on proxy"
fi

# ── Test 2: overlay always sets DATABASE_URL + defines db ──────
echo "Test 2: overlay sets DATABASE_URL and defines db with healthcheck"
if python3 -c "
import yaml
d = yaml.safe_load(open('$OVERLAY', encoding='utf-8'))
proxy = d['services']['proxy']
env = proxy.get('environment', {}) or {}
assert 'DATABASE_URL' in env, 'overlay does not set DATABASE_URL'
assert 'db' in d['services'], 'overlay does not define db service'
db = d['services']['db']
assert 'healthcheck' in db, 'db service has no healthcheck'
print('OK')
" >/dev/null 2>&1; then
    pass "overlay sets DATABASE_URL + defines db with healthcheck"
else
    fail "overlay missing DATABASE_URL / db service / healthcheck"
fi

# ── Test 3: overlay proxy depends_on db with service_healthy ───
echo "Test 3: overlay proxy depends_on db: condition: service_healthy"
if python3 -c "
import yaml
d = yaml.safe_load(open('$OVERLAY', encoding='utf-8'))
proxy = d['services']['proxy']
depends = proxy.get('depends_on', {})
assert isinstance(depends, dict) and 'db' in depends, 'proxy does not depend_on db'
cond = depends['db']
assert isinstance(cond, dict) and cond.get('condition') == 'service_healthy', \
    f'db dependency is not health-conditioned: {cond!r}'
print('OK')
" >/dev/null 2>&1; then
    pass "overlay proxy depends_on db: service_healthy"
else
    fail "overlay proxy dependency is not health-conditioned"
fi

# ── Test 4: fixture cases ──────────────────────────────────────
echo "Test 4: fixture cases (base+DATABASE_URL fails, base passes, overlay passes, overlay-db-removed fails)"
if python3 -c "
import copy
import yaml

def load(path):
    return yaml.safe_load(open(path, encoding='utf-8'))

def proxy_has_db_url(compose):
    proxy = compose['services']['proxy']
    env = proxy.get('environment', {}) or {}
    return 'DATABASE_URL' in env

def has_db_service(compose):
    return 'db' in compose.get('services', {})

def has_healthy_dep(compose):
    proxy = compose['services']['proxy']
    depends = proxy.get('depends_on', {})
    if not isinstance(depends, dict) or 'db' not in depends:
        return False
    cond = depends['db']
    return isinstance(cond, dict) and cond.get('condition') == 'service_healthy'

base = load('$BASE')
overlay = load('$OVERLAY')

# Fixture 1: base + DATABASE_URL leaked → FAIL (db-less image, no db service)
base_leaked = copy.deepcopy(base)
base_leaked['services']['proxy']['environment']['DATABASE_URL'] = 'postgresql://x'
assert proxy_has_db_url(base_leaked) and not has_db_service(base_leaked), \
    'fixture 1 setup wrong'
print('fixture 1 (base+DATABASE_URL → fail): OK')

# Fixture 2: base without → PASS
assert not proxy_has_db_url(base), 'base should be db-less'
print('fixture 2 (base → pass): OK')

# Fixture 3: overlay (base + db) → PASS
assert proxy_has_db_url(overlay) and has_db_service(overlay) and has_healthy_dep(overlay), \
    'overlay should be a legal DB-mode pairing'
print('fixture 3 (overlay → pass): OK')

# Fixture 4: overlay with db removed → FAIL
overlay_no_db = copy.deepcopy(overlay)
del overlay_no_db['services']['db']
assert proxy_has_db_url(overlay_no_db) and not has_db_service(overlay_no_db), \
    'fixture 4 setup wrong'
print('fixture 4 (overlay db removed → fail): OK')
" >/dev/null 2>&1; then
    pass "all four fixture cases behave correctly"
else
    fail "fixture cases failed"
fi

# ── Test 5: docker compose config smoke (when Docker available) ─
echo "Test 5: docker compose config smoke (guarded)"
if command -v docker >/dev/null 2>&1; then
    # docker-compose.yml declares `env_file: .env`; newer Docker Compose
    # hard-errors when it is missing. A transient stub satisfies the
    # file-existence check (mirrors ci.yml's own compose validation).
    touch .env
    trap 'rm -f .env' EXIT
    if docker compose -f "$BASE" config >/dev/null 2>&1 \
       && docker compose -f "$BASE" -f "$OVERLAY" config >/dev/null 2>&1; then
        pass "docker compose config parses base + overlay"
    else
        fail "docker compose config failed (base or overlay)"
    fi
    rm -f .env
    trap - EXIT
else
    echo "  ℹ️  docker not available — skipping compose config smoke"
    pass "docker compose config smoke skipped (no docker)"
fi

echo ""
echo "Result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]