"""Manual end-to-end security scenarios, run through the REAL pipeline on fakes.

    python -m scripts.abac_demo

No database or API key needed: a FakeProvider supplies the exact SQL a model
might generate, and a FakeDB provides the schema + execution. Every request still
flows through the real guardrails -> policy -> schema -> explain -> execute ->
judge -> score pipeline, so the enforcement shown here is the production path.

It demonstrates all four decision states of the deterministic security layer:
ALLOWED, DENIED, AMBIGUOUS, UNRESOLVED -- with arbitrary aliases, self-joins,
CTEs, correlated subqueries and derived tables -- and proves a high LLM judge
score can never override a policy block.
"""

from __future__ import annotations

from app.config import Settings
from app.pipeline import Pipeline
from app.ports import ExecutionResult, ExplainCost, JudgeVerdict, SchemaMap
from app.providers.fake import FakeProvider

SCHEMA: SchemaMap = {
    "customers":   {"id": "INT", "name": "TEXT", "country": "TEXT", "ssn": "TEXT",
                    "phone": "TEXT", "referrer_id": "INT"},
    "employees":   {"id": "INT", "name": "TEXT", "department": "TEXT", "ssn": "TEXT",
                    "salary": "NUMERIC", "manager_id": "INT"},
    "orders":      {"id": "INT", "customer_id": "INT", "status": "TEXT", "total": "NUMERIC"},
    "order_items": {"id": "INT", "order_id": "INT", "product_id": "INT", "quantity": "INT"},
}
DENIED = ["customers.ssn", "customers.phone", "employees.salary"]


class FakeDB:
    def __init__(self):
        self.executed = []

    def load_schema(self) -> SchemaMap:
        return SCHEMA

    def explain_cost(self, sql: str) -> ExplainCost:
        return ExplainCost(ok=True, total_cost=10.0, plan_rows=5.0)

    def execute(self, sql: str) -> ExecutionResult:
        self.executed.append(sql)
        return ExecutionResult(ok=True, columns=["result"], rows=[["<rows>"]], row_count=1)


def run(label: str, generated_sql: str) -> None:
    settings = Settings(cache_enabled=False, num_samples=3, judge_mode="conditional",
                        max_plan_cost=1e9, max_plan_rows=1e9, groq_api_key="x",
                        denied_columns=DENIED)
    db = FakeDB()
    # The judge always "approves" -- to prove it can never override a policy block.
    provider = FakeProvider(candidates=[generated_sql],
                            verdict=JudgeVerdict(True, 1.0, "looks correct"))
    ans = Pipeline(provider, db, settings=settings).answer(label)

    status = "BLOCKED" if ans.blocked else "ALLOWED"
    print(f"\n=== {label}")
    print(f"    generated : {generated_sql}")
    print(f"    result    : {status}")
    if ans.blocked:
        print(f"    reason    : {ans.block_reason}")
        st = ans.signals.get("policy_status")
        if st:
            print(f"    status    : {st.upper()}")
        if ans.signals.get("policy_original_ref"):
            print(f"    resolved  : {ans.signals.get('policy_original_ref')} "
                  f"-> {ans.signals.get('policy_canonical_ref')}")
        if ans.signals.get("policy_ambiguous_candidates"):
            print(f"    candidates: {ans.signals.get('policy_ambiguous_candidates')}")
        print(f"    executed? : {'yes' if db.executed else 'NO (blocked before DB)'}")
    else:
        print(f"    confidence: {ans.confidence}")
        print(f"    executed? : {'yes' if db.executed else 'no'}")


if __name__ == "__main__":
    print("Policy: denied_columns =", DENIED)
    print("(aliases are free syntax -- resolved to canonical table.column)")

    # ---- ALLOWED ----
    run("normal join + aggregation",
        "SELECT c.name, COUNT(o.id) AS n FROM customers AS c "
        "JOIN orders AS o ON o.customer_id = c.id GROUP BY c.id, c.name "
        "ORDER BY n DESC LIMIT 1")
    run("same column name, different (allowed) table",
        "SELECT e.name, e.ssn FROM employees AS e")
    run("self-join with distinct aliases",
        "SELECT c1.name, c2.name FROM customers AS c1 "
        "JOIN customers AS c2 ON c1.referrer_id = c2.id")
    run("clean CTE + join",
        "WITH co AS (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) "
        "SELECT c.name, co.n FROM customers AS c JOIN co ON c.id = co.customer_id")

    # ---- DENIED (any alias resolves to the canonical denied column) ----
    run("denied ssn via a house alias", "SELECT c.name, c.ssn FROM customers AS c")
    run("denied ssn via an arbitrary alias", "SELECT cust.ssn FROM customers AS cust")
    run("denied column hidden behind a column alias",
        "SELECT ssn AS customer_ssn FROM customers")
    run("denied column hidden in a derived table",
        "SELECT d.masked FROM (SELECT ssn AS masked FROM customers) d")
    run("denied column only in a correlated subquery",
        "SELECT c.name FROM customers c WHERE EXISTS "
        "(SELECT 1 FROM orders o WHERE o.customer_id = c.id AND c.ssn IS NOT NULL)")
    run("denied column only in ORDER BY", "SELECT name FROM customers ORDER BY ssn")

    # ---- AMBIGUOUS (unqualified, matches >1 in-scope table, one is denied) ----
    run("ambiguous unqualified denied column",
        "SELECT ssn FROM customers JOIN employees ON employees.id = customers.id")

    # ---- UNRESOLVED (fail-closed) ----
    run("unknown alias -> cannot resolve", "SELECT x.ssn FROM customers c")
    run("duplicate alias -> ambiguous structure",
        "SELECT c.name FROM customers c JOIN employees c ON TRUE")

    # ---- SELECT * is expanded, then every column is policy-checked ----
    run("wildcard over a table with a denied column", "SELECT * FROM customers")
    run("qualified wildcard over a denied table", "SELECT c.* FROM customers AS c")
    run("wildcard over an all-permitted table", "SELECT * FROM orders")
    run("wildcard scoped to the clean table in a join",
        "SELECT o.* FROM customers c JOIN orders o ON c.id = o.customer_id")

    # ---- exactly one statement per request (stacked/injected -> blocked) ----
    run("stacked read then DROP", "SELECT name FROM customers; DROP TABLE customers")
    run("two SELECTs in one request", "SELECT name FROM customers; SELECT ssn FROM customers")
