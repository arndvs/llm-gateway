"""Tests for litellm_logger.

Covers the pure helpers and the PROXY_LOG emission contract:
- _duration_ms: datetime objects, numeric timestamps, error cases
- _extract: OpenAI-style choices (dict/object), Anthropic-style content blocks,
  empty/missing content, non-string content, usage extraction
- _extract_http_info: status code, ratelimit headers, defensive behavior
- _emit: integration test verifying JSON structure, field presence, no content leak

Uses only stdlib (unittest). No external test dependencies.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from datetime import datetime
from io import StringIO
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from litellm_logger import _duration_ms, _extract, _extract_http_info, _emit, ProxyObservabilityLogger


# ---------------------------------------------------------------------------
# Helpers to build fake httpx-like responses / exceptions
# ---------------------------------------------------------------------------


class FakeHttpxResponse:
    """Mimics httpx.Response with .status_code and .headers."""

    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}


class FakeException:
    """Mimics a LiteLLM exception that carries .status_code and .headers."""

    def __init__(self, status_code: int, headers: dict | None = None):
        self.status_code = status_code
        self.headers = headers or {}


# ===================================================================
# _duration_ms — unit tests
# ===================================================================


class TestDurationMs(unittest.TestCase):
    """Test _duration_ms with datetime objects and numeric timestamps."""

    def test_datetime_objects_one_second(self):
        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 1)
        self.assertEqual(_duration_ms(start, end), 1000)

    def test_datetime_objects_fractional_seconds(self):
        start = datetime(2024, 1, 1, 0, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 0, 500000)  # +0.5s
        self.assertEqual(_duration_ms(start, end), 500)

    def test_numeric_timestamps_float(self):
        # LiteLLM may pass Unix timestamps as floats; delta = end - start in seconds
        start = 1700000000.0
        end = 1700000002.5  # 2.5s later
        self.assertEqual(_duration_ms(start, end), 2500)

    def test_none_inputs_returns_none(self):
        """When inputs are None, should return None (not raise)."""
        self.assertIsNone(_duration_ms(None, None))

    def test_mismatched_types_returns_none(self):
        """When subtraction fails, should return None."""
        self.assertIsNone(_duration_ms("bad", 123))

    def test_timedelta_result_with_microseconds(self):
        """Verify rounding works for microsecond-precision datetimes."""
        start = datetime(2024, 1, 1, 0, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 0, 1500)  # 1.5ms
        # round(0.0015 * 1000) = round(1.5) = 2 (banker's rounding)
        result = _duration_ms(start, end)
        self.assertEqual(result, 2)


# ===================================================================
# _extract — unit tests
# ===================================================================


class TestExtractOpenAIStyleDict(unittest.TestCase):
    """Test _extract with OpenAI-style choices as dicts."""

    def test_basic_choices_dict(self):
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Hello world"},
                }
            ],
            "usage": {"completion_tokens": 5},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 11)  # len("Hello world")
        self.assertEqual(ctoks, 5)


class TestExtractOpenAIStyleObject(unittest.TestCase):
    """Test _extract with OpenAI-style choices as objects (SimpleNamespace)."""

    def test_object_style_response(self):
        msg = SimpleNamespace(content="Hello from object")
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(
            choices=[choice],
            content=None,
            usage=SimpleNamespace(completion_tokens=7),
        )
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 17)  # len("Hello from object")
        self.assertEqual(ctoks, 7)

    def test_object_style_no_usage(self):
        msg = SimpleNamespace(content="data")
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(choices=[choice], content=None, usage=None)
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 4)
        self.assertIsNone(ctoks)


class TestExtractAnthropicStyle(unittest.TestCase):
    """Test _extract with Anthropic-style /v1/messages content blocks."""

    def test_text_block_list(self):
        """Standard Anthropic response with text blocks."""
        response = {
            "content": [
                {"type": "text", "text": "Hello "},
                {"type": "text", "text": "world"},
            ],
            "stop_reason": "end_turn",
            "usage": {"completion_tokens": 4},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "end_turn")
        self.assertEqual(content_len, 11)  # "Hello " + "world"
        self.assertEqual(ctoks, 4)

    def test_empty_text_blocks(self):
        """Text blocks with empty text should give content_len=0."""
        response = {
            "content": [{"type": "text", "text": ""}],
            "stop_reason": "end_turn",
            "usage": {"completion_tokens": 0},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "end_turn")
        self.assertEqual(content_len, 0)
        self.assertEqual(ctoks, 0)

    def test_tool_use_blocks_ignored_for_text_length(self):
        """Only text blocks are counted for content_len."""
        response = {
            "content": [
                {"type": "text", "text": "Using tool"},
                {"type": "tool_use", "id": "x", "name": "foo", "input": {}},
            ],
            "stop_reason": "tool_use",
            "usage": {"completion_tokens": 15},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "tool_use")
        self.assertEqual(content_len, 10)  # only "Using tool"
        self.assertEqual(ctoks, 15)

    def test_content_is_string(self):
        """When content is a plain string (unusual but possible)."""
        response = {
            "content": "Plain string content",
            "stop_reason": "end_turn",
            "usage": {"completion_tokens": 5},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "end_turn")
        self.assertEqual(content_len, 20)  # len("Plain string content")
        self.assertEqual(ctoks, 5)

    def test_content_is_non_list_non_string(self):
        """When content is neither list nor string, content_len=-1."""
        response = {
            "content": 12345,
            "stop_reason": "end_turn",
            "usage": {"completion_tokens": 1},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "end_turn")
        self.assertEqual(content_len, -1)
        self.assertEqual(ctoks, 1)


# ===================================================================
# _extract_http_info — unit tests
# ===================================================================


class TestExtractHttpInfo(unittest.TestCase):
    """Extract http_status and ratelimit from original_response / exception."""

    def test_status_code_extracted(self):
        kwargs = {"original_response": FakeHttpxResponse(200)}
        info = _extract_http_info(kwargs)
        self.assertEqual(info["http_status"], 200)

    def test_ratelimit_headers_extracted_and_prefix_stripped(self):
        headers = {
            "x-ratelimit-limit": "100",
            "x-ratelimit-remaining": "42",
            "x-ratelimit-reset": "1700000000",
            "content-type": "application/json",
        }
        kwargs = {"original_response": FakeHttpxResponse(200, headers)}
        info = _extract_http_info(kwargs)
        self.assertEqual(
            info["ratelimit"],
            {"limit": "100", "remaining": "42", "reset": "1700000000"},
        )

    def test_no_ratelimit_headers_omits_key(self):
        headers = {"content-type": "application/json"}
        kwargs = {"original_response": FakeHttpxResponse(200, headers)}
        info = _extract_http_info(kwargs)
        self.assertNotIn("ratelimit", info)

    def test_status_code_from_exception(self):
        kwargs = {"exception": FakeException(429)}
        info = _extract_http_info(kwargs)
        self.assertEqual(info["http_status"], 429)

    def test_ratelimit_from_exception(self):
        headers = {
            "x-ratelimit-limit": "60",
            "x-ratelimit-remaining": "0",
        }
        kwargs = {"exception": FakeException(429, headers)}
        info = _extract_http_info(kwargs)
        self.assertEqual(info["ratelimit"], {"limit": "60", "remaining": "0"})

    def test_empty_kwargs(self):
        info = _extract_http_info({})
        self.assertIsNone(info["http_status"])
        self.assertNotIn("ratelimit", info)

    def test_none_kwargs(self):
        info = _extract_http_info(None)
        self.assertIsNone(info["http_status"])

    def test_both_original_response_and_exception_prefers_original(self):
        """When both are present, original_response takes precedence."""
        kwargs = {
            "original_response": FakeHttpxResponse(200),
            "exception": FakeException(500),
        }
        info = _extract_http_info(kwargs)
        self.assertEqual(info["http_status"], 200)

    def test_ratelimit_header_case_insensitive(self):
        """Headers should be matched case-insensitively."""
        headers = {"X-RateLimit-Remaining": "5"}
        kwargs = {"original_response": FakeHttpxResponse(200, headers)}
        info = _extract_http_info(kwargs)
        self.assertEqual(info["ratelimit"], {"remaining": "5"})


# ===================================================================
# _emit integration — full PROXY_LOG output validation
# ===================================================================


def _capture_emit(kwargs, response_obj=None, status="success"):
    """Call _emit and capture the PROXY_LOG line from stdout."""
    start = datetime(2024, 1, 1, 0, 0, 0)
    end = datetime(2024, 1, 1, 0, 0, 1)

    buf = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        _emit(kwargs, response_obj, start, end, status)
    finally:
        sys.stdout = old_stdout

    line = buf.getvalue().strip()
    if not line.startswith("PROXY_LOG "):
        raise AssertionError(f"Expected PROXY_LOG prefix, got: {line!r}")
    return json.loads(line[len("PROXY_LOG "):])


class TestEmitHttpInfo(unittest.TestCase):
    """Verify _emit includes http_status and ratelimit from kwargs."""

    def test_emit_429_failure_with_ratelimit(self):
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "exception": FakeException(
                429,
                {
                    "x-ratelimit-limit": "60",
                    "x-ratelimit-remaining": "0",
                    "x-ratelimit-reset": "1700000060",
                },
            ),
        }
        rec = _capture_emit(kwargs, status="failure")
        self.assertEqual(rec["http_status"], 429)
        self.assertEqual(rec["ratelimit"]["remaining"], "0")


class TestEmitRouting(unittest.TestCase):
    """Verify _emit surfaces routed_model / is_fallback (refs #157)."""

    def test_primary_path_is_fallback_false(self):
        # Primary: requested alias routes to the OpenRouter deployment.
        kwargs = {
            "model": "claude-sonnet-4-6",
            "call_type": "completion",
            "litellm_params": {"model": "openrouter/deepseek/deepseek-v4-flash-0731"},
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(
            rec["routed_model"], "openrouter/deepseek/deepseek-v4-flash-0731"
        )
        self.assertFalse(rec["is_fallback"])

    def test_fallback_path_is_fallback_true(self):
        # Fallback: requested alias is the -fallback lane → Copilot deployment.
        kwargs = {
            "model": "claude-sonnet-4-6-fallback",
            "call_type": "completion",
            "litellm_params": {"model": "github_copilot/claude-sonnet-5"},
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["routed_model"], "github_copilot/claude-sonnet-5")
        self.assertTrue(rec["is_fallback"])

    def test_missing_metadata_degrades_to_none(self):
        # No litellm_params → both fields null, never raises.
        kwargs = {
            "model": "claude-sonnet-4-6",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertIsNone(rec["routed_model"])
        self.assertIsNone(rec["is_fallback"])


class TestEmitJsonStructure(unittest.TestCase):
    """Verify the full JSON record structure from _emit."""

    def test_all_expected_fields_present(self):
        kwargs = {
            "model": "gpt-4",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        response_obj = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "Hi"},
                }
            ],
            "usage": {"completion_tokens": 1},
        }
        rec = _capture_emit(kwargs, response_obj=response_obj)
        # Required fields
        self.assertEqual(rec["t"], "proxy_log")
        self.assertEqual(rec["status"], "success")
        self.assertEqual(rec["model"], "gpt-4")
        self.assertEqual(rec["call_type"], "completion")
        self.assertEqual(rec["ms"], 1000)
        self.assertEqual(rec["finish"], "stop")
        self.assertEqual(rec["content_len"], 2)
        self.assertEqual(rec["completion_tokens"], 1)
        self.assertFalse(rec["upstream_empty"])
        self.assertEqual(rec["http_status"], 200)

    def test_upstream_empty_flag_when_content_empty(self):
        kwargs = {
            "model": "test",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        response_obj = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": ""},
                }
            ],
            "usage": {"completion_tokens": 0},
        }
        rec = _capture_emit(kwargs, response_obj=response_obj, status="success")
        self.assertTrue(rec["upstream_empty"])

    def test_emit_with_anthropic_style_response(self):
        kwargs = {
            "model": "claude-3",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        response_obj = {
            "content": [
                {"type": "text", "text": "Hello from Claude"},
            ],
            "stop_reason": "end_turn",
            "usage": {"completion_tokens": 5},
        }
        rec = _capture_emit(kwargs, response_obj=response_obj)
        self.assertEqual(rec["finish"], "end_turn")
        self.assertEqual(rec["content_len"], 17)
        self.assertEqual(rec["completion_tokens"], 5)
        self.assertFalse(rec["upstream_empty"])

    def test_emit_defensive_on_bad_response_obj(self):
        """_emit should not crash even if response_obj is garbage."""
        kwargs = {
            "model": "test",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        # This should degrade gracefully
        rec = _capture_emit(kwargs, response_obj="not a real response")
        self.assertEqual(rec["status"], "success")
        self.assertEqual(rec["http_status"], 200)


class TestEmitStreamField(unittest.TestCase):
    """Verify _emit includes stream indicator from kwargs."""

    def test_emit_stream_true_when_stream_in_kwargs(self):
        """When kwargs['stream'] is True, log should record stream=True."""
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "stream": True,
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["stream"], True)

    def test_emit_stream_none_when_missing(self):
        """When kwargs has no 'stream' key, log should record stream=None."""
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["stream"], None)


class TestStreamingCallbacks(unittest.TestCase):
    """Verify logger handles streaming success/failure callbacks."""

    def test_log_stream_event_is_noop(self):
        """Per-chunk hooks emit no PROXY_LOG output."""
        kwargs = {"model": "test-model", "call_type": "completion", "stream": True}
        response_obj = {
            "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
            "usage": {"completion_tokens": 5},
        }
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ProxyObservabilityLogger().log_stream_event(
                kwargs, response_obj, datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1)
            )
        finally:
            sys.stdout = old_stdout
        self.assertEqual(buf.getvalue(), "")

    def test_emit_with_all_none(self):
        """When everything is None, _emit should not crash."""
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            _emit(None, None, None, None, "failure")
        finally:
            sys.stdout = old_stdout
        line = buf.getvalue().strip()
        assert line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}"
        rec = json.loads(line[len("PROXY_LOG "):])
        assert rec["status"] == "failure"
        assert rec["ms"] is None


# ===================================================================
# ProxyObservabilityLogger class — verify dispatch through the singleton
# ===================================================================


class TestProxyObservabilityLogger(unittest.TestCase):
    """Verify the CustomLogger subclass dispatches to _emit correctly."""

    def test_log_success_event_emits(self):
        from litellm_logger import proxy_handler_instance

        kwargs = {"model": "test", "call_type": "completion"}
        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 1)

        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            proxy_handler_instance.log_success_event(kwargs, None, start, end)
        finally:
            sys.stdout = old_stdout

        line = buf.getvalue().strip()
        self.assertTrue(line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}")
        rec = json.loads(line[len("PROXY_LOG "):])
        self.assertEqual(rec["status"], "success")

    def test_log_failure_event_emits(self):
        from litellm_logger import proxy_handler_instance

        kwargs = {"model": "test", "call_type": "completion"}
        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 1)

        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            proxy_handler_instance.log_failure_event(kwargs, None, start, end)
        finally:
            sys.stdout = old_stdout

        line = buf.getvalue().strip()
        self.assertTrue(line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}")
        rec = json.loads(line[len("PROXY_LOG "):])
        self.assertEqual(rec["status"], "failure")


if __name__ == "__main__":
    unittest.main()