"""Stage 1 tests: the multi-engine SqlAlchemy backend, driven entirely by config.

Stage 1 lets a single .env attach the tool to any SQL database. These tests run
fully offline against a temporary SQLite database (no Postgres, no key), proving:
  * SqlAlchemyDatabase connects to a non-Postgres engine, reflects its schema, and
    satisfies the Database port,
  * the pipeline's governance still blocks denied columns on that engine,
  * the app boots on a SQLite database via DB_BACKEND=sqlalchemy,
  * an empty governance policy is allowed but logged as a warning at startup.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.ports import Database


@pytest.fixture()
def sqlite_file(tmp_path) -> str:
    seed = Path("eval/seed_sqlite.sql").read_text(encoding="utf-8")
    db_path = tmp_path / "shop.sqlite"
    con = sqlite3.connect(str(db_path))
    con.executescript(seed)
    con.commit()
    con.close()
    return str(db_path)


# ---- SqlAlchemyDatabase -----------------------------------------------------

def test_backend_reflects_schema_and_is_a_database(sqlite_file):
    from app.config import Settings
    from app.sqlalchemy_db import SqlAlchemyDatabase

    db = SqlAlchemyDatabase(f"sqlite:///{sqlite_file}", settings=Settings(groq_api_key="x"))
    try:
        assert isinstance(db, Database)          # satisfies the port
        assert db.dialect == "sqlite"
        schema = db.load_schema()
        assert "customers" in schema and "ssn" in schema["customers"]
        assert db.ping() is True
        r = db.execute("SELECT country, COUNT(*) AS n FROM customers GROUP BY country")
        assert r.ok and r.row_count == 4
    finally:
        db.close()


def test_dialect_mapping():
    from app.sqlalchemy_db import sqlglot_dialect_for
    assert sqlglot_dialect_for("postgresql") == "postgres"
    assert sqlglot_dialect_for("mysql") == "mysql"
    assert sqlglot_dialect_for("mariadb") == "mysql"
    assert sqlglot_dialect_for("sqlite") == "sqlite"
    assert sqlglot_dialect_for("mssql") == "tsql"
    assert sqlglot_dialect_for("something_unknown") == "postgres"  # safe default


def test_governance_blocks_denied_column_on_sqlite(sqlite_file):
    from app.config import Settings
    from app.pipeline import Pipeline
    from app.policy import Policy
    from app.providers.fake import FakeProvider
    from app.sqlalchemy_db import SqlAlchemyDatabase

    db = SqlAlchemyDatabase(f"sqlite:///{sqlite_file}", settings=Settings(groq_api_key="x"))
    try:
        s = Settings(groq_api_key="x", llm_provider="fake", cache_enabled=False,
                     num_samples=1, judge_mode="off", max_plan_cost=1e9,
                     max_plan_rows=1e9, denied_columns=["customers.ssn"],
                     sql_dialect=db.dialect)
        pol = Policy(denied_columns=["customers.ssn"])
        prov = FakeProvider(candidates=["SELECT ssn FROM customers"])
        ans = Pipeline(prov, db, settings=s, policy=pol).answer("show ssns")
        assert ans.blocked is True
        assert "customers.ssn" in (ans.block_reason or "")
    finally:
        db.close()


# ---- app boots on SQLite via .env-style config ------------------------------

@pytest.fixture()
def client(sqlite_file, monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "sqlalchemy")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{sqlite_file}")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("CACHE_ENABLED", "false")
    monkeypatch.setenv("NUM_SAMPLES", "1")
    monkeypatch.setenv("JUDGE_MODE", "off")
    monkeypatch.setenv("DENIED_COLUMNS", '["customers.ssn"]')

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.api as api
    importlib.reload(api)

    from fastapi.testclient import TestClient
    with TestClient(api.app) as c:
        yield c


def test_app_boots_on_sqlite_and_is_ready(client):
    r = client.get("/ready").json()
    assert r["status"] == "ready"


def test_empty_governance_logs_warning(sqlite_file, monkeypatch, caplog):
    """With no DENIED_COLUMNS/TABLES, the app still starts but logs a reminder."""
    monkeypatch.setenv("DB_BACKEND", "sqlalchemy")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{sqlite_file}")
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("DENIED_COLUMNS", "[]")
    monkeypatch.setenv("DENIED_TABLES", "[]")

    import importlib
    import app.config as config
    importlib.reload(config)
    import app.api as api
    importlib.reload(api)

    from fastapi.testclient import TestClient
    with caplog.at_level("WARNING", logger="text2sql.api"):
        with TestClient(api.app):
            pass
    assert any("governance" in rec.message for rec in caplog.records)
