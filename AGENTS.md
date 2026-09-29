# AGENTS.md — llm-gateway

## Workspace role

**Runtime proxy, not product content.** This repo is a Sandcastle consumer and
the Copilot proxy host. It is a sibling folder in the multi-root workspace but
is not editable as product code — engine/template changes belong in
`arndvs/ctrlshft-hub` and the producer (`ctrlshft-public`). See
`~/dotfiles/WORKSPACE_INVARIANTS.md`.

## Security

**NEVER read `.env` or any file matching `.env.*`.** These contain secrets.

## Architecture

LiteLLM proxy translates Anthropic Messages API → OpenRouter (primary), with GitHub Copilot as fallback.

```
Claude Code  →  LiteLLM (:4000)  →  openrouter.ai (primary)
                                    └→ api.githubcopilot.com (fallback)
                 ↑ litellm_config.yaml
                 ↑ OPENROUTER_API_KEY from .env (primary)
                 ↑ OAuth token cached at ~/.config/litellm/github_copilot/ (fallback)
```

## Key files

| File | Purpose |
|------|---------|
| `litellm_config.yaml` | Proxy routing config — OpenRouter primary, Copilot fallback |
| `Makefile` | Workflow automation (setup/start/stop/test/enable/disable) |
| `start_proxy.sh` | Standalone proxy launcher with `.env` loading |
| `scripts/claude_enable.py` | Write proxy env vars to `~/.claude/settings.json` |
| `scripts/claude_disable.py` | Remove proxy config from Claude settings |
| `scripts/fetch_probe_engine.sh` | Fetch the probe engine (probe_completion.sh + probe_parser.py) from ctrlshft-hub at the pinned SHA |
| `.env.example` | Template for required environment variables |

## Conventions

- Shell scripts use `bash` with `set -euo pipefail`
- Python scripts are standalone (no dependencies beyond stdlib)
- Port default: `4000`; if `LITELLM_PORT` is set (for example in `.env`), it takes precedence. `make start PORT=XXXX` only applies when `LITELLM_PORT` is unset or removed.
- `UV_NATIVE_TLS=true` is required for corporate proxy / SSL environments
- `LITELLM_LOCAL_MODEL_COST_MAP=true` avoids remote cost map fetch
