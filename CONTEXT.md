# CONTEXT.md — llm-gateway

## 1. LiteLLM → Copilot routing (with OpenRouter primary)

LiteLLM translates Anthropic Messages API calls into GitHub Copilot API calls.
`litellm_config.yaml` maps Claude Code's hyphenated model names to concrete upstream models.
Each alias has a **primary** (OpenRouter) and a **fallback** (GitHub Copilot) deployment,
wired via `router_settings.fallbacks` so the proxy stays up if OpenRouter fails.

OpenRouter is the **default upstream**. GitHub Copilot's Claude models were returning
empty-200 ('no choices') completions through the proxy — which LiteLLM treats as *success*
and therefore never fails over. OpenRouter serves real completions reliably, so every
concrete alias defaults to a concrete `openrouter/...` model, with Copilot as the automatic
fallback lane. The dual-provider structure is preserved: flipping orientations is a config
edit, not a code change.

| Alias (Claude Code sends) | Primary (OpenRouter) | Fallback (Copilot) |
|---|---|---|
| `claude-sonnet-4-6` | `openrouter/deepseek/deepseek-v4-flash-0731` | `github_copilot/claude-sonnet-5` |
| `claude-haiku-4-5-20251001` | `openrouter/deepseek/deepseek-v4-flash-0731` | `github_copilot/claude-sonnet-5` (Copilot has no Haiku) |
| `claude-opus-4-6` | `openrouter/deepseek/deepseek-v4-flash-0731` | `github_copilot/claude-sonnet-5` |
| `claude-opus-4-7` | `openrouter/deepseek/deepseek-v4-flash-0731` | `github_copilot/claude-sonnet-5` |

No wildcard: `openrouter/*` is not a concrete model (HTTP 400 `no_db_connection`) and `github_copilot/*` silently passes unknown models through. Every alias has an explicit concrete fallback above; unknown models fail loudly by design.

Primary entries carry an `api_key` referencing the OpenRouter key from the environment; fallback entries carry the four editor headers Copilot validates.

**Auth boundary.** `general_settings.master_key` reads from `LITELLM_MASTER_KEY` env var. Claude Code authenticates to LiteLLM with this key; LiteLLM authenticates to Copilot with the OAuth token cached at `~/.config/litellm/github_copilot/`, and to OpenRouter with `OPENROUTER_API_KEY`. The credentials never cross.

**Proxy-mode key manifest.** The env-var keys that constitute "Claude Code is in proxy mode" are declared once as `PROXY_ENV_KEYS` in `scripts/proxy_status.py` (refs #117): `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`. `claude_enable.py` writes only these keys and `claude_disable.py` removes only these keys, so a key added to the manifest is written and removed together — preventing a half-disabled state. `OPENROUTER_API_KEY` is a proxy-side credential (in `.env`, used by LiteLLM to reach OpenRouter), not a Claude-settings key, and is intentionally absent from the manifest. `tests/test_proxy_status.py` asserts the manifest matches what enable writes and disable removes.

**Proxy settings.** `drop_params: true` and `additional_drop_params: ["response_format", "thinking"]` are global `litellm_settings` that silently strip parameters the upstream doesn't support. `json_logs: true` enables structured proxy logs, and `callbacks: ["litellm_logger.proxy_handler_instance", "health_version.version_callback_instance"]` registers the metadata logger plus health/version route callback. `stream: true` is set in `litellm_params` on every route to reduce empty-content 200s from the Anthropic adapter — streaming delivers chunks incrementally and avoids the adapter race where a non-streamed response can return empty content.

**Settings-block contract.** The three settings blocks carry safety-critical config and are enforced by `tests/test_settings_contract.py` (refs #119): `drop_params` must be boolean `true`, `additional_drop_params` a list of strings, `callbacks` a non-empty list whose module paths resolve to existing files at the repo root, `general_settings.master_key` an `os.environ/...` reference, and `router_settings.num_retries` a positive int matching `litellm_settings.num_retries`. No unknown top-level keys are allowed. This is the executable contract for the settings blocks, mirroring the `model_list` contract in `test_model_entry_contract.py` (refs #80).

## 2. DB-less default mode

`docker-compose.yml` runs proxy-only — no database, no extra containers.

| Aspect | Detail |
|---|---|
| Port binding | `127.0.0.1:${LITELLM_PORT:-4000}:4000` — localhost only |
| Cost map | `LITELLM_LOCAL_MODEL_COST_MAP=true` — no remote fetch |
| Healthcheck | `GET /health/readiness` every 30 s |
| Restart | `unless-stopped` |

**Postgres overlay.** `docker-compose.db.yml` adds a `db` service (Postgres 16 Alpine) and sets `DATABASE_URL` on the proxy. Enables spend tracking, virtual keys, and model-in-db.

```
docker compose -f docker-compose.yml -f docker-compose.db.yml up --build
```

> **Rule:** never set `DATABASE_URL` without also starting the db service — LiteLLM enters DB mode with no reachable database and returns `400 "No connected db"` on every request.

## 3. Observability — PROXY_LOG

`litellm_logger.py` is a `CustomLogger` callback registered via `litellm_settings.callbacks`. It emits one `PROXY_LOG` JSON line per completion to stdout.

**Fields logged:**

| Field | Description |
|---|---|
| `model` | Requested model name (alias) |
| `routed_model` | Actual model that served the request (e.g. `openrouter/deepseek/deepseek-v4-flash-0731` or `github_copilot/claude-sonnet-5`); `null` when unavailable |
| `is_fallback` | `true` when the serving deployment is a `-fallback` lane (Copilot); `false` for primary; `null` when unavailable |
| `call_type` | LiteLLM call type |
| `stream` | Whether the request used streaming (`true`, `false`, or `null` if unknown) |
| `ms` | Latency in milliseconds |
| `finish` | Upstream `finish_reason` / `stop_reason` |
| `content_len` | Text content length (0 = empty, −1 = non-string) |
| `completion_tokens` | Token count from usage |
| `upstream_empty` | `true` when status=success and (content_len=0 or completion_tokens=0) |
| `http_status` | HTTP status code from upstream (int or null) |
| `ratelimit` | Dict of `x-ratelimit-*` headers (prefix-stripped); **omitted** when none present |
| `status` | `success` or `failure` |

**Fallback-rate alerting.** `routed_model` + `is_fallback` make fallback-served
requests greppable: `grep 'is_fallback.*true'` on structured logs identifies
every completion served by the Copilot fallback lane, enabling fallback-rate
measurement and alerting when the primary (OpenRouter) degrades.

**Design rules:**

- **Metadata only** — never logs message content. Safe for production.
- **Defensive** — every code path is wrapped in `try/except`. A logging failure degrades to a no-op; it never raises and never affects request handling.
- **Why it exists** — empty `/v1/messages` completions were traced to LiteLLM's Anthropic-translation adapter. The `upstream_empty` flag lets operators confirm whether the upstream actually returned content when a client sees an empty response.

## 4. CI workflows

Three workflows under `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push to `dev`/`main`, all PRs | Security tests (`test_security.sh`), YAML parse, compose config validation (base + db overlay), Docker build, ShellCheck |
| `proxy-canary.yml` | every 30 min (`*/30 * * * *`) + manual | Probes hosted proxy: readiness check then a real `/v1/messages` completion. Hard failures (auth/5xx/unreachable) → opens issue. Empty content after retries → **warning, not failure** (transient upstream quirk) |
| `model-health.yml` | daily 13:00 UTC + manual | Extracts every explicit alias from `litellm_config.yaml`, sends a completion through the proxy for each. Failing aliases → auto-opens/updates a `model-health` issue |

**Proxy-canary detail.** Retries up to 5 times with 6 s sleep between attempts. Distinguishes hard errors (401/403/400/5xx/unreachable) from the upstream empty-content quirk. On persistent empty content the job sets `status=degraded` and emits a GitHub Actions warning — the proxy is verified as up and authenticating, so it does not page.

**Model-health detail.** Parses `litellm_config.yaml` with PyYAML to extract all non-wildcard aliases (the wildcard was removed: `openrouter/*` returns HTTP 400 and `github_copilot/*` silently passes unknown models through). Each alias is probed with 5 retries (4 s apart). Failing aliases are collected and reported in a `model-health` labeled issue. Guards against false greens: if YAML parsing yields zero aliases, the job fails immediately.

## 5. Version endpoint — `/health/version`

Single canonical module: **`health_version.py`**. Registered as a LiteLLM callback via `litellm_config.yaml`; at import time it attaches a `custom_api_router` to the proxy's FastAPI app.

**Environment variables:**

| Variable | Source | Behaviour |
|---|---|---|
| `BUILD_SHA` | `docker build --build-arg BUILD_SHA=$(git rev-parse --short HEAD)` | 7-char SHA; trimmed if longer |
| `BUILD_TIMESTAMP` | `docker build --build-arg BUILD_TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)` | ISO 8601 UTC string |

**Fallback for local dev.** When `BUILD_SHA` is unset or `"unknown"` (i.e. `make start` without `--build-arg`), the module calls `git rev-parse --short HEAD` so local dev always returns the real working-tree SHA instead of the literal string `"unknown"`.

**Response shape** (no auth required, same as `/health/readiness`):

```json
{"sha": "abc1234", "built_at": "2024-06-01T12:00:00Z"}
```

**Design constraints:**
- Only one module may register `/health/version`. `TestSingleRouteRegistration` in `tests/test_health_version.py` enforces this as a permanent regression guard.
- `version_endpoint.py` was deleted (refs #67); `LITELLM_WORKER_STARTUP_HOOKS` is not used.
