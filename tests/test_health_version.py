"""Tests for health_version — the /health/version endpoint's get_version() logic."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestGetVersion:
    """Unit tests for get_version() — the core logic."""

    def test_returns_sha_from_env(self, monkeypatch):
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        monkeypatch.setenv("BUILD_TIMESTAMP", "2024-01-15T10:30:00Z")
        from health_version import get_version

        result = get_version()
        assert result["sha"] == "abc1234"

    def test_full_sha_is_truncated_to_7_chars(self, monkeypatch):
        """A full 40-char SHA baked in at build time should be trimmed to 7 chars."""
        full_sha = "a" * 40
        monkeypatch.setenv("BUILD_SHA", full_sha)
        from health_version import get_version

        result = get_version()
        assert result["sha"] == "a" * 7, f"Expected 7-char SHA, got {result['sha']!r}"

    def test_returns_built_at_from_env(self, monkeypatch):
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        monkeypatch.setenv("BUILD_TIMESTAMP", "2024-01-15T10:30:00Z")
        from health_version import get_version

        result = get_version()
        assert result["built_at"] == "2024-01-15T10:30:00Z"

    def test_sha_defaults_to_unknown_when_env_and_git_both_fail(self, monkeypatch):
        """'unknown' is returned only when both env and git are unavailable."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        monkeypatch.delenv("BUILD_TIMESTAMP", raising=False)
        from health_version import get_version

        with patch("health_version.subprocess.run", side_effect=Exception("git not found")):
            result = get_version()
        assert result["sha"] == "unknown"

    def test_response_has_exactly_two_keys(self, monkeypatch):
        monkeypatch.setenv("BUILD_SHA", "deadbee")
        monkeypatch.setenv("BUILD_TIMESTAMP", "2024-06-01T00:00:00Z")
        from health_version import get_version

        result = get_version()
        assert set(result.keys()) == {"sha", "built_at"}

    def test_git_fallback_sha_truncated_to_7_chars(self, monkeypatch):
        """git rev-parse --short can return >7 chars in large repos; must be trimmed."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        from health_version import get_version

        with patch("health_version.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "abcdef1234567890\n"  # 16-char abbrev
            result = get_version()

        assert result["sha"] == "abcdef1", f"Expected 7-char SHA, got {result['sha']!r}"

    def test_built_at_unknown_when_env_is_empty_string(self, monkeypatch):
        """BUILD_TIMESTAMP='' (empty) normalizes to 'unknown', not an empty string."""
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        monkeypatch.setenv("BUILD_TIMESTAMP", "")
        from health_version import get_version

        result = get_version()
        assert result["built_at"] == "unknown"


class TestCustomApiRouter:
    """Verify the module exports a custom_api_router compatible with LiteLLM."""

    def test_module_exports_custom_api_router(self):
        import health_version

        assert hasattr(health_version, "custom_api_router")

    def test_router_has_health_version_route(self):
        import health_version

        routes = [r.path for r in health_version.custom_api_router.routes]
        assert "/health/version" in routes


class TestRegisterRouter:
    """Verify the module exports its integration points and degrades outside the proxy."""

    def test_module_exports_callback_and_router(self):
        """The callback instance and router are what registration wires up."""
        import health_version

        assert health_version.version_callback_instance is not None

    def test_register_router_does_not_raise_outside_proxy(self):
        """Calling _register_router outside a litellm proxy context must be safe."""
        import health_version

        health_version._register_router()


class TestGitFallback:
    """Unit tests for the git rev-parse fallback path in get_version()."""

    def test_sha_falls_back_to_git_when_env_unset(self, monkeypatch):
        """When BUILD_SHA is missing, git provides the SHA."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        from health_version import get_version

        with patch("health_version.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "deadbee\n"
            result = get_version()

        assert result["sha"] == "deadbee"
        mock_run.assert_called_once()

    def test_sha_falls_back_to_git_when_env_is_unknown(self, monkeypatch):
        """'unknown' (the Dockerfile default) triggers the git fallback."""
        monkeypatch.setenv("BUILD_SHA", "unknown")
        from health_version import get_version

        with patch("health_version.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "face123\n"
            result = get_version()

        assert result["sha"] == "face123"

    def test_sha_unknown_when_git_fails(self, monkeypatch):
        """If git raises, sha falls back to 'unknown' — never propagates the exception."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        from health_version import get_version

        with patch("health_version.subprocess.run", side_effect=Exception("git not found")):
            result = get_version()

        assert result["sha"] == "unknown"

    def test_sha_unknown_when_git_returns_nonzero(self, monkeypatch):
        """Non-zero git exit code is treated the same as a failure."""
        monkeypatch.delenv("BUILD_SHA", raising=False)
        from health_version import get_version

        with patch("health_version.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            result = get_version()

        assert result["sha"] == "unknown"

    def test_git_not_called_when_build_sha_is_set(self, monkeypatch):
        """git is never invoked when BUILD_SHA is already baked in."""
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        from health_version import get_version

        with patch("health_version.subprocess.run") as mock_run:
            get_version()

        mock_run.assert_not_called()


class TestBuiltAtEdgeCases:
    """Edge cases for the built_at field."""

    def test_built_at_unknown_when_env_is_literal_unknown(self, monkeypatch):
        """The Dockerfile default 'unknown' for BUILD_TIMESTAMP stays as 'unknown'."""
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        monkeypatch.setenv("BUILD_TIMESTAMP", "unknown")
        from health_version import get_version

        result = get_version()
        assert result["built_at"] == "unknown"

    def test_built_at_unknown_when_env_unset(self, monkeypatch):
        """BUILD_TIMESTAMP not set at all → 'unknown'."""
        monkeypatch.setenv("BUILD_SHA", "abc1234")
        monkeypatch.delenv("BUILD_TIMESTAMP", raising=False)
        from health_version import get_version

        result = get_version()
        assert result["built_at"] == "unknown"


class TestSingleRouteRegistration:
    """Regression guard: exactly one /health/version GET route must be registered.

    This test exists to catch any future attempt to register the route via a
    second mechanism (e.g. re-introducing LITELLM_WORKER_STARTUP_HOOKS or a
    second module).  If this test fails, a route collision has been introduced.

    Two levels of assertion:
    - Router level: custom_api_router carries exactly one route definition.
    - App level: including the router on a simulated FastAPI app produces exactly
      one included router entry and the endpoint responds correctly (the app-level
      check Copilot requested so a second module's registration would be caught).
    """

    def test_custom_api_router_has_exactly_one_route(self):
        import health_version

        routes = health_version.custom_api_router.routes
        assert len(routes) == 1, (
            f"Expected exactly 1 route on custom_api_router, found {len(routes)}: "
            f"{[r.path for r in routes]}"
        )

    def test_single_route_is_health_version_get(self):
        import health_version

        route = health_version.custom_api_router.routes[0]
        assert route.path == "/health/version"
        assert "GET" in route.methods

    def test_exactly_one_health_version_route_on_simulated_app(self):
        """Include custom_api_router on a real FastAPI app and assert exactly one
        GET /health/version route exists, using a recursive route collector that
        handles both eager-flattening (older FastAPI) and lazy _IncludedRouter
        (FastAPI 0.100+) registration styles.

        A second module registering GET /health/version via either @app.get() or
        a second include_router() would cause the count to exceed 1 and fail.
        """
        from fastapi import FastAPI
        from fastapi.routing import APIRoute
        from fastapi.testclient import TestClient
        import health_version

        def _collect_get_routes(routes, path: str) -> list:
            """Recursively collect all GET APIRoute objects for a given path.

            Handles both direct APIRoute registrations and lazy _IncludedRouter
            objects (FastAPI 0.100+), which nest their routes under original_router.
            """
            found = []
            for r in routes:
                if isinstance(r, APIRoute) and r.path == path and "GET" in (r.methods or set()):
                    found.append(r)
                # _IncludedRouter (FastAPI 0.100+) stores the original router;
                # recurse to find APIRoute objects nested inside it.
                if hasattr(r, "original_router") and hasattr(r.original_router, "routes"):
                    found.extend(_collect_get_routes(r.original_router.routes, path))
            return found

        app = FastAPI()
        app.include_router(health_version.custom_api_router)

        hv_routes = _collect_get_routes(app.routes, "/health/version")
        assert len(hv_routes) == 1, (
            f"Expected exactly 1 GET /health/version route, found {len(hv_routes)} — "
            f"a second module may have registered the route"
        )

        # Functional check: the route is reachable and returns the correct schema.
        client = TestClient(app)
        resp = client.get("/health/version")
        assert resp.status_code == 200
        assert set(resp.json().keys()) == {"sha", "built_at"}
