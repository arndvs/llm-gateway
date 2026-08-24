# AGENTS.md — llm-gateway

## Workspace role

**Runtime proxy, not product content.** This repo is a Sandcastle consumer and
the Copilot proxy host. It is a sibling folder in the multi-root workspace but
is not editable as product code — engine/template changes belong in
`arndvs/sandcastle-hub` and the producer (`ctrlshft-public`). See
`~/dotfiles/WORKSPACE_INVARIANTS.md`.

## Security

**NEVER read `.env` or any file matching `.env.*`.** These contain secrets.

## Architecture

LiteLLM proxy translates Anthropic Messages API → GitHub Copilot API (primary), with OpenRouter as fallback.

```
Claude Code  →  LiteLLM (:4000)  →  api.githubcopilot.com (primary)
                                    └→ openrouter.ai (fallback)
                 ↑ litellm_config.yaml
                 ↑ OAuth token cached at ~/.config/litellm/github_copilot/
                 ↑ OPENROUTER_API_KEY from .env (fallback)
```

## Key files

| File | Purpose |
|------|---------|
| `litellm_config.yaml` | Proxy routing config — OpenRouter primary, Copilot fallback |
| `Makefile` | Workflow automation (setup/start/stop/test/enable/disable) |
| `start_proxy.sh` | Standalone proxy launcher with `.env` loading |
| `scripts/claude_enable.py` | Write proxy env vars to `~/.claude/settings.json` |
| `scripts/claude_disable.py` | Remove proxy config from Claude settings |
| `.env.example` | Template for required environment variables |

## Conventions

- Shell scripts use `bash` with `set -euo pipefail`
- Python scripts are standalone (no dependencies beyond stdlib)
- Port default: `4000`; if `LITELLM_PORT` is set (for example in `.env`), it takes precedence. `make start PORT=XXXX` only applies when `LITELLM_PORT` is unset or removed.
- `UV_NATIVE_TLS=true` is required for corporate proxy / SSL environments
- `LITELLM_LOCAL_MODEL_COST_MAP=true` avoids remote cost map fetch
