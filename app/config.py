"""Typed, validated configuration.

Uses pydantic-settings so bad config fails fast at startup with a clear message
rather than surfacing as a confusing runtime error later.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ---- LLM provider ----
    llm_provider: str = "groq"                       # "groq" | "openai_compatible" | "fake"
    groq_api_key: str = ""
    # gpt-oss-20b: fastest + cheapest on Groq after the Llama 3.x deprecation.
    model: str = Field(default="openai/gpt-oss-20b", alias="GROQ_MODEL")
    # Base URL lets you point at any OpenAI-compatible free endpoint (Gemini, Cerebras...).
    base_url: str = "https://api.groq.com/openai/v1"
    reasoning_effort: str = "low"                    # low | medium | high (gpt-oss)
    max_retries: int = 3                             # provider-level retry on 429/5xx

    # ---- database (READ-ONLY role -- see sql/setup.sql) ----
    database_url: str = "postgresql://text2sql_ro:readonly@localhost:5432/shop"
    pool_min_size: int = 1
    pool_max_size: int = 10
    # Which database backend to use at startup:
    #   "postgres"   -> the original psycopg PostgresDatabase (default, unchanged)
    #   "sqlalchemy" -> the Stage-1 multi-engine backend (Postgres/MySQL/SQLite/...)
    # The web UI's "Connect a database" panel always uses the sqlalchemy backend.
    db_backend: str = "postgres"
    # SQL dialect for the AST layers (guardrails/resolver/schema/policy). sqlglot
    # needs this to parse the right flavor of SQL. Kept in lock-step with whatever
    # database is connected (postgres, mysql, sqlite, tsql, ...).
    sql_dialect: str = "postgres"

    # ---- guardrails / safety ----
    max_rows: int = 1000                             # in-SQL LIMIT injected by guardrails
    # Hard ceiling on rows the pipeline will actually surface to a caller/UI, enforced
    # independently of the Database implementation and of the query's own LIMIT. This
    # bounds data egress + payload size even if a Database port returns more than asked.
    max_result_rows: int = 200
    statement_timeout_ms: int = 5000
    max_question_chars: int = 2000
    max_plan_cost: float = 1_000_000.0               # reject queries whose plan exceeds this
    max_plan_rows: float = 5_000_000.0

    # ---- data governance (empty = allow all / deny none) ----
    allowed_tables: list[str] = Field(default_factory=list)
    denied_tables: list[str] = Field(default_factory=list)
    denied_columns: list[str] = Field(default_factory=list)   # "table.column"
    # Optional alias *suggestions* (canonical table -> preferred alias), a house
    # style passed to the generation prompt only. NOT a security mechanism: the
    # resolver/policy resolve any valid alias to its canonical table.column and
    # enforce policy on that identity, so the model may use arbitrary aliases.
    # Empty = no suggestion.
    table_aliases: dict[str, str] = Field(
        default_factory=lambda: {
            "customers": "c",
            "employees": "e",
            "products": "p",
            "orders": "o",
            "order_items": "oi",
        }
    )

    # ---- hallucination detection / confidence ----
    num_samples: int = 5
    temperature: float = 0.4
    judge_mode: str = "conditional"                  # "always" | "conditional" | "off"
    conditional_judge_threshold: float = 1.0         # run judge only if consistency < this
    min_confidence_to_return_rows: float = 0.0       # withhold rows below this score

    # ---- caching ----
    cache_enabled: bool = True
    cache_ttl_seconds: int = 3600
    schema_cache_ttl_seconds: int = 300

    # ---- api ----
    api_keys: list[str] = Field(default_factory=list)   # empty = auth disabled
    rate_limit_per_minute: int = 60


settings = Settings()
