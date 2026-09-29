.PHONY: help setup start stop test test-stream generate-config claude-enable claude-disable claude-status install-claude

# Effective port is owned by scripts/proxy_endpoint.py (refs #195) — the
# canonical precedence chain (PROXY_BASE_URL → settings ANTHROPIC_BASE_URL →
# LITELLM_PORT/.env → localhost:4000). Targets quote it via the resolver CLI
# instead of parsing the port by hand. `proxy-port` is the single place the
# resolver is quoted; PORT := is the eager definition for the common case.
PORT := $(shell python3 scripts/proxy_endpoint.py | sed -n 's/^port=//p')
proxy-port = $(shell python3 scripts/proxy_endpoint.py | sed -n 's/^port=//p')

help:
	@echo ""
	@echo "llm-gateway"
	@echo "─────────────────────────────────────────"
	@echo "  make setup               Set up .env with generated keys"
	@echo "  make start               Start LiteLLM proxy (OpenRouter primary, Copilot fallback)"
	@echo "  make stop                Stop the proxy"
	@echo "  make test                Test proxy is working (non-streaming)"
	@echo "  make test-stream         Test proxy streaming (SSE) response"
	@echo "  make generate-config     Regenerate litellm_config.yaml from the model mapping"
	@echo ""
	@echo "  make claude-enable       Point Claude Code at local proxy"
	@echo "  make claude-disable      Restore Claude Code to Anthropic direct"
	@echo "  make claude-status       Show current Claude Code config"
	@echo ""
	@echo "  make install-claude      Install Claude Code CLI via npm"
	@echo ""

# ── Setup ──────────────────────────────────────────────────────

setup:
	@if [ ! -f .env ]; then \
		echo "Generating .env..."; \
		umask 077; \
		python3 -c "\
import uuid; \
mk = 'sk-' + str(uuid.uuid4()); \
open('.env','w').write('LITELLM_MASTER_KEY=' + mk + '\nLITELLM_PORT=$(PORT)\nLITELLM_LOCAL_MODEL_COST_MAP=true\n'); \
print('✅ .env created'); \
print('   LITELLM_MASTER_KEY stored in .env'); \
"; \
		chmod 600 .env; \

	else \
		echo "✅ .env already exists — skipping"; \
	fi
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "Installing uv..."; \
		curl -LsSf https://astral.sh/uv/install.sh | sh; \
		if ! command -v uv >/dev/null 2>&1; then \
			echo "❌ uv was installed but is not available on PATH in this shell."; \
			echo "   The installer often places uv in $$HOME/.local/bin and requires a new shell."; \
			echo "   Add $$HOME/.local/bin to your PATH or start a new shell, then re-run 'make setup' or 'make start'."; \
			exit 1; \
		fi; \
	fi
	@echo "✅ Setup complete. Run 'make start' to start the proxy."

# ── Proxy lifecycle ────────────────────────────────────────────

start:
	@if [ ! -f .env ]; then echo "❌ .env not found. Run 'make setup' first."; exit 1; fi
	@set -a && . ./.env && set +a && \
	PORT="$$($(proxy-port))" && \
     echo "Starting LiteLLM proxy (OpenRouter primary, GitHub Copilot fallback) on port $$PORT..." && \
     source scripts/_launch_proxy.sh && \
     launch_proxy "$$PORT" "litellm_config.yaml"
stop:
	@if [ ! -f .env ]; then echo "❌ .env not found. Run 'make setup' first."; exit 1; fi
	@set -a && . ./.env && set +a && \
	PORT="$$($(proxy-port))" && \
	pkill -f "litellm --config .*litellm_config.yaml --port $$PORT" 2>/dev/null && echo "✅ Proxy stopped" || echo "ℹ️  No proxy process found"

# ── Config generation ──────────────────────────────────────────

generate-config:
	@python3 scripts/generate_config.py -o litellm_config.yaml && \
	git diff --exit-code litellm_config.yaml && echo "✅ litellm_config.yaml is up to date with generate_config.py" || \
	{ echo "❌ litellm_config.yaml was regenerated (drift fixed). Review and commit the change."; exit 1; }

# ── Test ───────────────────────────────────────────────────────

test:
	@if [ ! -f .env ]; then echo "❌ .env not found. Run 'make setup' first."; exit 1; fi
	@set -a && . ./.env && set +a && \
	PORT="$$($(proxy-port))" && \
	MASTER_KEY=$$LITELLM_MASTER_KEY && \
	echo "Testing proxy at http://localhost:$$PORT..." && \
	curl -sf -X POST http://localhost:$$PORT/v1/messages \
		-H "Content-Type: application/json" \
		-H "Authorization: Bearer $$MASTER_KEY" \
		-d '{"model":"claude-sonnet-4-6","max_tokens":50,"messages":[{"role":"user","content":"Say hello in one word."}]}' \
	| python3 -m json.tool && echo "" && echo "✅ Proxy is working!" \
	|| { echo "❌ Test failed. Is the proxy running? ('make start')"; exit 1; }

test-stream:
	@if [ ! -f .env ]; then echo "❌ .env not found. Run 'make setup' first."; exit 1; fi
	@set -a && . ./.env && set +a && \
	PORT="$$($(proxy-port))" && \
	MASTER_KEY=$$LITELLM_MASTER_KEY && \
	echo "Testing streaming (SSE) at http://localhost:$$PORT..." && \
	RESPONSE=$$(curl -sf -X POST http://localhost:$$PORT/v1/messages \
		-H "Content-Type: application/json" \
		-H "Authorization: Bearer $$MASTER_KEY" \
		-H "Accept: text/event-stream" \
		-d '{"model":"claude-sonnet-4-6","max_tokens":50,"stream":true,"messages":[{"role":"user","content":"Say hi."}]}') && \
	echo "$$RESPONSE" | head -20 && \
	if echo "$$RESPONSE" | grep -q 'data:'; then \
		echo "" && echo "✅ Streaming response contains SSE data lines!"; \
		if echo "$$RESPONSE" | grep -q '"upstream_empty":true'; then \
			echo "⚠️  upstream_empty:true detected in streaming response (document if expected)"; \
		else \
			echo "✅ No upstream_empty:true — streaming is delivering content"; \
		fi; \
	else \
		echo "❌ No SSE data: lines found. Is streaming enabled?"; exit 1; \
	fi \
	|| { echo "❌ Stream test failed. Is the proxy running? ('make start')"; exit 1; }

# ── Claude Code configuration ──────────────────────────────────

claude-enable:
	@if [ ! -f .env ]; then echo "❌ .env not found. Run 'make setup' first."; exit 1; fi
	@set -a && . ./.env && set +a && \
	PORT="$$($(proxy-port))" && \
	MASTER_KEY=$$LITELLM_MASTER_KEY && \
	if [ -z "$$MASTER_KEY" ]; then echo "❌ LITELLM_MASTER_KEY not found in .env"; exit 1; fi; \
	SETTINGS_FILE="$$HOME/.claude/settings.json"; \
	if [ -f "$$SETTINGS_FILE" ]; then \
		BACKUP="$$SETTINGS_FILE.backup.$$(date +%Y%m%d_%H%M%S)"; \
		cp "$$SETTINGS_FILE" "$$BACKUP"; \
		chmod 600 "$$BACKUP"; \
		echo "📁 Backed up settings to $$BACKUP"; \
	fi; \
	LITELLM_MASTER_KEY="$$MASTER_KEY" LITELLM_PORT="$$PORT" python3 scripts/claude_enable.py

claude-disable:
	@SETTINGS_FILE="$$HOME/.claude/settings.json"; \
	if [ -f "$$SETTINGS_FILE" ]; then \
		BACKUP="$$SETTINGS_FILE.proxy_backup.$$(date +%Y%m%d_%H%M%S)"; \
		cp "$$SETTINGS_FILE" "$$BACKUP"; \
		chmod 600 "$$BACKUP"; \
		echo "📁 Backed up current settings to $$BACKUP"; \
	fi
	@python3 scripts/claude_disable.py

claude-status:
	@echo ""
	@echo "Claude Code configuration"
	@echo "─────────────────────────────────────────"
	@SETTINGS_FILE="$$HOME/.claude/settings.json"; \
	if [ -f "$$SETTINGS_FILE" ]; then \
		python3 scripts/claude_status_redact.py < "$$SETTINGS_FILE" 2>/dev/null || { echo '(could not parse settings)'; exit 0; }; \
		echo ""; \
		python3 scripts/proxy_status.py "$$SETTINGS_FILE" "$$($(proxy-port))"; \
	else \
		echo "No settings file — using Claude Code defaults (Anthropic direct)"; \
	fi
	@echo ""

# ── Install ────────────────────────────────────────────────────

install-claude:
	@if command -v npm >/dev/null 2>&1; then \
		npm install -g @anthropic-ai/claude-code && \
		echo "✅ Claude Code installed. Run 'make setup' next."; \
	else \
		echo "❌ npm not found. Install Node.js first: https://nodejs.org/"; \
	fi
