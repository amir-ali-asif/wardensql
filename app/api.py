"""FastAPI service: versioned API, auth, rate limiting, health/readiness, metrics.

Endpoints:
  POST /v1/ask         -> ask a question (X-API-Key required if api_keys configured)
  GET  /health         -> liveness (process is up)
  GET  /ready          -> readiness (database reachable)
  GET  /metrics        -> Prometheus text metrics
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .config import settings
from .observability import configure_logging, metrics
from .pipeline import PIPELINE_STAGES, Answer, Pipeline, StepEvent
from .policy import Policy
from .providers import get_provider
from .ratelimit import RateLimiter

configure_logging()

logger = logging.getLogger("text2sql.api")

_STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Text-to-SQL", version="1.1.0", description=__doc__)

_db = None
_pipeline: Pipeline | None = None
_limiter = RateLimiter(settings.rate_limit_per_minute)


def _build_database():
    """Build the database chosen by config at startup.

    DB_BACKEND=postgres    -> original psycopg PostgresDatabase (default).
    DB_BACKEND=sqlalchemy  -> multi-engine backend driven by DATABASE_URL, so a
                              single .env can point the tool at Postgres / MySQL /
                              SQLite / SQL Server. Returns (db, sqlglot_dialect).
    """
    if settings.db_backend == "sqlalchemy":
        from .sqlalchemy_db import SqlAlchemyDatabase
        db = SqlAlchemyDatabase(settings.database_url, settings=settings)
        return db, db.dialect
    from .db import PostgresDatabase
    return PostgresDatabase(settings), settings.sql_dialect


@app.on_event("startup")
def _startup() -> None:
    global _db, _pipeline
    _db, dialect = _build_database()

    # Keep the pipeline's SQL dialect in step with the connected backend so the AST
    # layers parse the right flavor of SQL.
    from copy import copy
    run_settings = copy(settings)
    run_settings.sql_dialect = dialect

    # Governance is configured entirely in .env (single source of truth). If no
    # columns/tables are denied we still start, but log a visible reminder so an
    # empty policy is never a silent oversight when attaching a new database.
    if not settings.denied_columns and not settings.denied_tables:
        logger.warning(
            "governance: no DENIED_COLUMNS or DENIED_TABLES set for database '%s' "
            "-- every column is currently answerable. Set them in .env to lock down "
            "sensitive data.",
            settings.database_url.rsplit("/", 1)[-1].split("?")[0],
        )

    policy = Policy(
        allowed_tables=settings.allowed_tables,
        denied_tables=settings.denied_tables,
        denied_columns=settings.denied_columns,
        table_aliases=settings.table_aliases,
    )
    _pipeline = Pipeline(get_provider(settings), _db, settings=run_settings, policy=policy)


@app.on_event("shutdown")
def _shutdown() -> None:
    if _db is not None and hasattr(_db, "close"):
        _db.close()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=settings.max_question_chars)


def _authorize(request: Request, x_api_key: str | None = Header(default=None)) -> str:
    if settings.api_keys:
        if x_api_key not in settings.api_keys:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        principal = x_api_key
    else:
        principal = request.client.host if request.client else "anonymous"
    if not _limiter.allow(principal):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    return principal


@app.post("/v1/ask")
def ask(body: AskRequest, principal: str = Depends(_authorize)) -> dict:
    assert _pipeline is not None
    request_id = str(uuid.uuid4())
    ans: Answer = _pipeline.answer(body.question, request_id=request_id)
    payload = ans.to_dict()
    payload["request_id"] = request_id
    return payload


@app.get("/")
def index() -> FileResponse:
    """The web UI (single page, no build step)."""
    return FileResponse(_STATIC_DIR / "index.html")


def _authorize_stream(request: Request, api_key: str | None = None) -> str:
    """Auth for the SSE endpoint. Browsers' EventSource can't send custom headers,
    so the key arrives as a query parameter here instead of X-API-Key."""
    if settings.api_keys:
        if api_key not in settings.api_keys:
            raise HTTPException(status_code=401, detail="invalid or missing API key")
        principal = api_key
    else:
        principal = request.client.host if request.client else "anonymous"
    if not _limiter.allow(principal):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    return principal


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, default=str) + "\n\n"


@app.get("/v1/ask/stream")
def ask_stream(question: str, request: Request, api_key: str | None = None) -> StreamingResponse:
    """Run a question and stream each pipeline stage as it resolves (Server-Sent
    Events). The pipeline runs in a worker thread; its step callback pushes events
    onto a queue that this generator drains, so the UI sees gates resolve live."""
    _authorize_stream(request, api_key)
    assert _pipeline is not None
    request_id = str(uuid.uuid4())

    def event_stream():
        events: "queue.Queue[tuple[str, object]]" = queue.Queue()

        def on_step(ev: StepEvent) -> None:
            events.put(("step", ev))

        def run() -> None:
            try:
                ans = _pipeline.answer(question, request_id=request_id, on_step=on_step)
                events.put(("result", ans))
            except Exception as exc:  # surface failures to the UI instead of hanging
                events.put(("error", str(exc)))
            finally:
                events.put(("__end__", None))

        threading.Thread(target=run, daemon=True).start()

        # First, tell the UI the full ordered stage list so it can draw the skeleton.
        yield _sse({"type": "stages",
                    "stages": [{"step": s, "label": l} for s, l in PIPELINE_STAGES]})

        while True:
            kind, payload = events.get()
            if kind == "__end__":
                break
            if kind == "step":
                ev: StepEvent = payload  # type: ignore[assignment]
                yield _sse({"type": "step", **ev.to_dict()})
            elif kind == "result":
                ans: Answer = payload    # type: ignore[assignment]
                out = ans.to_dict()
                out["request_id"] = request_id
                yield _sse({"type": "result", "answer": out})
            elif kind == "error":
                yield _sse({"type": "error", "message": str(payload)})
        yield _sse({"type": "done"})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    ping = getattr(_db, "ping", None)
    if _db is None or (callable(ping) and not ping()):
        raise HTTPException(status_code=503, detail="database not reachable")
    return {"status": "ready"}


@app.get("/metrics")
def metrics_endpoint() -> Response:
    return Response(content=metrics.render(), media_type="text/plain")
