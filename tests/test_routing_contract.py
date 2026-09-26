"""Offline routing-contract tests (refs #113, #133).

Boots a LiteLLM Router in-process against a mock upstream that records received
requests, and verifies the routing contract for the dual-provider structure:

- Each **primary** alias remaps to the correct OpenRouter model and carries an
  api_key.
- Each **fallback** alias remaps to the Copilot model and carries all four
  editor headers.
- ``router_settings.fallbacks`` wires each primary to its fallback.
- ``stream: true`` is set on the upstream request.
- ``thinking`` is stripped (``drop_params`` + ``additional_drop_params``).

Hermetic: no network, no secrets, no Docker. Runs in CI in well under 30s.
New model entries added to ``litellm_config.yaml`` are automatically covered
because aliases are discovered from the YAML.

NOTE on ``response_format``: the config declares it in ``additional_drop_params``
(the durable contract, asserted here), but whether litellm actually strips it at
runtime is version-dependent — the Anthropic adapter supports ``response_format``
and may keep it. We assert the config-level intent rather than a version-locked
runtime behavior.

Refs #113, #133
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "litellm_config.yaml"

# The four editor headers Copilot validates (single source: generate_config.py).
EDITOR_HEADERS = {
    "Editor-Version",
    "Editor-Plugin-Version",
    "Copilot-Integration-Id",
    "User-Agent",
}

FALLBACK_SUFFIX = "-fallback"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        pytest.skip(f"litellm_config.yaml not found at {CONFIG_PATH}")
    data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        pytest.fail(
            f"litellm_config.yaml did not parse as a mapping (got {type(data).__name__})"
        )
    return data


class _MockUpstream(BaseHTTPRequestHandler):
    """Records each POST and returns a provider-appropriate completion.

    The dual-provider config routes primaries to OpenRouter (OpenAI-style
    ``choices`` responses) and fallbacks to Copilot (Anthropic-style ``content``
    responses). The mock inspects the request path to return the matching
    format so the router treats the response as valid and does not fall back.
    """

    recorded: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.recorded.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(body) if body else None,
            }
        )
        # OpenRouter uses the OpenAI chat-completions path; Copilot uses the
        # Anthropic messages path. Return the matching response shape.
        if "/chat/completions" in self.path:
            resp = json.dumps(
                {
                    "id": "chatcmpl_mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "mock",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hi"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ).encode()
        else:
            resp = json.dumps(
                {
                    "id": "msg_mock",
                    "type": "message",
                    "role": "assistant",
                    "model": "mock",
                    "content": [{"type": "text", "text": "hi"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *args):  # silence request logging
        pass


@pytest.fixture(scope="module")
def router_and_mock():
    """Boot a LiteLLM Router against a mock upstream; yield (router, mock).

    Hermetic by construction: the mock `api_base` override is not enough to keep
    the `github_copilot/` provider off the network — LiteLLM's Copilot
    authenticator reads a token from `GITHUB_COPILOT_TOKEN_DIR` and, when absent,
    falls back to a device-code OAuth fetch (which CI's clean runner can't do →
    `GetAccessTokenError`). We point that dir at a temp file with a dummy token
    so Router init is hermetic everywhere (refs #113, #133, #146).
    """
    litellm = pytest.importorskip("litellm")
    from litellm import Router

    cfg = _load_config()

    # Hermetic Copilot token: point the authenticator at a temp dir with a
    # dummy access-token JSON so it never hits the network (CI-safe). The
    # authenticator reads a JSON file with {token, expires_at} (litellm 1.95).
    import os
    import tempfile

    token_dir = tempfile.mkdtemp(prefix="copilot-token-")
    api_key_name = os.environ.get(
        "GITHUB_COPILOT_API_KEY_FILE", "api-key.json"
    )
    with open(os.path.join(token_dir, api_key_name), "w") as f:
        import json

        json.dump(
            {
                "token": "sk-copilot-test-token",
                # Far future so the authenticator treats it as valid forever.
                "expires_at": 4102444800,  # 2100-01-01
            },
            f,
        )

    # Save prior env so we restore it in teardown.
    prior_dir = os.environ.get("GITHUB_COPILOT_TOKEN_DIR")
    prior_key_file = os.environ.get("GITHUB_COPILOT_API_KEY_FILE")
    os.environ["GITHUB_COPILOT_TOKEN_DIR"] = token_dir

    # Start the mock upstream on an ephemeral port.
    _MockUpstream.recorded = []
    server = HTTPServer(("127.0.0.1", 0), _MockUpstream)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # Point every deployment at the mock upstream so no real network is used.
    model_list = []
    for entry in cfg["model_list"]:
        params = dict(entry["litellm_params"])
        params["api_base"] = f"http://127.0.0.1:{port}"
        params["api_key"] = "test-key"
        model_list.append({"model_name": entry["model_name"], "litellm_params": params})

    router_settings = cfg.get("router_settings", {})
    litellm_settings = cfg.get("litellm_settings", {})
    # Mirror the proxy's litellm_settings.drop_params so unsupported params
    # (e.g. `thinking` on OpenRouter) are dropped exactly as the proxy does.
    litellm.drop_params = litellm_settings.get("drop_params", True)
    router = Router(
        model_list=model_list,
        fallbacks=router_settings.get("fallbacks", []),
        num_retries=router_settings.get("num_retries", 3),
    )

    try:
        yield router, _MockUpstream
    finally:
        server.shutdown()
        thread.join(timeout=5)
        # Restore prior env so other tests are unaffected.
        if prior_dir is not None:
            os.environ["GITHUB_COPILOT_TOKEN_DIR"] = prior_dir
        else:
            os.environ.pop("GITHUB_COPILOT_TOKEN_DIR", None)
        if prior_key_file is not None:
            os.environ["GITHUB_COPILOT_API_KEY_FILE"] = prior_key_file
        else:
            os.environ.pop("GITHUB_COPILOT_API_KEY_FILE", None)


def _primary_aliases(config: dict) -> list[str]:
    """Return primary (non-fallback, non-wildcard) model aliases from the YAML."""
    aliases = []
    for entry in config["model_list"]:
        name = entry["model_name"]
        if name.endswith(FALLBACK_SUFFIX) or name == "*":
            continue
        aliases.append(name)
    return aliases


def _fallback_aliases(config: dict) -> list[str]:
    """Return fallback model aliases from the YAML."""
    return [
        entry["model_name"]
        for entry in config["model_list"]
        if entry["model_name"].endswith(FALLBACK_SUFFIX)
    ]


class TestPrimaryRouting:
    """Each primary alias must remap to OpenRouter and carry an api_key."""

    def test_primary_remaps_to_openrouter_model(self, router_and_mock):
        router, mock = router_and_mock
        config = _load_config()
        for alias in _primary_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias,
                messages=[{"role": "user", "content": "hi"}],
                thinking={"type": "enabled"},
                stream=False,
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            body = mock.recorded[0]["body"]
            expected = next(
                e["litellm_params"]["model"]
                for e in config["model_list"]
                if e["model_name"] == alias
            )
            # litellm strips the openrouter/ provider prefix at runtime, so
            # the upstream sees the bare OpenRouter model id. Assert the suffix.
            expected_bare = expected.split("/", 1)[1]
            assert body["model"] == expected_bare, (
                f"alias {alias!r} remapped to {body['model']!r}, expected "
                f"{expected_bare!r} (from {expected!r})"
            )

    def test_primary_carries_api_key(self, router_and_mock):
        router, mock = router_and_mock
        config = _load_config()
        for alias in _primary_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias, messages=[{"role": "user", "content": "hi"}]
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            entry = next(
                e for e in config["model_list"] if e["model_name"] == alias
            )
            assert "api_key" in entry["litellm_params"], (
                f"alias {alias!r} primary must carry an api_key (OpenRouter)"
            )

    def test_primary_sets_stream_true(self, router_and_mock):
        router, mock = router_and_mock
        config = _load_config()
        for alias in _primary_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias, messages=[{"role": "user", "content": "hi"}]
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            assert mock.recorded[0]["body"]["stream"] is True, (
                f"alias {alias!r} did not set stream: true"
            )

    def test_thinking_param_is_stripped(self, router_and_mock):
        """thinking must be dropped (drop_params + additional_drop_params)."""
        router, mock = router_and_mock
        config = _load_config()
        # The proxy applies litellm_settings.additional_drop_params per request
        # (litellm's drop_params alone does not strip `thinking` in newer
        # versions). Mirror that here so the test asserts the durable contract
        # rather than a version-locked litellm default.
        additional = config.get("litellm_settings", {}).get(
            "additional_drop_params", []
        )
        for alias in _primary_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias,
                messages=[{"role": "user", "content": "hi"}],
                thinking={"type": "enabled"},
                additional_drop_params=additional,
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            assert "thinking" not in mock.recorded[0]["body"], (
                f"alias {alias!r} did not strip the thinking param"
            )


class TestFallbackRouting:
    """Each fallback alias must remap to Copilot and carry the editor headers."""

    def test_fallback_remaps_to_copilot_model(self, router_and_mock):
        router, mock = router_and_mock
        config = _load_config()
        for alias in _fallback_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias,
                messages=[{"role": "user", "content": "hi"}],
                stream=False,
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            body = mock.recorded[0]["body"]
            expected = next(
                e["litellm_params"]["model"]
                for e in config["model_list"]
                if e["model_name"] == alias
            )
            # litellm strips the github_copilot/ provider prefix at runtime, so
            # the upstream sees the bare Copilot model id. Assert the suffix.
            expected_bare = expected.split("/", 1)[1]
            assert body["model"] == expected_bare, (
                f"fallback {alias!r} remapped to {body['model']!r}, expected "
                f"{expected_bare!r} (from {expected!r})"
            )

    def test_fallback_carries_all_four_editor_headers(self, router_and_mock):
        router, mock = router_and_mock
        config = _load_config()
        for alias in _fallback_aliases(config):
            mock.recorded.clear()
            router.completion(
                model=alias, messages=[{"role": "user", "content": "hi"}]
            )
            assert mock.recorded, f"no upstream request recorded for {alias}"
            headers = mock.recorded[0]["headers"]
            missing = EDITOR_HEADERS - set(headers.keys())
            assert not missing, (
                f"fallback {alias!r} missing editor header(s): {sorted(missing)}"
            )


class TestFallbackWiring:
    """router_settings.fallbacks must wire each primary to its fallback."""

    def test_every_primary_has_fallback(self):
        config = _load_config()
        fallback_map: dict[str, list[str]] = {}
        for item in config.get("router_settings", {}).get("fallbacks", []):
            for model, fb_list in item.items():
                fallback_map[model] = fb_list
        for alias in _primary_aliases(config):
            assert alias in fallback_map, (
                f"primary {alias!r} has no router_settings.fallbacks entry"
            )
            assert f"{alias}{FALLBACK_SUFFIX}" in fallback_map[alias], (
                f"primary {alias!r} fallbacks do not include {alias}{FALLBACK_SUFFIX}"
            )


class TestParamDropContract:
    """The config must declare the param-drop intent (durable contract)."""

    def test_config_declares_response_format_and_thinking_in_additional_drop_params(self):
        config = _load_config()
        additional = config.get("litellm_settings", {}).get("additional_drop_params", [])
        assert "response_format" in additional, (
            "additional_drop_params must include 'response_format' (CONTEXT.md §1)"
        )
        assert "thinking" in additional, (
            "additional_drop_params must include 'thinking' (CONTEXT.md §1)"
        )
        assert config.get("litellm_settings", {}).get("drop_params") is True, (
            "litellm_settings.drop_params must be true (CONTEXT.md §1)"
        )
