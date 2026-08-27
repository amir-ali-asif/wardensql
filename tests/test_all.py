"""Tests for the deterministic layers and the full pipeline (with fakes).

No database or API key required: the pipeline test injects a FakeProvider and a
FakeDatabase, exercising the real orchestration end-to-end.
"""

import pytest

from app.guardrails import check_sql
from app.schema import validate_references
from app.confidence import canonicalize, consistency, score
from app.policy import Policy
from app.explain import parse_explain_cost, within_budget
from app.cache import TTLCache, make_key
from app.ratelimit import RateLimiter
from app.ports import ExecutionResult, ExplainCost, JudgeVerdict, SchemaMap
from app.pipeline import Pipeline
from app.providers.fake import FakeProvider

SCHEMA: SchemaMap = {
    "customers": {"id": "INT", "name": "TEXT", "country": "TEXT", "ssn": "TEXT"},
    "orders": {"id": "INT", "customer_id": "INT", "status": "TEXT", "total": "NUMERIC"},
}

# ---------------- guardrails ----------------
ALLOWED = [
    "SELECT name FROM customers",
    "WITH t AS (SELECT id FROM customers) SELECT * FROM t",
    "SELECT country, COUNT(*) FROM customers GROUP BY country",
    "SELECT 1 UNION SELECT 2",
]
BLOCKED = [
    "DELETE FROM customers", "DROP TABLE customers", "UPDATE customers SET name='x'",
    "INSERT INTO customers(name) VALUES('x')", "TRUNCATE customers",
    "ALTER TABLE customers ADD COLUMN x INT", "GRANT SELECT ON customers TO bob",
    "SELECT * FROM customers; DROP TABLE customers", "SELECT * INTO backup FROM customers",
    "SELECT pg_sleep(10)", "COPY customers TO '/tmp/x'", "not sql",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed(sql):
    assert check_sql(sql).ok, sql


@pytest.mark.parametrize("sql", BLOCKED)
def test_blocked(sql):
    assert not check_sql(sql).ok, sql


def test_limit_injected():
    assert "LIMIT" in check_sql("SELECT * FROM orders").safe_sql.upper()


# ---------------- schema validation ----------------
def test_schema_ok():
    assert validate_references("SELECT name FROM customers", SCHEMA).ok


def test_hallucinated_column():
    assert not validate_references("SELECT email FROM customers", SCHEMA).ok


def test_hallucinated_table():
    assert not validate_references("SELECT name FROM clients", SCHEMA).ok


# ---------------- governance policy ----------------
def test_denied_table():
    p = Policy(denied_tables=["orders"])
    assert not p.check("SELECT * FROM orders", SCHEMA).ok


def test_denied_column():
    p = Policy(denied_columns=["customers.ssn"])
    assert not p.check("SELECT ssn FROM customers", SCHEMA).ok
    assert p.check("SELECT name FROM customers", SCHEMA).ok


def test_allow_list():
    p = Policy(allowed_tables=["customers"])
    assert p.check("SELECT name FROM customers", SCHEMA).ok
    assert not p.check("SELECT * FROM orders", SCHEMA).ok


def test_cte_not_treated_as_base_table():
    p = Policy(allowed_tables=["customers"])
    assert p.check("WITH t AS (SELECT id FROM customers) SELECT * FROM t", SCHEMA).ok


# ---------------- explain cost guard ----------------
def test_parse_and_budget():
    plan = [{"Plan": {"Total Cost": 123.4, "Plan Rows": 10}}]
    cost = parse_explain_cost(plan)
    assert cost.ok and cost.total_cost == 123.4
    assert within_budget(cost, max_cost=1000, max_rows=1000)[0]
    assert not within_budget(cost, max_cost=100, max_rows=1000)[0]


# ---------------- confidence ----------------
def test_canonicalize():
    assert canonicalize("select NAME from customers") == canonicalize("SELECT name FROM customers")


def test_consistency_fraction():
    cands = ["SELECT count(*) FROM orders"] * 4 + ["SELECT sum(total) FROM orders"]
    assert consistency(cands)[1] == pytest.approx(0.8)


def test_hard_gate_zeroes():
    c = score(guardrail_ok=True, policy_ok=False, schema_ok=True,
              execution_ok=True, consistency_fraction=1.0, judge_score=1.0)
    assert c.blocked and c.score == 0.0


# ---------------- cache + rate limit ----------------
def test_ttl_cache():
    c = TTLCache(60)
    c.set(make_key("a"), {"v": 1})
    assert c.get(make_key("a")) == {"v": 1}
    assert c.get(make_key("missing")) is None


def test_rate_limiter_blocks():
    rl = RateLimiter(per_minute=2)
    assert rl.allow("k") and rl.allow("k") and not rl.allow("k")


# ---------------- full pipeline with fakes ----------------
class FakeDB:
    def load_schema(self) -> SchemaMap:
        return SCHEMA

    def explain_cost(self, sql: str) -> ExplainCost:
        return ExplainCost(ok=True, total_cost=10.0, plan_rows=5.0)

    def execute(self, sql: str) -> ExecutionResult:
        return ExecutionResult(ok=True, columns=["country", "n"],
                               rows=[["US", 2], ["UK", 1]], row_count=2)


def _settings(**over):
    from app.config import Settings
    base = dict(cache_enabled=False, num_samples=3, judge_mode="conditional",
                max_plan_cost=1e9, max_plan_rows=1e9, groq_api_key="x")
    base.update(over)
    return Settings(**base)


def test_pipeline_happy_path():
    provider = FakeProvider(candidates=["SELECT country, COUNT(*) FROM customers GROUP BY country"])
    pipe = Pipeline(provider, FakeDB(), settings=_settings())
    ans = pipe.answer("orders per country")
    assert not ans.blocked
    assert ans.confidence > 0
    assert ans.row_count == 2


def test_pipeline_blocks_destructive():
    provider = FakeProvider(candidates=["DROP TABLE customers"])
    pipe = Pipeline(provider, FakeDB(), settings=_settings())
    ans = pipe.answer("delete everything")
    assert ans.blocked and "guardrail" in ans.block_reason


def test_pipeline_blocks_governed_column():
    provider = FakeProvider(candidates=["SELECT ssn FROM customers"])
    pipe = Pipeline(provider, FakeDB(), settings=_settings(denied_columns=["customers.ssn"]))
    ans = pipe.answer("show me everyone's ssn")
    assert ans.blocked and "policy" in ans.block_reason


def test_pipeline_blocks_hallucinated_column():
    provider = FakeProvider(candidates=["SELECT email FROM customers"])
    pipe = Pipeline(provider, FakeDB(), settings=_settings())
    ans = pipe.answer("list customer emails")
    assert ans.blocked and "hallucination" in ans.block_reason


def test_pipeline_cache_hit_second_call():
    provider = FakeProvider(candidates=["SELECT country, COUNT(*) FROM customers GROUP BY country"])
    pipe = Pipeline(provider, FakeDB(), settings=_settings(cache_enabled=True))
    q = "orders per country"
    first = pipe.answer(q)
    second = pipe.answer(q)
    assert not first.cached and second.cached


# ==================================================================
# ABAC alias resolution + canonical column governance
# (deterministic; no LLM, no DB)
# ==================================================================

# Richer schema: `employees` also has an `ssn` column, so table-specific ABAC
# (customers.ssn denied but employees.ssn allowed) is meaningfully testable.
SCHEMA2: SchemaMap = {
    "customers":   {"id": "INT", "name": "TEXT", "country": "TEXT", "ssn": "TEXT", "phone": "TEXT"},
    "employees":   {"id": "INT", "name": "TEXT", "department": "TEXT", "ssn": "TEXT", "salary": "NUMERIC"},
    "products":    {"id": "INT", "name": "TEXT", "category": "TEXT", "price": "NUMERIC"},
    "orders":      {"id": "INT", "customer_id": "INT", "status": "TEXT", "total": "NUMERIC"},
    "order_items": {"id": "INT", "order_id": "INT", "product_id": "INT", "quantity": "INT"},
}
ALIASES = {"customers": "c", "employees": "e", "products": "p",
           "orders": "o", "order_items": "oi"}


def _pol(denied=("customers.ssn",), aliases=ALIASES, **kw):
    return Policy(denied_columns=list(denied), table_aliases=aliases, **kw)


# ---- canonical resolution: alias -> table.column ----
def test_abac_denied_column_direct_canonical():
    r = _pol().check("SELECT customers.ssn FROM customers", SCHEMA2)
    assert not r.ok and r.reason == "column 'customers.ssn' is not permitted"
    assert r.canonical_ref == "customers.ssn"


def test_abac_denied_through_predefined_alias():
    r = _pol().check("SELECT c.name, c.ssn FROM customers AS c", SCHEMA2)
    assert not r.ok and r.canonical_ref == "customers.ssn"
    assert r.original_ref == "c.ssn"          # audit shows what was written


def test_abac_denied_through_implicit_alias_no_as():
    assert not _pol().check("SELECT c.ssn FROM customers c", SCHEMA2).ok


def test_abac_fully_qualified_denied():
    assert not _pol().check("SELECT customers.ssn FROM customers", SCHEMA2).ok


# ---- table-specific semantics: same column name, different table ----
def test_abac_same_column_other_table_allowed():
    assert _pol().check("SELECT e.ssn FROM employees AS e", SCHEMA2).ok


def test_abac_same_column_other_table_fully_qualified_allowed():
    assert _pol().check("SELECT employees.ssn FROM employees", SCHEMA2).ok


# ---- arbitrary aliases: identity is the CANONICAL column, not the alias ----
# The security layer must NOT require a fixed alias. Any valid alias resolves to
# the same canonical table.column, and the deny decision is made on that identity.
def test_abac_arbitrary_alias_resolves_to_canonical_denied():
    # An unusual alias 'cust' must still resolve customers.ssn and be blocked --
    # because the CANONICAL column is denied, not because the alias is "wrong".
    r = _pol(aliases={"customers": "c"}).check("SELECT cust.ssn FROM customers AS cust", SCHEMA2)
    assert not r.ok
    assert r.reason == "column 'customers.ssn' is not permitted"
    assert r.canonical_ref == "customers.ssn" and r.original_ref == "cust.ssn"


def test_abac_arbitrary_alias_nondenied_column_allowed():
    # A non-denied column through any alias is permitted; aliases are free syntax.
    r = _pol(aliases={"customers": "c"}).check("SELECT x.name FROM customers AS x", SCHEMA2)
    assert r.ok


def test_abac_denied_resolves_through_any_alias():
    p = _pol(denied=("order_items.quantity",))
    # Whatever alias is used, order_items.quantity resolves and is denied.
    assert not p.check("SELECT oi.quantity FROM order_items AS oi", SCHEMA2).ok
    r = p.check("SELECT z.quantity FROM order_items AS z", SCHEMA2)
    assert not r.ok and r.reason == "column 'order_items.quantity' is not permitted"
    assert r.canonical_ref == "order_items.quantity"


# ---- multiple denied columns ----
def test_abac_multiple_denied_columns():
    p = _pol(denied=("customers.ssn", "customers.phone"))
    assert not p.check("SELECT c.phone FROM customers AS c", SCHEMA2).ok
    assert not p.check("SELECT c.ssn FROM customers AS c", SCHEMA2).ok
    assert p.check("SELECT c.name FROM customers AS c", SCHEMA2).ok


# ---- JOIN with several predefined aliases (clean query) ----
def test_abac_join_multiple_aliases_allowed():
    q = ("SELECT c.name, COUNT(o.id) AS order_count "
         "FROM customers AS c JOIN orders AS o ON o.customer_id = c.id "
         "GROUP BY c.id, c.name ORDER BY order_count DESC LIMIT 1")
    assert _pol().check(q, SCHEMA2).ok


def test_abac_join_denied_column_via_alias_blocked():
    q = "SELECT c.ssn, o.status FROM customers AS c JOIN orders AS o ON o.customer_id = c.id"
    assert not _pol().check(q, SCHEMA2).ok


# ---- unqualified columns ----
def test_abac_unqualified_single_table_denied():
    r = _pol().check("SELECT ssn FROM customers", SCHEMA2)
    assert not r.ok and r.canonical_ref == "customers.ssn"


def test_abac_unqualified_single_table_nondenied_allowed():
    assert _pol().check("SELECT ssn FROM employees", SCHEMA2).ok


def test_abac_unqualified_ambiguous_fails_closed():
    q = "SELECT ssn FROM customers JOIN employees ON employees.id = customers.id"
    r = _pol().check(q, SCHEMA2)
    assert not r.ok and r.reason == "ambiguous column 'ssn'; unable to determine canonical table"


def test_abac_unqualified_ambiguous_but_nondenied_allowed():
    # `id` is ambiguous across the join but not denied -> not a security concern.
    q = "SELECT id FROM customers JOIN orders ON orders.customer_id = customers.id"
    assert _pol().check(q, SCHEMA2).ok


# ---- substring / literal safety (AST, not string matching) ----
def test_abac_substring_column_not_matched():
    p = _pol(denied=("orders.id",))
    assert p.check("SELECT o.customer_id FROM orders AS o", SCHEMA2).ok   # customer_id != id


def test_abac_sensitive_text_in_literal_not_flagged():
    assert _pol().check("SELECT c.name FROM customers AS c WHERE c.name = 'SSN expert'", SCHEMA2).ok


# ---- case-insensitivity (Postgres folds unquoted identifiers) ----
def test_abac_case_insensitive_match():
    assert not _pol().check("SELECT C.SSN FROM CUSTOMERS AS C", SCHEMA2).ok


# ---- SELECT * cannot hide a denied column (star expansion) ----
def test_abac_star_over_denied_table_blocked():
    assert not _pol().check("SELECT * FROM customers", SCHEMA2).ok


def test_abac_star_over_clean_table_allowed():
    assert _pol().check("SELECT * FROM orders", SCHEMA2).ok


def test_abac_star_in_subquery_cannot_hide_denied():
    assert not _pol().check("SELECT c.ssn FROM (SELECT * FROM customers) c", SCHEMA2).ok


# ---- CTEs ----
def test_abac_cte_hiding_denied_column_blocked():
    assert not _pol().check("WITH t AS (SELECT ssn FROM customers) SELECT t.ssn FROM t", SCHEMA2).ok


def test_abac_cte_clean_allowed():
    assert _pol().check("WITH t AS (SELECT name FROM customers) SELECT * FROM t", SCHEMA2).ok


def test_abac_cte_name_not_treated_as_table_alias():
    # A CTE named like a table must not be treated as a base table.
    q = "WITH orders AS (SELECT 1 AS x) SELECT x FROM orders"
    assert _pol(denied=("orders.total",)).check(q, SCHEMA2).ok


# ---- backward compatibility: canonical policy still works unchanged ----
def test_abac_backward_compatible_canonical_policy():
    # No table_aliases configured at all -> alias enforcement off, canonical deny works.
    p = Policy(denied_columns=["customers.ssn"])
    assert not p.check("SELECT ssn FROM customers", SCHEMA2).ok
    assert not p.check("SELECT customers.ssn FROM customers", SCHEMA2).ok
    assert p.check("SELECT name FROM customers", SCHEMA2).ok


# ==================================================================
# End-to-end pipeline: ABAC enforced BEFORE execution; judge can't override
# ==================================================================
class FakeDB2:
    """Fake DB over SCHEMA2. execute() records that it ran, so tests can assert a
    denied query never reaches execution."""
    def __init__(self):
        self.executed = []

    def load_schema(self) -> SchemaMap:
        return SCHEMA2

    def explain_cost(self, sql: str) -> ExplainCost:
        return ExplainCost(ok=True, total_cost=10.0, plan_rows=5.0)

    def execute(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        return ExecutionResult(ok=True, columns=["x"], rows=[[1]], row_count=1)


def _settings2(**over):
    from app.config import Settings
    base = dict(cache_enabled=False, num_samples=3, judge_mode="conditional",
                max_plan_cost=1e9, max_plan_rows=1e9, groq_api_key="x",
                denied_columns=["customers.ssn"], table_aliases=ALIASES)
    base.update(over)
    return Settings(**base)


def test_pipeline_blocks_denied_column_via_alias():
    # The core fix: an aliased denied column is blocked end-to-end.
    provider = FakeProvider(candidates=["SELECT c.name, c.ssn FROM customers AS c"])
    db = FakeDB2()
    # High judge score must NOT rescue a policy failure.
    provider._verdict = JudgeVerdict(True, 1.0, "great")
    pipe = Pipeline(provider, db, settings=_settings2())
    ans = pipe.answer("show me everyone's ssn")
    assert ans.blocked and "policy" in ans.block_reason
    assert ans.block_reason == "policy: column 'customers.ssn' is not permitted"
    assert ans.confidence == 0.0                     # judge cannot override policy_ok=false
    assert db.executed == []                          # never reached the database
    assert ans.signals.get("policy_canonical_ref") == "customers.ssn"


def test_pipeline_allows_same_column_other_table():
    provider = FakeProvider(candidates=["SELECT e.ssn FROM employees AS e"])
    db = FakeDB2()
    pipe = Pipeline(provider, db, settings=_settings2())
    ans = pipe.answer("employee ssns")
    assert not ans.blocked                            # employees.ssn is not denied
    assert db.executed                                # it did run


def test_pipeline_blocks_arbitrary_alias_on_denied_canonical():
    # An arbitrary alias is allowed as syntax, but it resolves to customers.ssn,
    # which is denied -- so the query is blocked on the CANONICAL identity, and
    # never reaches the database.
    provider = FakeProvider(candidates=["SELECT cust.ssn FROM customers AS cust"])
    db = FakeDB2()
    pipe = Pipeline(provider, db, settings=_settings2())
    ans = pipe.answer("ssn via odd alias")
    assert ans.blocked
    assert ans.block_reason == "policy: column 'customers.ssn' is not permitted"
    assert ans.signals.get("policy_canonical_ref") == "customers.ssn"
    assert db.executed == []


def test_pipeline_allows_clean_join_aggregation():
    q = ("SELECT c.name, COUNT(o.id) AS order_count FROM customers AS c "
         "JOIN orders AS o ON o.customer_id = c.id GROUP BY c.id, c.name "
         "ORDER BY order_count DESC LIMIT 1")
    provider = FakeProvider(candidates=[q])
    db = FakeDB2()
    pipe = Pipeline(provider, db, settings=_settings2())
    ans = pipe.answer("who ordered most")
    assert not ans.blocked and ans.confidence > 0 and db.executed


# ==================================================================
# Result-size cap: hard, provider-agnostic egress ceiling
# ==================================================================
class FakeDBRows:
    """A DB whose execute() returns exactly `n` rows, to test the pipeline cap
    independently of any Database implementation's own limiting."""
    def __init__(self, n: int):
        self.n = n

    def load_schema(self) -> SchemaMap:
        return SCHEMA2

    def explain_cost(self, sql: str) -> ExplainCost:
        return ExplainCost(ok=True, total_cost=1.0, plan_rows=1.0)

    def execute(self, sql: str) -> ExecutionResult:
        rows = [[i] for i in range(self.n)]
        return ExecutionResult(ok=True, columns=["n"], rows=rows,
                               row_count=len(rows), truncated=False)


def test_result_cap_truncates_oversized_result():
    provider = FakeProvider(candidates=["SELECT id FROM employees"])
    pipe = Pipeline(provider, FakeDBRows(50), settings=_settings2(max_result_rows=10))
    ans = pipe.answer("many rows")
    assert not ans.blocked
    assert ans.row_count == 10 and len(ans.rows) == 10 and ans.truncated is True


def test_result_cap_leaves_small_result_untouched():
    provider = FakeProvider(candidates=["SELECT id FROM employees"])
    pipe = Pipeline(provider, FakeDBRows(3), settings=_settings2(max_result_rows=10))
    ans = pipe.answer("few rows")
    assert ans.row_count == 3 and len(ans.rows) == 3 and ans.truncated is False


# ==================================================================
# Pipeline step instrumentation (drives the live UI trace)
# ==================================================================
def _collect_steps(provider, db, **over):
    events = []
    pipe = Pipeline(provider, db, settings=_settings2(**over))
    ans = pipe.answer("q", on_step=lambda ev: events.append(ev))
    return ans, events


def test_step_events_happy_path_reaches_score():
    provider = FakeProvider(candidates=["SELECT e.ssn FROM employees AS e"])
    ans, events = _collect_steps(provider, FakeDB2())
    steps = [(e.step, e.status) for e in events]
    ids = [s for s, _ in steps]
    # gates resolve in pipeline order and none is a block
    assert ids == ["cache", "generate", "consistency", "guardrails",
                   "policy", "schema", "explain", "execute", "judge", "score"]
    assert all(st != "blocked" for _, st in steps)
    assert steps[-1] == ("score", "ok")
    assert not ans.blocked


def test_step_events_block_on_policy_before_execute():
    provider = FakeProvider(candidates=["SELECT c.ssn FROM customers AS c"])
    ans, events = _collect_steps(provider, FakeDB2())
    by_step = {e.step: e for e in events}
    assert ans.blocked
    assert by_step["policy"].status == "blocked"
    # blocked at the policy gate -> execution/score gates never emit
    assert "execute" not in by_step and "score" not in by_step
    # the resolved provenance is surfaced for the UI/auditor
    assert by_step["policy"].data.get("resolved") == "c.ssn → customers.ssn"


def test_step_events_optional_no_callback():
    # answer() must work unchanged when no on_step is supplied.
    provider = FakeProvider(candidates=["SELECT e.ssn FROM employees AS e"])
    pipe = Pipeline(provider, FakeDB2(), settings=_settings2())
    ans = pipe.answer("q")           # no on_step
    assert not ans.blocked
