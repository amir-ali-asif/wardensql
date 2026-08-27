# Warden — production-grade architecture

A natural-language interface that turns plain-English questions into SQL, runs them
safely against PostgreSQL, enforces data-governance rules, detects hallucinations,
and returns results with a confidence score — wrapped in the operational layers a
real service needs: connection pooling, caching, observability, auth, rate limiting,
a cost guard, containerised deployment, and CI.

> **Honest scope.** This is a production-*grade codebase*, not a running production
> *service*. The architecture, safety, and deployment scaffolding are here and
> tested. Turning it into a live big-scale service is an operations task: a cloud
> host, managed Postgres, a paid or higher-tier LLM (the free tier caps throughput),
> load testing, and monitoring. See "Path to real production" at the bottom.

---

## What makes it more than a portfolio project

| Layer | What it does | Where |
|---|---|---|
| **Provider abstraction** | Swap Groq / any OpenAI-compatible endpoint; parallel sampling; token + rate-limit-header accounting; retry/backoff | `app/providers/` |
| **Guardrails** | AST analysis: only single read-only SELECT/CTE; blocks writes, DDL, stacked queries, DoS functions | `app/guardrails.py` |
| **Data governance (ABAC)** | Scope-aware, fail-closed column policy on **canonical** identifiers. A deterministic resolver turns every reference (any alias, join, self-join, CTE, derived table, correlated subquery, set-op, function/CASE/window) into `table.column` — or a precise failure — and the policy engine returns ALLOWED / DENIED / UNRESOLVED / AMBIGUOUS | `app/resolver.py`, `app/policy.py` |
| **Hallucination detection** | Schema-level (deterministic) + semantic (LLM judge) | `app/schema.py`, `app/providers/` |
| **Cost guard** | `EXPLAIN`-based rejection of queries that would scan too much | `app/explain.py` |
| **Result-size cap** | Hard ceiling on rows surfaced to any caller/UI, enforced in the pipeline independent of the SQL `LIMIT` or DB driver — bounds data egress | `app/pipeline.py` |
| **Confidence** | Self-consistency across samples + judge, gated by hard checks | `app/confidence.py` |
| **Caching** | Repeat questions skip the LLM entirely; schema cached with TTL | `app/cache.py`, `app/db.py` |
| **Observability** | Structured JSON logs, per-request audit trail, Prometheus metrics | `app/observability.py` |
| **Web UI** | Single-page browser interface that streams every pipeline gate live (SSE) and shows SQL, confidence, and rows | `app/api.py`, `app/static/` |
| **Hardened API** | Versioned, pooled, health/readiness probes, API-key auth, rate limiting | `app/api.py` |
| **Deployment** | Dockerfile + docker-compose (app + Postgres), CI workflow | `Dockerfile`, `docker-compose.yml` |
| **Testability** | Dependency injection → full pipeline runs on fakes, no DB/key needed | `tests/` |
| **Evaluation** | Offline harness: execution accuracy + confidence calibration (ECE, Brier, reliability diagram) over a labeled dataset | `eval/` |
| **Connect any database** | Point the tool at Postgres / MySQL / SQLite / SQL Server via `.env`; schema auto-discovered; all safety gates apply on every engine | `app/sqlalchemy_db.py`, `app/config.py` |

## The pipeline

```
question
  │
  ├─ 0. cache lookup .............. hit → return instantly, 0 LLM calls
  ├─ 1. generate k candidates ..... parallel sampling
  ├─ 2. self-consistency .......... chosen query + agreement fraction
  ├─ 3. guardrails ................ HARD GATE (static AST safety)
  ├─ 4. governance policy ......... HARD GATE (allow/deny tables & columns)
  ├─ 5. schema validation ......... HARD GATE (real identifiers only)
  ├─ 6. EXPLAIN cost guard ........ reject over-budget plans
  ├─ 7. execute ................... read-only txn + statement timeout
  ├─ 8. judge (conditional) ....... semantic critic, only when consensus is weak
  └─ 9. score + audit + cache ..... confidence, audit event, cache result
```

The three core ideas from the original design still hold: **the read-only DB role is
the real guardrail** (the AST check is convenience), **hallucination is two problems**
(deterministic schema check + LLM judge), and **confidence comes from self-consistency**,
not the model's self-report.

### Scope-aware, fail-closed column governance (ABAC)

Policies are always written with **canonical** identifiers (`table.column`), never
aliases:

```
DENIED_COLUMNS=["customers.ssn","customers.phone","employees.salary"]
```

Aliases are SQL syntax, not the identity of the data, so **the model may use any
valid alias**. There is no fixed-alias requirement. A deterministic pipeline (pure
AST analysis, no LLM) resolves every reference to its canonical column *before* the
policy check:

```
SQL text
  → parse the COMPLETE input   → exactly one statement, else MULTIPLE_STATEMENTS
  → read-only gate             → reject writes/DDL structurally
  → qualify + build scopes     → expand `*`, bind columns, one scope per (sub)query
  → resolve per scope          → alias → table, column → table.column
  → lineage                    → derived tables / CTEs → base columns
  → ResolvedRef list           → RESOLVED | UNRESOLVED | AMBIGUOUS
  → policy engine              → ALLOWED | DENIED | UNRESOLVED | AMBIGUOUS
                                 | MULTIPLE_STATEMENTS
```

The analysis lives in **`app/resolver.py`** (SQL → canonical references) and the
decision in **`app/policy.py`** (references + deny/allow lists → verdict).

**Decision states** — only `RESOLVED + allowed` proceeds:

| State | Meaning | Example |
|---|---|---|
| **RESOLVED + allowed** | Proven to map to permitted columns only | `SELECT e.ssn FROM employees e` (only `customers.ssn` denied) |
| **RESOLVED + denied** | Proven to map to a denied column — via *any* alias, function, CASE, WHERE/ORDER BY/GROUP BY/HAVING/JOIN-ON, derived table, CTE, or an expanded `*` | `SELECT cust.ssn FROM customers cust` → `customers.ssn` |
| **AMBIGUOUS** | Unqualified name matches >1 in-scope table and one is denied — never guessed | `SELECT ssn FROM customers JOIN employees …` |
| **UNRESOLVED** | Source can't be proven (unknown alias/table, duplicate alias, unsupported/​un-parseable structure, unexpandable `*`) | `SELECT x.ssn FROM customers c` |
| **MULTIPLE_STATEMENTS** | Input holds ≠ 1 statement (stacked/injected query) — refused before anything else | `SELECT name FROM customers; DROP TABLE customers` |

Key properties:

- **Any alias resolves to canonical.** `c.ssn`, `cust.ssn`, `c1.ssn` all resolve to
  `customers.ssn`. You never list aliases in the policy.
- **Table-specific.** Denying `customers.ssn` does **not** deny `employees.ssn`.
- **Scope-aware, not one global alias map.** Each subquery/CTE/derived-table/set-op
  branch resolves against its *own* sources; correlated references resolve against the
  correct enclosing scope. The same alias can mean different tables in different scopes.
- **Lineage.** A derived table or CTE can't launder a denied column: `SELECT d.m FROM
  (SELECT ssn AS m FROM customers) d` is blocked on `customers.ssn`.
- **`SELECT *` is expanded, then every column is checked.** `*` and `t.*` are expanded
  against the live schema (inside joins, subqueries and CTEs too): `SELECT * FROM
  customers` blocks on the exposed `customers.ssn`, while `SELECT * FROM orders` is
  allowed because every exposed column is permitted. A `*` that *cannot* be expanded
  (unknown table) fails closed. A wildcard can never hide a denied column.
- **Exactly one statement per request.** The complete input is parsed and its statement
  boundaries counted with the SQL parser — never `sql.split(";")`, so a `;` inside a
  string literal is safe — *before* execution. Zero or more-than-one statement is
  blocked as `MULTIPLE_STATEMENTS`, so a stacked/injected second statement
  (`SELECT …; DROP TABLE …`) can never be validated-first-then-executed.
- **Fail-closed everywhere.** UNKNOWN ≠ ALLOWED, AMBIGUOUS ≠ ALLOWED, UNSUPPORTED ≠
  ALLOWED. Anything the resolver can't prove is blocked.
- **AST, not string matching.** A denied `customers.id` never matches `customer_id`,
  and the text `WHERE name = 'SSN expert'` never trips the `ssn` rule.
- **Read-only.** Writes/DDL/DCL are refused structurally by the resolver *and* the
  guardrails, on top of the read-only Postgres role — three independent layers.

The security decision is deterministic code, enforced **before** the judge and
before execution; a high LLM judge score can never override `policy_ok = false`.
Every decision carries structured provenance (resolved / denied / unresolved /
ambiguous references and a human-readable explanation) into the audit log —
identifiers only, never column values.

---

## Evaluation

An offline evaluation harness (`eval/`) measures the system end to end against a
labeled dataset of questions with known-correct SQL — turning "it seems to work"
into numbers.

**How it works.** Each question is run through the *real* pipeline (all safety
gates active). The result set is compared to the gold query's result set with an
order/format-tolerant oracle. Governance cases are scored on correct *refusal*.
The harness runs fully offline (deterministic `FakeProvider` + in-memory SQLite),
so anyone can reproduce it with no database and no API key:

```bash
python -m eval.run          # accuracy + calibration report
python -m eval.run --plot   # also saves eval/reliability.png
python -m eval.run --live   # optional: use the real Groq model (needs GROQ_API_KEY)
```

**What it reports.**

- **Execution accuracy** — fraction of questions whose result set matches gold
  (governance cases must be blocked).
- **Calibration** — does the confidence score track correctness? Reported as
  **ECE** (Expected Calibration Error) and **Brier score**, plus a **reliability
  diagram** (`eval/reliability.png`).

**Results.**

| Mode | Provider | Execution accuracy | ECE | Brier |
|---|---|---|---|---|
| Offline (pipeline fidelity) | FakeProvider (gold SQL) | 100% (12/12) | 0.000 | 0.000 |
| Live (model quality) | Groq `gpt-oss-20b`, 1 sample | 83.3% (10/12) | 0.200 | 0.200 |

The offline mode validates that the pipeline faithfully passes valid queries and
blocks denied ones (a regression guard, enforced in CI). The live mode measures
the actual model's SQL-writing quality — the gap between the two rows is the
honest cost of using a small free-tier model, made visible instead of assumed.

**What calibration reveals.** On the live run the system reported ~1.0 confidence
across the answered cases, but only 80% were actually correct — a 0.20 gap (ECE =
Brier = 0.200). That is textbook **overconfidence**, surfaced instead of assumed.

> Live numbers use the Groq free tier (8000 tokens/min), so the live runner uses
> 1 sample with light pacing. A paid tier with 5-sample self-consistency would
> give a richer calibration curve.

![Reliability diagram](eval/reliability.png)

---

## Connect any database (Stage 1: "bring your own database")

Out of the box the service can point at **any** SQL database, not just the demo
Postgres — Postgres, MySQL/MariaDB, SQLite, or SQL Server. Attaching a new database
is a **single-file** operation: everything that describes a connection lives in
`.env`, so there's exactly one place to edit and nothing can drift out of sync.

**One file, three things, set together:**

```bash
# 1) which database
DB_BACKEND=sqlalchemy
DATABASE_URL=mysql+pymysql://readonly:secret@10.0.0.5:3306/hospital
# 2) governance for THAT schema (canonical table.column identifiers)
DENIED_COLUMNS=["patients.ssn","patients.mrn"]
DENIED_TABLES=["billing_internal"]
# 3) optional alias hints for the prompt
TABLE_ALIASES={"patients":"p","admissions":"a"}
```

Restart the app and it's live against the new database. Keeping the connection and
its governance side by side in one file is deliberate: the denied columns *are* part
of a connection's identity, so splitting them across two places (e.g. a UI for the
connection + `.env` for governance) invites a silent hole where a new database is
connected but still guarded by the *old* database's rules. One file removes that
whole class of mistake.

**How it works.**

- `app/sqlalchemy_db.py` — a second `Database` implementation (alongside the
  original `PostgresDatabase`) built on **SQLAlchemy**. One interface over
  PostgreSQL, MySQL/MariaDB, SQLite, and SQL Server. On startup it **auto-discovers
  the schema** (reflection), and maps the engine to the correct **sqlglot dialect**
  so the AST guardrails/resolver parse the right flavor of SQL.
- `app/config.py` — `DB_BACKEND` selects the backend and `DATABASE_URL` drives it;
  the SQL dialect is auto-detected from the connection.
- `app/api.py` — at startup, builds the configured database, threads its dialect
  into the pipeline, and (choice made for this project) **warns in the logs if no
  `DENIED_COLUMNS`/`DENIED_TABLES` are set**, so an empty governance policy is
  visible rather than silent. The app still starts — governance is opt-in per
  database, but never forgotten by accident.

**Every safety guarantee still applies on every engine.** Guardrails, scope-aware
ABAC governance, schema-hallucination checks and the row cap all run unchanged —
verified by tests against a live SQLite database (a `SELECT ssn FROM customers`
question is still blocked with full `customers.ssn` provenance). Read-only is
enforced three ways: the AST layers refuse writes before execution, `execute()`
rolls back its transaction (never commits), and you are urged to connect with a
**read-only database user** — least privilege remains the strongest guarantee.

> **Why config, not a UI.** A "connect from the browser" panel looks nicer but only
> does half the job: it moves the connection string out of `.env` while governance,
> aliases and limits stay behind — two places to edit for one decision, and a real
> risk they disagree. Config-as-source-of-truth is the simpler, safer design, and
> "restart on config change" is normal for a service pointed at one database.

---

## Run it

### Option A — Docker (recommended; no local Postgres needed)

```bash
export GROQ_API_KEY=gsk_your_key       # from console.groq.com
docker compose up --build
```

Postgres comes up seeded with the demo data and read-only role, then the API.
**Open the web UI at [http://localhost:8000/](http://localhost:8000/)** and watch each
pipeline gate resolve live as your question runs. Or call the API directly:

```bash
curl -s localhost:8000/v1/ask -H 'content-type: application/json' \
     -d '{"question":"how many completed orders per country?"}'
```

### Option B — local Python

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                   # add your GROQ_API_KEY

createdb shop && psql shop -f sql/setup.sql #for testing purposes

uvicorn app.api:app --reload           # UI + API at :8000, API docs at /docs
# or:
python -m app.cli "which category sold the most units?"
```

### Option C — no Postgres at all (SQLite, fastest way to try it)

Prefer zero database setup? Boot straight onto a SQLite file using the Stage-1
multi-engine backend:

```bash
pip install -r requirements.txt
# build a demo SQLite database from the eval seed:
python -c "import sqlite3,pathlib; sqlite3.connect('shop.sqlite').executescript(pathlib.Path('eval/seed_sqlite.sql').read_text()).connection.commit()"

DB_BACKEND=sqlalchemy DATABASE_URL="sqlite:///$(pwd)/shop.sqlite" \
  GROQ_API_KEY=gsk_your_key uvicorn app.api:app --reload
```

Then open the UI and ask away. To attach a different database, edit `DATABASE_URL`
(and its governance) in `.env` and restart — see "Connect any database" above.

### Web UI

Open [http://localhost:8000/](http://localhost:8000/). Type a question and the page runs
it through the real pipeline, drawing a live trace of every gate — cache, generate,
self-consistency, guardrails, governance (ABAC), schema, cost guard, execute, judge,
score — and marking each one pass / skip / **blocked** as it resolves. A blocked query
visibly breaks the trace at the gate that stopped it and shows the reason (including the
`c.ssn → customers.ssn` alias resolution for a governance block); an answered query shows
the executed SQL, a confidence meter, run metrics, and the result rows. It's a single
static page served by the API (no build step), backed by a Server-Sent-Events stream.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI (live pipeline trace) |
| POST | `/v1/ask` | Ask a question (send `X-API-Key` if `API_KEYS` is set) |
| GET | `/v1/ask/stream` | Same, as a Server-Sent-Events stream of pipeline steps (used by the UI) |
| GET | `/health` | Liveness |
| GET | `/ready` | Readiness (checks DB) |
| GET | `/metrics` | Prometheus metrics |

### Tests

```bash
pytest -q          # 198 tests, no DB or API key required
```

---

## Configuration

All via env / `.env` (see `.env.example`). Highlights for the free tier and scale:

- `NUM_SAMPLES` / `JUDGE_MODE=conditional` — control LLM calls per question.
- `DENIED_COLUMNS='["customers.ssn"]'` — governance in action; try asking for SSNs.
- `MIN_CONFIDENCE_TO_RETURN_ROWS=0.4` — withhold results the system isn't sure about.
- `API_KEYS` + `RATE_LIMIT_PER_MINUTE` — multi-tenant auth and abuse protection.
- `BASE_URL` — point at any OpenAI-compatible free endpoint if you outgrow Groq.
- `DB_BACKEND=sqlalchemy` + `DATABASE_URL=...` — connect Postgres/MySQL/SQLite/SQL
  Server from `.env`. See "Connect any database" above.

## Path to a real product (the operations half)

These need infrastructure/budget, not code, and are the honest gap between this and a
big-scale service:

1. **Deploy** the container to a host (Fly.io / Railway / Cloud Run) behind TLS.
2. **Managed database** with read replicas; keep a read-only user.
3. **Externalise state** — move the cache and rate limiter to Redis so multiple
   instances share them.
4. **Ship logs & metrics** to Grafana/Loki or a hosted APM; alert on block-rate and
   low-confidence spikes.
5. **LLM budget** — the free tier caps you at ~100–150 questions/day; a paid or
   higher tier (or self-hosted model) is required for real traffic.
6. **Load test** (k6/Locust) and tune pool sizes and worker counts.
7. **Result-based consistency** — compare candidate *result sets*, not just SQL text,
   for a stronger confidence signal.
```

**Stage 2 — multi-company SaaS (beyond this codebase).** The Stage-1 config-driven
"attach any database" feature makes this a single-operator tool: one database per
running instance, set in `.env`. Turning it into a service many companies sign up
for adds: user accounts + auth, **encrypted credential storage** (never plaintext),
strict **tenant isolation** so no company can see another's data or schema, a UI or
API to manage connections per tenant, **per-company governance**, and per-connection
eval datasets. That is a product-building effort, deliberately out of scope here —
Stage 1 demonstrates the "any database" capability end to end.
