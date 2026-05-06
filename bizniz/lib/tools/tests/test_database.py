"""Tests for database tool factories."""
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from bizniz.lib.tools.database import (
    _guess_db_service,
    build_database_handlers,
    make_query_database,
)


def _proc(stdout="", stderr="", returncode=0):
    p = MagicMock()
    p.stdout = stdout
    p.stderr = stderr
    p.returncode = returncode
    return p


class TestQueryDatabase:
    def test_no_compose(self):
        handler = make_query_database("", default_service="db")
        assert "ERROR: query_database unavailable" in handler({"sql": "select 1"})

    def test_no_sql(self):
        handler = make_query_database("/p/c.yml", default_service="db")
        assert "non-empty 'sql'" in handler({})

    def test_uses_default_service(self):
        handler = make_query_database("/p/c.yml", default_service="postgres")
        with patch("subprocess.run", return_value=_proc(stdout="1\n")) as m:
            out = handler({"sql": "select 1"})
        argv = m.call_args[0][0]
        assert "postgres" in argv
        assert "psql" in argv[-1]
        assert "select 1" in argv[-1]
        assert "1" in out

    def test_action_service_overrides(self):
        handler = make_query_database("/p/c.yml", default_service="postgres")
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as m:
            handler({"service": "alt_db", "sql": "select 1"})
        argv = m.call_args[0][0]
        assert "alt_db" in argv

    def test_no_service_no_default_no_compose_file(self):
        handler = make_query_database("/nonexistent.yml", default_service=None)
        out = handler({"sql": "select 1"})
        assert "could not auto-detect" in out

    def test_sql_quote_escaping(self):
        handler = make_query_database("/p/c.yml", default_service="postgres")
        with patch("subprocess.run", return_value=_proc(stdout="")) as m:
            handler({"sql": "select 'it''s'"})
        psql_cmd = m.call_args[0][0][-1]
        # Single quote in SQL must be escaped for shell
        assert "'\\''" in psql_cmd

    def test_timeout(self):
        handler = make_query_database("/p/c.yml", default_service="db")
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            out = handler({"sql": "select 1"})
        assert "timed out" in out

    def test_returncode_surfaced(self):
        handler = make_query_database("/p/c.yml", default_service="db")
        with patch("subprocess.run", return_value=_proc(stderr="ERROR: relation x does not exist", returncode=1)):
            out = handler({"sql": "select * from x"})
        assert "exit code: 1" in out
        assert "relation x does not exist" in out


class TestGuessDbService:
    def test_finds_postgres_image(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text(
            "services:\n"
            "  api: { image: ghcr.io/me/api:1 }\n"
            "  db:  { image: postgres:16 }\n"
        )
        assert _guess_db_service(str(compose)) == "db"

    def test_finds_postgis(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  geo: { image: postgis/postgis:16 }\n")
        assert _guess_db_service(str(compose)) == "geo"

    def test_returns_none_no_postgres(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  api: { image: nginx }\n")
        assert _guess_db_service(str(compose)) is None

    def test_returns_none_missing_file(self):
        assert _guess_db_service("/no/such/file") is None

    def test_autodetect_fallback_in_handler(self, tmp_path):
        compose = tmp_path / "compose.yml"
        compose.write_text("services:\n  pg: { image: postgres:16 }\n")
        handler = make_query_database(str(compose), default_service=None)
        with patch("subprocess.run", return_value=_proc(stdout="ok")) as m:
            handler({"sql": "select 1"})
        argv = m.call_args[0][0]
        assert "pg" in argv


class TestBuilder:
    def test_builder(self):
        handlers = build_database_handlers("/p/c.yml", default_service="db")
        assert set(handlers.keys()) == {"query_database"}
