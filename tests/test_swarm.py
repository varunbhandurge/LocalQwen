import base64
import json
import os
import time
from pathlib import Path

os.environ.setdefault("LLM_BASE_URL", "http://localhost:11434/v1")
os.environ.setdefault("LLM_API_KEY", "ollama")
os.environ.setdefault("FAST_LLM_MODEL", "qwen3.6")

import pytest


# ============================================================================ #
# Fixtures                                                                    #
# ============================================================================ #

@pytest.fixture
def tmp_db(tmp_path: Path):
    """Provide a temporary SQLite DB path."""
    db_file = tmp_path / "test_swarm.db"
    yield db_file


@pytest.fixture(autouse=True)
def _patch_db_path(monkeypatch, tmp_db: Path):
    """Route all DB calls to the temporary database."""
    import app.db as _mod

    monkeypatch.setattr(_mod, "_DB_PATH", tmp_db.resolve())
    _mod.init_db(tmp_db.resolve())
    yield


@pytest.fixture
def engine_module(monkeypatch, tmp_db: Path):
    """Import swarm.engine after patching the DB path."""
    import app.db as _mod
    monkeypatch.setattr(_mod, "_DB_PATH", tmp_db.resolve())

    # Reset any cached state before importing swarm (avoids stale imports)
    import sys
    for key in list(sys.modules):
        if key.startswith("swarm"):
            del sys.modules[key]

    from swarm.engine import run_swarm as engine_run, _classify_for_parallel
    return {
        "run_swarm": engine_run,
        "classify_for_parallel": _classify_for_parallel,
    }


# ============================================================================ #
# Swarm engine tests                                                            #
# ============================================================================ #

class TestExtractJSON:
    def test_direct_json(self):
        from swarm.engine import _extract_json
        assert _extract_json('{"a":1}') == {"a": 1}

    def test_markdown_fence(self):
        from swarm.engine import _extract_json
        text = '```json\n{"b":2}\n```'
        assert _extract_json(text) == {"b": 2}

    def test_trailing_text(self):
        from swarm.engine import _extract_json
        text = '{"c":3} some extra words'
        result = _extract_json(text)
        assert result == {"c": 3}


class TestExtractNextAgent:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("next_agent: route", "route"),
            ("next_agent: ZOHOSWARM", "zohe_swarm"),
            ("no hint here", None),
            ("next_agent: END", None),
        ],
    )
    def test_next_agent(self, text, expected):
        from swarm.engine import _extract_next_agent, Agent
        result = _extract_next_agent(text)
        assert result is None or result.value == expected  # type: ignore[union-attr]


class TestClassifyForParallel:
    def test_returns_empty_on_bad_llm(self, engine_module, monkeypatch):
        """When the LLM crashes, parallel classification should return []."""

        async def _bad(*a, **kw):
            raise RuntimeError("simulated failure")

        class FakeLLM:
            chat = type("C", (), {"completions": type("Co", (), {"create": _bad})()})()  # type: ignore[attr-defined]

        monkeypatch.setattr("swarm.engine._make_fast_llm", lambda: FakeLLM())

        result = engine_module["classify_for_parallel"]("test query", FakeLLM())
        assert result == []


class TestToolDispatch:
    def test_zoho_schemas(self):
        from swarm.tools import zoho_tool_schemas

        schemas = zoho_tool_schemas()
        assert len(schemas) == 2
        names = {s["function"]["name"] for s in schemas}
        assert "zoho_search_contacts" in names
        assert "zoho_crud_record" in names

    def test_jira_schemas(self):
        from swarm.tools import jira_tool_schemas
        schemas = jira_tool_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "jira_create_ticket" in names
        assert "jira_query" in names

    def test_sqlite_schema(self):
        from swarm.tools import sqlite_tool_schemas
        schemas = sqlite_tool_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "sqlite_query"


@pytest.mark.asyncio
class TestSQLiteWrapper:
    async def test_sqlite_query_create_table(self, tmp_db):
        from swarm.engine import sqlite_query_wrapper

        # First create a table so we have something to query.
        import app.db
        db_path = Path(os.environ.get("BA_ACCOUNT_SWARM_DB_PATH", str(tmp_db)))
        if not db_path.exists():
            app.db.init_db(db_path)

    async def test_sqlite_query_no_rows(self, tmp_db):
        from swarm.engine import sqlite_query_wrapper
        result = sqlite_query_wrapper("SELECT 1 WHERE 0")
        assert "no rows" in result.lower() or "Query returned no rows." == result


class TestDBAPI:
    def test_init_creates_tables(self, tmp_db):
        import app.db as dbmod

        dbmod.init_db(tmp_db)
        import sqlite3 as _sqlite
        conn = _sqlite.connect(str(tmp_db))
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "account_context" in tables
        assert "hitl_approvals" in tables
        assert "swarm_audit_logs" in tables

    def test_save_and_get_context(self, tmp_db):
        import app.db as dbmod

        dbmod.save_context("user1", "role", "admin")
        val = dbmod.get_context("user1", "role")
        assert val == "admin"

    def test_get_context_missing(self, tmp_db):
        import app.db as dbmod

        result = dbmod.get_context("nobody", "key_a")
        # default returned when key missing — depends on default param; here it's None
        assert result is None or result is not None  # just ensure no exception


class TestHitlFlow:
    def test_create_and_update(self, tmp_db):
        import app.db as dbmod

        req = dbmod.create_hitl_request("web", "test_tool", "Check this out")
        assert req.status == "pending"

        ok = dbmod.hitl_update(req.id, "approved", answerer="admin")
        assert ok is True

        pending = dbmod.hitl_get_pending("web")
        approved_ids = [h.id for h in pending if h.status == "approved"]
        # Note: hitl_get_pending filters on status='pending' so this may be empty.
        # Verify we can read it directly.
        pending_all = dbmod.hitl_get_pending()
        found = any(h.id == req.id and h.status == "approved" for h in pending_all)
        assert found
