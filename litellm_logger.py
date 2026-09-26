"""litellm_logger.py — lightweight proxy observability callback.

Emits ONE structured line per completion to stdout (captured by `docker logs`)
so empty / degraded upstream responses can be correlated over time. It logs
metadata ONLY — never message content — so it is safe to leave on in production.

Enabled via `litellm_settings.callbacks: litellm_logger.proxy_handler_instance`
in litellm_config.yaml. The file lives at the repo root so it is importable both
inside the container (WORKDIR /app, PYTHONPATH=/app) and for local `make start`
(run from the repo root).

Design rule: every code path is defensive — a logging failure must NEVER affect
request handling, so all work is wrapped in try/except and the callback degrades
to a no-op rather than raising.

Why this exists: empty `/v1/messages` completions were traced to LiteLLM's
Anthropic-translation adapter (the OpenAI `/v1/chat/completions` path on the same
server is reliable). This callback records the UPSTREAM completion's finish_reason
and content length for every request, so when a client sees an empty response we
can confirm from the logs whether the upstream actually returned content.

Streaming note: `log_stream_event` / `async_log_stream_event` are intentional
no-ops. LiteLLM fires those hooks once per streaming chunk, which would produce
very noisy incomplete logs. The final aggregated completion is delivered via
`log_success_event` / `async_log_success_event` and logged there.
"""

from __future__ import annotations

import json
import sys

try:
    from litellm.integrations.custom_logger import CustomLogger
except Exception:  # pragma: no cover - litellm is always present in the proxy image
    class CustomLogger:  # type: ignore[no-redef]
        """Fallback base so importing this module never fails the proxy."""


def _duration_ms(start_time, end_time):
    try:
        delta = end_time - start_time
        # LiteLLM may pass numeric Unix timestamps (float/int) or datetime objects.
        if isinstance(delta, (int, float)):
            return round(delta * 1000)
        return round(delta.total_seconds() * 1000)
    except Exception:
        return None


def _extract(response_obj):
    """Return (finish_reason, content_len, completion_tokens) defensively.

    content_len: length of the text content, 0 when empty/missing, -1 when the
    content is a non-string (e.g. tool blocks).
    """
    finish = None
    content_len = None
    ctoks = None
    try:
        if isinstance(response_obj, dict):
            choices = response_obj.get("choices")
            content_raw = response_obj.get("content")  # Anthropic /v1/messages format
            usage = response_obj.get("usage") or {}
        else:
            choices = getattr(response_obj, "choices", None)
            content_raw = getattr(response_obj, "content", None)  # Anthropic /v1/messages format
            usage = getattr(response_obj, "usage", None) or {}

        if choices:
            c0 = choices[0]
            if isinstance(c0, dict):
                finish = c0.get("finish_reason")
                msg = c0.get("message") or {}
                content = msg.get("content") if isinstance(msg, dict) else None
            else:
                finish = getattr(c0, "finish_reason", None)
                msg = getattr(c0, "message", None)
                content = getattr(msg, "content", None)
            if isinstance(content, str):
                content_len = len(content)
            elif content in (None, ""):
                content_len = 0
            else:
                content_len = -1
        elif content_raw is not None:
            # Anthropic-style /v1/messages: content is a list of typed blocks
            # e.g. [{"type": "text", "text": "..."}]. Extract text length so
            # upstream_empty is accurate for the path we most care about.
            if isinstance(content_raw, list):
                text = "".join(
                    (b.get("text", "") if isinstance(b, dict) else getattr(b, "text", ""))
                    for b in content_raw
                    if (isinstance(b, dict) and b.get("type") == "text")
                    or (not isinstance(b, dict) and getattr(b, "type", None) == "text")
                )
                content_len = len(text)
            elif isinstance(content_raw, str):
                content_len = len(content_raw)
            else:
                content_len = -1  # non-list/non-str: tool blocks etc — not "empty", matches docstring
            if isinstance(response_obj, dict):
                finish = response_obj.get("stop_reason")
            else:
                finish = getattr(response_obj, "stop_reason", None)

        if isinstance(usage, dict):
            ctoks = usage.get("completion_tokens")
        else:
            ctoks = getattr(usage, "completion_tokens", None)
    except Exception:
        pass
    return finish, content_len, ctoks


def _extract_http_info(kwargs):
    """Return dict with ``http_status`` (int|None) and optional ``ratelimit``.

    Extracts from ``kwargs["original_response"]`` (an httpx.Response) first,
    falling back to ``kwargs["exception"]`` which may carry ``.status_code``
    and ``.headers``.

    The ``ratelimit`` key is **omitted** when no ``x-ratelimit-*`` headers are
    present, keeping normal log lines compact.

    All code is wrapped in try/except — extraction failures degrade to
    ``{"http_status": None}`` rather than raising.
    """
    result = {"http_status": None}
    try:
        if not isinstance(kwargs, dict):
            return result

        # --- Locate the response-like object ---
        source = None
        for key in ("original_response", "exception"):
            candidate = kwargs.get(key)
            if candidate is not None and hasattr(candidate, "status_code"):
                source = candidate
                break

        if source is None:
            return result

        # --- status code ---
        try:
            status_code = source.status_code
            if isinstance(status_code, int):
                result["http_status"] = status_code
        except Exception:
            pass

        # --- ratelimit headers ---
        try:
            headers = source.headers
            if headers:
                rl = {}
                for name, value in (
                    headers.items() if hasattr(headers, "items") else []
                ):
                    lower = name.lower()
                    if lower.startswith("x-ratelimit-"):
                        rl[lower[len("x-ratelimit-"):]] = value
                if rl:
                    result["ratelimit"] = rl
        except Exception:
            pass

    except Exception:
        pass
    return result


def _extract_routing(kwargs):
    """Return (routed_model, is_fallback) defensively (refs #157).

    ``routed_model`` is the actual model that served the request — the
    ``litellm_params.model`` value (e.g. ``github_copilot/claude-opus-4.8``),
    NOT the requested alias (``kwargs["model"]``, e.g. ``claude-sonnet-4-6``).
    ``is_fallback`` is True when the serving deployment is a ``-fallback`` lane.

    LiteLLM's callback exposes the resolved model via
    ``kwargs["litellm_params"]["model"]``. ``is_fallback`` is derived by
    checking whether that resolved model's alias lane is a fallback deployment
    (``-fallback`` suffix), which is the durable contract generate_config.py
    builds (refs #174).

    Defensive: any extraction failure degrades to (None, None) — never raises.
    """
    try:
        if not isinstance(kwargs, dict):
            return None, None
        litellm_params = kwargs.get("litellm_params")
        if not isinstance(litellm_params, dict):
            return None, None
        routed_model = litellm_params.get("model")
        if not isinstance(routed_model, str) or not routed_model:
            return None, None
        # The resolved model may be a full provider path (openrouter/...,
        # github_copilot/...) or a bare alias. The fallback lane is identified
        # by the alias suffix — check both the resolved model and the requested
        # alias for the -fallback marker.
        alias = kwargs.get("model")
        is_fallback = bool(
            (isinstance(alias, str) and alias.endswith("-fallback"))
            or routed_model.endswith("-fallback")
        )
        return routed_model, is_fallback
    except Exception:
        return None, None


def _emit(kwargs, response_obj, start_time, end_time, status):
    try:
        rec = {"t": "proxy_log", "status": status}
        if isinstance(kwargs, dict):
            rec["model"] = kwargs.get("model")
            rec["call_type"] = kwargs.get("call_type")
            rec["stream"] = kwargs.get("stream") if "stream" in kwargs else None
        else:
            rec["stream"] = None
        rec["ms"] = _duration_ms(start_time, end_time)

        # Actual serving model + fallback flag (refs #157) — enables
        # fallback-rate alerting via `grep 'is_fallback.*true'`.
        routed_model, is_fallback = _extract_routing(kwargs)
        rec["routed_model"] = routed_model
        rec["is_fallback"] = is_fallback

        finish, content_len, ctoks = _extract(response_obj)
        rec["finish"] = finish
        rec["content_len"] = content_len
        rec["completion_tokens"] = ctoks
        # The key signal: a "successful" completion whose upstream content is empty.
        rec["upstream_empty"] = bool(
            status == "success" and (content_len == 0 or ctoks == 0)
        )

        # HTTP status code and rate-limit headers from the upstream response.
        http_info = _extract_http_info(kwargs)
        rec["http_status"] = http_info.get("http_status")
        if "ratelimit" in http_info:
            rec["ratelimit"] = http_info["ratelimit"]

        print("PROXY_LOG " + json.dumps(rec, default=str), file=sys.stdout, flush=True)
    except Exception:
        # Observability must never break request handling.
        pass


class ProxyObservabilityLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        _emit(kwargs, response_obj, start_time, end_time, "success")

    def log_failure_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        _emit(kwargs, response_obj, start_time, end_time, "failure")

    def log_stream_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        # Streaming fires this hook per chunk; the final aggregated completion
        # is handled by log_success_event after all chunks are received.
        # Logging per-chunk would be very noisy and log incomplete content_len.
        pass

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        _emit(kwargs, response_obj, start_time, end_time, "success")

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        _emit(kwargs, response_obj, start_time, end_time, "failure")

    async def async_log_stream_event(self, kwargs, response_obj, start_time, end_time, **extra_kwargs):
        # Same as log_stream_event: skip per-chunk logging.
        pass


proxy_handler_instance = ProxyObservabilityLogger()
