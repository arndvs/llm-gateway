"""Unit tests for scripts/db_mode_guard.py (refs #169).

The DB-mode boundary: DATABASE_URL set requires a reachable Postgres. These
tests exercise resolve_db_mode and assert_db_reachable directly.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import db_mode_guard  # noqa: E402


class TestResolveDbMode:
    def test_unset_is_db_less(self):
        assert db_mode_guard.resolve_db_mode({}) == "db-less"

    def test_empty_string_is_db_less(self):
        assert db_mode_guard.resolve_db_mode({"DATABASE_URL": ""}) == "db-less"

    def test_whitespace_is_db_less(self):
        assert db_mode_guard.resolve_db_mode({"DATABASE_URL": "   "}) == "db-less"

    def test_set_is_db(self):
        assert (
            db_mode_guard.resolve_db_mode({"DATABASE_URL": "postgresql://u:p@db:5432/l"})
            == "db"
        )

    def test_defaults_to_os_environ(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert db_mode_guard.resolve_db_mode() == "db-less"
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db:5432/l")
        assert db_mode_guard.resolve_db_mode() == "db"


class TestAssertDbReachable:
    def test_unreachable_returns_nonzero_with_canonical_message(self, capsys):
        # A closed port on loopback is reliably unreachable.
        rc = db_mode_guard.assert_db_reachable("postgresql://u:p@127.0.0.1:1/l")
        assert rc != 0
        captured = capsys.readouterr()
        assert db_mode_guard.CANONICAL_ERROR in captured.err

    def test_reachable_returns_zero(self):
        # Bind a real listener on an ephemeral port and connect to it.
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def _accept():
            try:
                conn, _ = listener.accept()
                conn.close()
            except Exception:
                pass

        thread = threading.Thread(target=_accept, daemon=True)
        thread.start()
        try:
            rc = db_mode_guard.assert_db_reachable(
                f"postgresql://u:p@127.0.0.1:{port}/l"
            )
            assert rc == 0
        finally:
            listener.close()
            thread.join(timeout=2)

    def test_malformed_url_returns_nonzero(self, capsys):
        rc = db_mode_guard.assert_db_reachable("not-a-url")
        assert rc != 0
        assert db_mode_guard.CANONICAL_ERROR in capsys.readouterr().err

    def test_default_port_5432_when_absent(self):
        host, port = db_mode_guard._parse_host_port("postgresql://u:p@db/l")
        assert host == "db"
        assert port == 5432

    def test_explicit_port_parsed(self):
        host, port = db_mode_guard._parse_host_port("postgresql://u:p@db:5433/l")
        assert host == "db"
        assert port == 5433


class TestMain:
    def test_db_less_exits_zero(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        assert db_mode_guard.main([]) == 0

    def test_db_unreachable_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/l")
        assert db_mode_guard.main([]) != 0
        assert db_mode_guard.CANONICAL_ERROR in capsys.readouterr().err