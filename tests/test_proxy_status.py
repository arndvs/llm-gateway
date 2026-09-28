"""Unit tests for scripts/proxy_status.py.

These exercise the URL resolution / validation / classification / health logic
directly, complementing the end-to-end `make claude-status` integration tests in
scripts/test_security.sh (which fake HOME and stub curl). Every edge case those
integration tests cover (hosted, IPv6 loopback, missing scheme, missing host,
malformed env, trailing slash) has a fast, deterministic unit test here.

Refs #81
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import proxy_status  # noqa: E402  (import after sys.path setup)


@pytest.fixture(autouse=True)
def _clear_proxy_base_url(monkeypatch):
    """Clear PROXY_BASE_URL so tests exercise the settings-only contract.

    resolve_proxy_url now honors PROXY_BASE_URL (refs #187); without clearing
    it, a developer's shell with PROXY_BASE_URL exported would change the
    resolution of every settings-only test.
    """
    monkeypatch.delenv("PROXY_BASE_URL", raising=False)


class TestResolveProxyUrl:
    def test_returns_configured_url(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.test"}}
        assert proxy_status.resolve_proxy_url(settings) == "https://proxy.example.test"

    def test_strips_trailing_slash(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.test/"}}
        assert proxy_status.resolve_proxy_url(settings) == "https://proxy.example.test"

    def test_empty_value_falls_back_to_localhost_port(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": ""}}
        assert proxy_status.resolve_proxy_url(settings, fallback_port="9999") == "http://localhost:9999"

    def test_missing_key_returns_none(self):
        assert proxy_status.resolve_proxy_url({"env": {"OTHER": "x"}}) is None

    def test_non_dict_env_returns_none(self):
        # Test 1f3: malformed env block ("env": []) → route Anthropic direct.
        assert proxy_status.resolve_proxy_url({"env": []}) is None

    def test_non_dict_settings_returns_none(self):
        assert proxy_status.resolve_proxy_url([]) is None

    def test_non_string_value_is_coerced_not_crashed(self):
        # A non-string ANTHROPIC_BASE_URL (malformed settings) must not raise; it
        # is coerced so validate_proxy_url can reject it as invalid.
        assert proxy_status.resolve_proxy_url({"env": {"ANTHROPIC_BASE_URL": 123}}) == "123"
        assert proxy_status.resolve_proxy_url({"env": {"ANTHROPIC_BASE_URL": True}}) == "True"

    def test_json_null_is_coerced_not_localhost_fallback(self):
        # An explicit JSON null must NOT be treated as "use localhost"; it is
        # coerced so validate_proxy_url rejects it (matches the old Makefile).
        assert proxy_status.resolve_proxy_url({"env": {"ANTHROPIC_BASE_URL": None}}) == "None"

    def test_proxy_base_url_is_honored(self, monkeypatch):
        # Ref #187: with PROXY_BASE_URL set and no ANTHROPIC_BASE_URL in
        # settings, status must report the same endpoint claude-enable would
        # write — not "routing direct".
        monkeypatch.setenv("PROXY_BASE_URL", "https://proxy.example.test")
        settings = {"env": {"OTHER": "x"}}
        assert proxy_status.resolve_proxy_url(settings) == "https://proxy.example.test"

    def test_proxy_base_url_wins_over_settings_url(self, monkeypatch):
        # Canonical precedence (proxy_endpoint.py): PROXY_BASE_URL beats
        # ANTHROPIC_BASE_URL. Status must agree with enable.
        monkeypatch.setenv("PROXY_BASE_URL", "https://proxy.example.test")
        settings = {"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000"}}
        assert proxy_status.resolve_proxy_url(settings) == "https://proxy.example.test"

    def test_proxy_base_url_empty_string_is_ignored(self, monkeypatch):
        # An empty PROXY_BASE_URL is treated as unset (matches the canonical
        # resolver's .strip() handling).
        monkeypatch.setenv("PROXY_BASE_URL", "")
        settings = {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.test"}}
        assert proxy_status.resolve_proxy_url(settings) == "https://proxy.example.test"


class TestValidateProxyUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost:4000",
            "https://proxy.example.test",
            "http://127.0.0.1:8080",
            "http://[::1]:4000",
        ],
    )
    def test_valid_urls(self, url):
        assert proxy_status.validate_proxy_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "proxy.example.test",   # Test 1f: no scheme
            "http://:4000",          # Test 1f2: no hostname
            "ftp://host",            # wrong scheme
            "",                       # empty
            "://nohost",             # no scheme, no host
        ],
    )
    def test_invalid_urls(self, url):
        assert proxy_status.validate_proxy_url(url) is False


class TestClassifyProxy:
    @pytest.mark.parametrize("url", ["http://localhost:4000", "http://127.0.0.1:8080", "http://[::1]:4000"])
    def test_local(self, url):
        # Test 1e: IPv6 loopback is local.
        assert proxy_status.classify_proxy(url) == "local"

    def test_hosted(self):
        assert proxy_status.classify_proxy("https://proxy.example.test") == "hosted"

    @pytest.mark.parametrize("url", [None, ""])
    def test_direct(self, url):
        assert proxy_status.classify_proxy(url) == "direct"


class TestProbeHealth:
    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            proxy_status.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})()
        )
        assert proxy_status.probe_health("http://x") is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(
            proxy_status.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 22})()
        )
        assert proxy_status.probe_health("http://x") is False

    def test_curl_missing_or_timeout_is_false(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("curl not found")

        monkeypatch.setattr(proxy_status.subprocess, "run", boom)
        assert proxy_status.probe_health("http://x") is False


class TestRenderStatus:
    def test_direct_when_no_proxy(self):
        lines = proxy_status.render_status({"env": {}}, probe=lambda url: True)
        assert lines[0] == "🌐 Routing: Anthropic API directly"

    def test_invalid_url(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "proxy.example.test"}}
        lines = proxy_status.render_status(settings, probe=lambda url: True)
        assert "Proxy URL in settings is invalid" in lines[0]

    def test_local_running(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000"}}
        lines = proxy_status.render_status(settings, probe=lambda url: True)
        assert lines[0] == "🔗 Routing: local proxy"
        assert lines[1] == "✅ Proxy: running at http://localhost:4000"

    def test_local_down_suggests_make_start(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "http://localhost:4000"}}
        lines = proxy_status.render_status(settings, probe=lambda url: False)
        assert lines[0] == "🔗 Routing: local proxy"
        assert "make start" in lines[1]

    def test_hosted_down_suggests_endpoint_check(self):
        settings = {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.test"}}
        lines = proxy_status.render_status(settings, probe=lambda url: False)
        assert lines[0] == "🔗 Routing: hosted proxy"
        assert "check the hosted proxy endpoint" in lines[1]

    def test_trailing_slash_stripped_in_health_line(self):
        # Test 1f4: the displayed URL must not carry a trailing slash.
        settings = {"env": {"ANTHROPIC_BASE_URL": "https://proxy.example.test/"}}
        lines = proxy_status.render_status(settings, probe=lambda url: False)
        assert "https://proxy.example.test " in lines[1]
        assert "https://proxy.example.test/ " not in lines[1]


class TestReadFallbackPort:
    def test_reads_litellm_port(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('LITELLM_PORT="8123"\nOTHER=1\n')
        assert proxy_status.read_fallback_port(str(env)) == "8123"

    def test_defaults_when_absent(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("OTHER=1\n")
        assert proxy_status.read_fallback_port(str(env)) == proxy_status.DEFAULT_PORT

    def test_defaults_when_file_missing(self, tmp_path):
        assert proxy_status.read_fallback_port(str(tmp_path / "nope.env")) == proxy_status.DEFAULT_PORT

    def test_ignores_lookalike_keys(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text('LITELLM_PORTAL="9999"\nLITELLM_PORT=8080\n')
        assert proxy_status.read_fallback_port(str(env)) == "8080"


class TestMain:
    def test_passes_make_default_port_to_fallback(self, tmp_path, monkeypatch):
        # `make claude-status PORT=XXXX` passes the port as argv[1]; main() must
        # feed it to read_fallback_port as the default (.env still takes precedence).
        # argv excludes the program name (argv[0] is stripped by main()).
        settings = tmp_path / "settings.json"
        settings.write_text('{"env": {"ANTHROPIC_BASE_URL": ""}}')
        captured = {}

        def fake_read(env_path=".env", default=proxy_status.DEFAULT_PORT):
            captured["default"] = default
            return default

        monkeypatch.setattr(proxy_status, "read_fallback_port", fake_read)
        proxy_status.main([str(settings), "6543"])
        assert captured["default"] == "6543"


class TestProxyEnvKeysManifest:
    """The PROXY_ENV_KEYS manifest must match what enable writes / disable removes.

    This is the drift guard for the single-source proxy-settings key manifest
    (refs #117): if a key is added to claude_enable.py but not the manifest,
    claude_disable.py would silently leave it behind, producing a half-disabled
    state. The manifest is the source of truth.
    """

    def test_manifest_is_tuple_of_strings(self):
        """PROXY_ENV_KEYS must be a tuple/frozenset of non-empty strings."""
        keys = proxy_status.PROXY_ENV_KEYS
        assert isinstance(keys, (tuple, frozenset)), (
            "PROXY_ENV_KEYS must be a tuple or frozenset"
        )
        assert keys, "PROXY_ENV_KEYS must not be empty"
        assert all(isinstance(k, str) and k for k in keys), (
            "PROXY_ENV_KEYS must contain only non-empty strings"
        )

    def test_manifest_contains_the_three_proxy_mode_keys(self):
        """The manifest must declare the canonical proxy-mode keys."""
        keys = set(proxy_status.PROXY_ENV_KEYS)
        assert {
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
        } <= keys

    def test_openrouter_key_is_not_in_manifest(self):
        """OPENROUTER_API_KEY is proxy-side, not a Claude-settings key."""
        assert "OPENROUTER_API_KEY" not in proxy_status.PROXY_ENV_KEYS

    def test_enable_writes_only_manifest_keys(self, tmp_path, monkeypatch):
        """claude_enable.py must write exactly the keys in the manifest.

        Runs claude_enable.py against an isolated settings file and asserts the
        written env keys exactly match PROXY_ENV_KEYS — no extras and none
        missing, so a key added to enable but not the manifest (or vice versa)
        fails the drift guard.
        """
        import subprocess
        import sys

        settings_file = tmp_path / "settings.json"
        settings_file.write_text("{}")
        env = {
            **os.environ,
            "LITELLM_MASTER_KEY": "sk-test",
            "LITELLM_PORT": "4000",
            "CLAUDE_SETTINGS_FILE": str(settings_file),
            "PYTHONPATH": str(REPO_ROOT / "scripts"),
        }
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "claude_enable.py")],
            env=env,
            check=True,
            capture_output=True,
        )
        import json

        written = json.loads(settings_file.read_text())["env"]
        assert set(written) == set(proxy_status.PROXY_ENV_KEYS), (
            f"claude_enable.py wrote keys {set(written) ^ set(proxy_status.PROXY_ENV_KEYS)} "
            f"that differ from PROXY_ENV_KEYS"
        )

    def test_disable_removes_only_manifest_keys(self, tmp_path, monkeypatch):
        """claude_disable.py must remove only keys in the manifest.

        A settings file with a proxy key plus an unrelated key must end with the
        unrelated key intact and all manifest keys removed.
        """
        import json
        import subprocess
        import sys

        # claude_disable.py uses Path.home() — point HOME (and USERPROFILE on
        # Windows) at tmp_path.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        # It writes to ~/.claude/settings.json, so create that path.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "http://localhost:4000",
                        "ANTHROPIC_AUTH_TOKEN": "sk-test",
                        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
                        "UNRELATED_KEY": "keep-me",
                    }
                }
            )
        )
        subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "claude_disable.py")],
            env={**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path)},
            check=True,
            capture_output=True,
        )
        remaining = json.loads((claude_dir / "settings.json").read_text())["env"]
        assert "UNRELATED_KEY" in remaining, "unrelated key must be preserved"
        assert not (set(proxy_status.PROXY_ENV_KEYS) & set(remaining)), (
            f"claude_disable.py left manifest keys behind: "
            f"{set(proxy_status.PROXY_ENV_KEYS) & set(remaining)}"
        )
