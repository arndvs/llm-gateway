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

    def test_datetime_objects_zero_duration(self):
        t = datetime(2024, 1, 1, 12, 0, 0)
        self.assertEqual(_duration_ms(t, t), 0)

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

    def test_empty_content_string(self):
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": ""},
                }
            ],
            "usage": {"completion_tokens": 0},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 0)
        self.assertEqual(ctoks, 0)

    def test_none_content(self):
        """When content is None, content_len should be 0."""
        response = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": None},
                }
            ],
            "usage": {"completion_tokens": 3},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 0)
        self.assertEqual(ctoks, 3)

    def test_non_string_content_tool_calls(self):
        """Non-string content (e.g. tool call list) gives content_len=-1."""
        response = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"content": [{"type": "function", "name": "foo"}]},
                }
            ],
            "usage": {"completion_tokens": 10},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "tool_calls")
        self.assertEqual(content_len, -1)
        self.assertEqual(ctoks, 10)

    def test_missing_usage(self):
        """When usage is missing, completion_tokens should be None."""
        response = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": "truncated"},
                }
            ],
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "length")
        self.assertEqual(content_len, 9)  # len("truncated")
        self.assertIsNone(ctoks)

    def test_missing_message(self):
        """When message is missing from choice, content_len should be 0."""
        response = {
            "choices": [{"finish_reason": "stop", "message": None}],
            "usage": {"completion_tokens": 0},
        }
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        # message is None, isinstance(None, dict) is False, so content stays None
        # then content in (None, "") is True => content_len = 0
        self.assertEqual(content_len, 0)
        self.assertEqual(ctoks, 0)


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

    def test_object_style_none_content(self):
        msg = SimpleNamespace(content=None)
        choice = SimpleNamespace(finish_reason="stop", message=msg)
        response = SimpleNamespace(
            choices=[choice],
            content=None,
            usage=SimpleNamespace(completion_tokens=0),
        )
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "stop")
        self.assertEqual(content_len, 0)
        self.assertEqual(ctoks, 0)

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

    def test_empty_content_list(self):
        """An empty content list should give content_len=0."""
        response = {
            "content": [],
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

    def test_anthropic_object_style(self):
        """Anthropic-style response as SimpleNamespace objects."""
        block = SimpleNamespace(type="text", text="Object text")
        response = SimpleNamespace(
            choices=None,
            content=[block],
            stop_reason="end_turn",
            usage=SimpleNamespace(completion_tokens=3),
        )
        finish, content_len, ctoks = _extract(response)
        self.assertEqual(finish, "end_turn")
        self.assertEqual(content_len, 11)  # len("Object text")
        self.assertEqual(ctoks, 3)

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


class TestExtractDefensive(unittest.TestCase):
    """Defensive behavior: _extract never raises."""

    def test_none_response(self):
        finish, content_len, ctoks = _extract(None)
        self.assertIsNone(finish)
        self.assertIsNone(content_len)
        self.assertIsNone(ctoks)

    def test_empty_dict(self):
        finish, content_len, ctoks = _extract({})
        self.assertIsNone(finish)
        self.assertIsNone(content_len)
        self.assertIsNone(ctoks)

    def test_empty_choices_list(self):
        """Empty choices list should not crash (IndexError)."""
        response = {"choices": [], "usage": {"completion_tokens": 0}}
        finish, content_len, ctoks = _extract(response)
        # choices is truthy? No, [] is falsy in Python. Falls through.
        self.assertIsNone(finish)
        self.assertIsNone(content_len)
        self.assertEqual(ctoks, 0)

    def test_garbage_input(self):
        finish, content_len, ctoks = _extract("not a response")
        self.assertIsNone(finish)
        self.assertIsNone(content_len)
        self.assertIsNone(ctoks)


# ===================================================================
# _extract_http_info — unit tests
# ===================================================================


class TestExtractHttpInfoFromOriginalResponse(unittest.TestCase):
    """Extract http_status and ratelimit from kwargs['original_response']."""

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

    def test_429_with_ratelimit(self):
        headers = {
            "x-ratelimit-limit": "60",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1700000060",
        }
        kwargs = {"original_response": FakeHttpxResponse(429, headers)}
        info = _extract_http_info(kwargs)
        self.assertEqual(info["http_status"], 429)
        self.assertEqual(info["ratelimit"]["remaining"], "0")


class TestExtractHttpInfoFromException(unittest.TestCase):
    """Extract http_status and ratelimit from kwargs['exception']."""

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


class TestExtractHttpInfoDefensive(unittest.TestCase):
    """Defensive behaviour: never raise, degrade gracefully."""

    def test_empty_kwargs(self):
        info = _extract_http_info({})
        self.assertIsNone(info["http_status"])
        self.assertNotIn("ratelimit", info)

    def test_none_kwargs(self):
        info = _extract_http_info(None)
        self.assertIsNone(info["http_status"])

    def test_non_dict_kwargs(self):
        info = _extract_http_info("garbage")
        self.assertIsNone(info["http_status"])

    def test_original_response_is_string(self):
        """original_response might be a raw string in some LiteLLM paths."""
        kwargs = {"original_response": "some raw text"}
        info = _extract_http_info(kwargs)
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

    def test_emit_includes_http_status(self):
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["http_status"], 200)

    def test_emit_omits_ratelimit_when_absent(self):
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200, {"content-type": "application/json"}),
        }
        rec = _capture_emit(kwargs)
        self.assertNotIn("ratelimit", rec)

    def test_emit_includes_ratelimit_when_present(self):
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(
                200,
                {"x-ratelimit-remaining": "10", "x-ratelimit-limit": "100"},
            ),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["ratelimit"], {"remaining": "10", "limit": "100"})

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

    def test_emit_null_http_status_when_no_response(self):
        kwargs = {"model": "test-model", "call_type": "completion"}
        rec = _capture_emit(kwargs)
        self.assertIsNone(rec["http_status"])

    def test_emit_never_logs_content(self):
        """Ensure no message content leaks into the log line."""
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        response_obj = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "SECRET_CONTENT_SHOULD_NOT_APPEAR"},
                }
            ],
            "usage": {"completion_tokens": 10},
        }
        rec = _capture_emit(kwargs, response_obj=response_obj)
        raw_line = json.dumps(rec)
        self.assertNotIn("SECRET_CONTENT_SHOULD_NOT_APPEAR", raw_line)


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

    def test_fallback_detected_via_resolved_model_suffix(self):
        # Even when the requested alias has no -fallback suffix, a resolved
        # model that IS a fallback lane must be flagged.
        kwargs = {
            "model": "claude-sonnet-4-6",
            "call_type": "completion",
            "litellm_params": {"model": "claude-sonnet-4-6-fallback"},
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
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

    def test_non_dict_litellm_params_degrades_to_none(self):
        kwargs = {
            "model": "claude-sonnet-4-6",
            "call_type": "completion",
            "litellm_params": "not-a-dict",
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

    def test_upstream_empty_false_on_failure(self):
        """upstream_empty should be False even if content is empty on failure."""
        kwargs = {
            "model": "test",
            "call_type": "completion",
            "exception": FakeException(500),
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
        rec = _capture_emit(kwargs, response_obj=response_obj, status="failure")
        self.assertFalse(rec["upstream_empty"])

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


class TestEmitDefensiveNoCrash(unittest.TestCase):
    """_emit must never raise, even with terrible inputs."""

    def test_emit_with_none_kwargs(self):
        """When kwargs is None, _emit should not crash."""
        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 1)
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            _emit(None, None, start, end, "success")
        finally:
            sys.stdout = old_stdout
        raw_line = buf.getvalue().strip()
        assert "SECRET_CONTENT_SHOULD_NOT_APPEAR" not in raw_line


# ===================================================================
# _emit integration — stream field in PROXY_LOG
# ===================================================================


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

    def test_emit_stream_false_when_stream_false_in_kwargs(self):
        """When kwargs['stream'] is False, log should record stream=False."""
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "stream": False,
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["stream"], False)

    def test_emit_stream_none_when_missing(self):
        """When kwargs has no 'stream' key, log should record stream=None."""
        kwargs = {
            "model": "test-model",
            "call_type": "completion",
            "original_response": FakeHttpxResponse(200),
        }
        rec = _capture_emit(kwargs)
        self.assertEqual(rec["stream"], None)


# ===================================================================
# ProxyObservabilityLogger — streaming callback methods
# ===================================================================


class TestStreamingCallbacks(unittest.TestCase):
    """Verify logger handles streaming success/failure callbacks."""

    def _capture_logger_call(self, method_name, kwargs, response_obj=None):
        """Call a logger method and capture the PROXY_LOG line."""
        from datetime import datetime
        import asyncio

        start = datetime(2024, 1, 1, 0, 0, 0)
        end = datetime(2024, 1, 1, 0, 0, 1)
        logger = ProxyObservabilityLogger()

        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            method = getattr(logger, method_name)
            if method_name.startswith("async_"):
                asyncio.run(method(kwargs, response_obj, start, end))
            else:
                method(kwargs, response_obj, start, end)
        finally:
            sys.stdout = old_stdout

        line = buf.getvalue().strip()
        assert line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}"
        return json.loads(line[len("PROXY_LOG "):])

    def test_log_stream_success_event(self):
        """log_stream_event is a no-op: per-chunk hooks produce no PROXY_LOG output."""
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

    def test_async_log_stream_event_noop(self):
        """async_log_stream_event is a no-op: per-chunk hooks produce no PROXY_LOG output."""
        kwargs = {"model": "test-model", "call_type": "completion", "stream": True}
        response_obj = {
            "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
            "usage": {"completion_tokens": 5},
        }
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            asyncio.run(
                ProxyObservabilityLogger().async_log_stream_event(
                    kwargs, response_obj, datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1)
                )
            )
        finally:
            sys.stdout = old_stdout
        self.assertEqual(buf.getvalue(), "")

    def test_async_log_success_event_emits(self):
        """async_log_success_event returns an awaitable and emits a PROXY_LOG line.

        Regression for the Python 3.12 RuntimeError: the async callback must
        await the sync emit through the event loop, not call it directly.
        """
        kwargs = {"model": "test-model", "call_type": "completion", "stream": True}
        response_obj = {
            "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
            "usage": {"completion_tokens": 5},
        }
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            asyncio.run(
                ProxyObservabilityLogger().async_log_success_event(
                    kwargs, response_obj, datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1)
                )
            )
        finally:
            sys.stdout = old_stdout
        line = buf.getvalue().strip()
        assert line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}"
        rec = json.loads(line[len("PROXY_LOG "):])
        assert rec["status"] == "success"
        assert rec["model"] == "test-model"

    def test_async_log_failure_event_emits(self):
        """async_log_failure_event returns an awaitable and emits a PROXY_LOG line."""
        kwargs = {"model": "test-model", "call_type": "completion", "stream": True}
        buf = StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            asyncio.run(
                ProxyObservabilityLogger().async_log_failure_event(
                    kwargs, None, datetime(2024, 1, 1), datetime(2024, 1, 1, 0, 0, 1)
                )
            )
        finally:
            sys.stdout = old_stdout
        line = buf.getvalue().strip()
        assert line.startswith("PROXY_LOG "), f"Expected PROXY_LOG prefix, got: {line!r}"
        rec = json.loads(line[len("PROXY_LOG "):])
        assert rec["status"] == "failure"
        assert rec["model"] == "test-model"

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
