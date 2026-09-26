#!/usr/bin/env python3
"""DB-mode guard — "database mode requires a reachable database" (refs #169).

The repo's most consequential misconfiguration rule was documented four times
and enforced nowhere: setting ``DATABASE_URL`` without a reachable Postgres
makes LiteLLM enter DB mode and serve ``400 "No connected db"`` on EVERY
request — the proxy "starts fine" and only every user sees the outage.

This module is the single executable contract. Two functions:

- ``resolve_db_mode(env)`` — ``"db-less"`` when ``DATABASE_URL`` is
  unset/empty, ``"db"`` otherwise.
- ``assert_db_reachable(database_url)`` — minimal, dependency-free
  connectivity check (stdlib ``socket`` connect to the Postgres host:port
  parsed from the URL). Prints the canonical error line and returns non-zero
  when unreachable.

stdlib-only. No network libraries, no subprocess.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Dict, Optional
from urllib.parse import urlparse

# The canonical error line — every guard path (entrypoint, launcher, tests)
# prints this exact message so operators recognize the misconfiguration.
CANONICAL_ERROR = (
    "DATABASE_URL set but no reachable database — start the db service "
    "(docker compose -f docker-compose.yml -f docker-compose.db.yml up) "
    "or unset DATABASE_URL for DB-less mode"
)

DEFAULT_PORT = "5432"


def resolve_db_mode(env: Optional[Dict[str, str]] = None) -> str:
    """Return ``"db-less"`` when DATABASE_URL is unset/empty, else ``"db"``."""
    env = env if env is not None else os.environ
    url = (env.get("DATABASE_URL") or "").strip()
    return "db" if url else "db-less"


def _parse_host_port(database_url: str):
    """Return (host, port) from a postgres:// URL, defaulting port 5432."""
    parsed = urlparse(database_url)
    host = parsed.hostname or "localhost"
    port = parsed.port or DEFAULT_PORT
    return host, int(port)


def assert_db_reachable(database_url: str, timeout: float = 3.0) -> int:
    """Check Postgres reachability; print canonical error + return non-zero.

    Returns 0 when reachable, 1 when not. Never raises — a guard must not
    crash the launcher with a traceback.
    """
    try:
        host, port = _parse_host_port(database_url)
        with socket.create_connection((host, port), timeout=timeout):
            return 0
    except Exception as exc:
        print(CANONICAL_ERROR, file=sys.stderr)
        print(f"  (could not connect to {database_url!r}: {exc})", file=sys.stderr)
        return 1


def main(argv: Optional[list] = None) -> int:
    """CLI: exit 0 when DB mode is configured correctly, non-zero otherwise.

    Usage: python3 scripts/db_mode_guard.py
    - DATABASE_URL unset/empty → exit 0 (db-less is always fine)
    - DATABASE_URL set + reachable → exit 0
    - DATABASE_URL set + unreachable → exit 1 with the canonical error
    """
    import sys

    argv = argv if argv is not None else sys.argv
    if resolve_db_mode() == "db-less":
        return 0
    return assert_db_reachable(os.environ.get("DATABASE_URL", ""))


if __name__ == "__main__":
    raise SystemExit(main())