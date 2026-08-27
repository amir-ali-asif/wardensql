"""Comprehensive tests for the deterministic, scope-aware, fail-closed SQL
security layer: app/resolver.py (SQL -> canonical refs) and app/policy.py
(canonical refs -> ALLOW/BLOCK). No LLM, no database.

Structured to mirror the required coverage: basic resolution, alias freedom,
every deny surface (WHERE/HAVING/GROUP BY/ORDER BY/JOIN ON/functions/CASE/
window), joins & self-joins, nested/correlated/CTE/derived-table lineage, set
operations, ambiguity, unresolved (fail-closed), read-only enforcement, and the
false-positive guards (literals, substring, id vs customer_id).
"""

import pytest

from app.policy import Decision, Policy
from app.resolver import RefStatus, analyze
from app.ports import SchemaMap

SCHEMA: SchemaMap = {
    "customers":   {"id": "INT", "name": "TEXT", "country": "TEXT", "ssn": "TEXT",
                    "phone": "TEXT", "cnic": "TEXT", "referrer_id": "INT",
                    "first_name": "TEXT", "last_name": "TEXT", "email": "TEXT"},
    "employees":   {"id": "INT", "name": "TEXT", "department": "TEXT", "ssn": "TEXT",
                    "salary": "NUMERIC", "manager_id": "INT"},
    "products":    {"id": "INT", "name": "TEXT", "category": "TEXT", "price": "NUMERIC"},
    "orders":      {"id": "INT", "customer_id": "INT", "status": "TEXT",
                    "total": "NUMERIC", "amount": "NUMERIC"},
    "order_items": {"id": "INT", "order_id": "INT", "product_id": "INT", "quantity": "INT"},
}

DENIED = ["customers.ssn", "customers.phone", "customers.cnic", "employees.salary"]


def pol(denied=DENIED, **kw) -> Policy:
    return Policy(denied_columns=list(denied), **kw)


def allowed(sql, denied=DENIED, schema=SCHEMA, **kw):
    r = pol(denied, **kw).check(sql, schema)
    assert r.ok, f"expected ALLOW, got BLOCK: {r.reason}\n{r.explanation}"
    return r


def blocked(sql, denied=DENIED, schema=SCHEMA, status=None, **kw):
    r = pol(denied, **kw).check(sql, schema)
    assert not r.ok, f"expected BLOCK, got ALLOW for: {sql}"
    if status is not None:
        assert r.status is status, f"expected {status}, got {r.status} ({r.reason})"
    return r


# ============================ BASIC ====================================

def test_simple_select_allowed():
    allowed("SELECT name FROM customers")


def test_simple_select_denied():
    r = blocked("SELECT name, ssn FROM customers", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_qualified_column_no_alias():
    r = blocked("SELECT customers.ssn FROM customers", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_qualified_nondenied_allowed():
    allowed("SELECT customers.name FROM customers")


# ============================ ALIASES ==================================

@pytest.mark.parametrize("alias", ["c", "cust", "customer", "c1", "x", "zzz"])
def test_arbitrary_alias_resolves_to_canonical(alias):
    r = blocked(f"SELECT {alias}.ssn FROM customers AS {alias}", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"
    assert r.original_ref == f"{alias}.ssn"


def test_alias_without_as():
    r = blocked("SELECT c.ssn FROM customers c", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_arbitrary_alias_nondenied_allowed():
    allowed("SELECT weird.name, weird.country FROM customers AS weird")


def test_no_fixed_alias_required_for_multiple_tables():
    allowed("SELECT cust.name, prod.name FROM customers cust CROSS JOIN products prod")


# ============================ JOINS ====================================

def test_join_allowed():
    allowed("SELECT c.name, o.id FROM customers c JOIN orders o ON c.id = o.customer_id")


def test_join_denied_in_select():
    blocked("SELECT c.ssn, o.status FROM customers c JOIN orders o ON o.customer_id=c.id",
            status=Decision.DENIED)


@pytest.mark.parametrize("jt", ["INNER JOIN", "LEFT JOIN", "RIGHT JOIN",
                                 "FULL JOIN", "CROSS JOIN"])
def test_join_types_all_validate_columns(jt):
    on = "" if jt == "CROSS JOIN" else " ON c.id = o.customer_id"
    allowed(f"SELECT c.name FROM customers c {jt} orders o{on}")
    blocked(f"SELECT c.ssn FROM customers c {jt} orders o{on}", status=Decision.DENIED)


def test_self_join_distinct_aliases_ok():
    allowed("SELECT c1.name, c2.name FROM customers c1 "
            "JOIN customers c2 ON c1.referrer_id = c2.id")


def test_self_join_denied_on_one_side():
    r = blocked("SELECT c1.name, c2.ssn FROM customers c1 "
                "JOIN customers c2 ON c1.referrer_id = c2.id", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_self_join_employees_manager():
    allowed("SELECT e.name, m.name FROM employees e JOIN employees m ON e.manager_id=m.id")
    blocked("SELECT e.name, m.salary FROM employees e JOIN employees m ON e.manager_id=m.id",
            status=Decision.DENIED)


# ============ DENY SURFACES BEYOND THE SELECT LIST =====================

def test_denied_in_where():
    blocked("SELECT name FROM customers WHERE ssn IS NOT NULL", status=Decision.DENIED)


def test_denied_in_order_by():
    blocked("SELECT name FROM customers ORDER BY ssn", status=Decision.DENIED)


def test_denied_in_group_by():
    blocked("SELECT phone, COUNT(*) FROM customers GROUP BY phone", status=Decision.DENIED)


def test_denied_in_having():
    blocked("SELECT country FROM customers GROUP BY country HAVING MAX(ssn) IS NOT NULL",
            status=Decision.DENIED)


def test_denied_in_join_condition():
    blocked("SELECT c.name FROM customers c JOIN employees e ON e.ssn = c.ssn",
            status=Decision.DENIED)


def test_denied_inside_function():
    blocked("SELECT COUNT(ssn) FROM customers", status=Decision.DENIED)
    blocked("SELECT MAX(salary) FROM employees", status=Decision.DENIED)


def test_denied_inside_case():
    blocked("SELECT CASE WHEN salary > 100000 THEN 'hi' ELSE 'lo' END FROM employees",
            status=Decision.DENIED)


def test_denied_inside_expression():
    blocked("SELECT 'x' || phone FROM customers", status=Decision.DENIED)


def test_allowed_expression_over_clean_columns():
    allowed("SELECT CONCAT(first_name, ' ', last_name) FROM customers")


def test_column_alias_cannot_hide_denied_source():
    r = blocked("SELECT ssn AS customer_ssn FROM customers", status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_distinct_denied():
    blocked("SELECT DISTINCT ssn FROM customers", status=Decision.DENIED)


def test_window_partition_or_order_denied():
    blocked("SELECT id, ROW_NUMBER() OVER (PARTITION BY country ORDER BY ssn) FROM customers",
            status=Decision.DENIED)


def test_window_clean_allowed():
    allowed("SELECT id, ROW_NUMBER() OVER (PARTITION BY country ORDER BY name) FROM customers")


# ============ TABLE-SPECIFIC SEMANTICS =================================

def test_same_column_other_table_allowed():
    # employees.ssn is not in the deny-list even though customers.ssn is.
    allowed("SELECT e.ssn FROM employees e")
    allowed("SELECT employees.ssn FROM employees")


def test_customers_salary_not_denied_but_employees_salary_is():
    # employees.salary denied; there is no customers.salary, sanity check table scoping
    blocked("SELECT salary FROM employees", status=Decision.DENIED)


# ============ NESTED / CORRELATED / CTE / DERIVED ======================

def test_nested_subquery_inner_denied():
    blocked("SELECT name FROM customers WHERE id IN "
            "(SELECT customer_id FROM orders WHERE amount > (SELECT AVG(salary) FROM employees))",
            status=Decision.DENIED)


def test_nested_subquery_clean_allowed():
    allowed("SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders)")


def test_correlated_subquery_clean():
    allowed("SELECT c.name FROM customers c WHERE EXISTS "
            "(SELECT 1 FROM orders o WHERE o.customer_id = c.id)")


def test_correlated_subquery_outer_denied_in_inner():
    # inner references the outer alias' denied column
    blocked("SELECT c.name FROM customers c WHERE EXISTS "
            "(SELECT 1 FROM orders o WHERE o.customer_id = c.id AND c.ssn IS NOT NULL)",
            status=Decision.DENIED)


def test_subquery_in_expression_scalar():
    allowed("SELECT c.name, (SELECT COUNT(*) FROM orders o WHERE o.customer_id=c.id) AS n "
            "FROM customers c")


def test_cte_clean_allowed():
    allowed("WITH t AS (SELECT name FROM customers) SELECT * FROM t")


def test_cte_hiding_denied_blocked():
    blocked("WITH t AS (SELECT ssn FROM customers) SELECT t.ssn FROM t", status=Decision.DENIED)


def test_cte_join_lineage_allowed():
    allowed("WITH co AS (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) "
            "SELECT c.name, co.n FROM customers c JOIN co ON c.id = co.customer_id")


def test_multiple_ctes_one_denied():
    blocked("WITH a AS (SELECT id, ssn FROM customers), b AS (SELECT id FROM employees) "
            "SELECT a.id FROM a JOIN b ON a.id = b.id", status=Decision.DENIED)


def test_derived_table_rename_cannot_hide_denied():
    r = blocked("SELECT d.masked FROM (SELECT ssn AS masked FROM customers) d",
                status=Decision.DENIED)
    assert r.canonical_ref == "customers.ssn"


def test_derived_table_clean_allowed():
    allowed("SELECT d.customer_name FROM (SELECT name AS customer_name FROM customers) d")


def test_derived_lineage_reported_in_audit():
    # A clean derived rename should carry lineage in the resolver output.
    rep = analyze("SELECT d.cn FROM (SELECT name AS cn FROM customers) d", SCHEMA)
    outer = [r for r in rep.refs if r.sql_reference == "d.cn"][0]
    assert outer.canonical == "customers.name"


# ============ STAR EXPANSION ===========================================
# `SELECT *` must be expanded against the schema and every resulting column
# policy-checked. A denied column exposed by a wildcard is RESOLVED + DENIED
# (never a harmless single "*"); a wildcard over an all-permitted table is
# ALLOWED. Applies to bare `*`, qualified `t.*`, joins, and nested scopes.

def test_star_over_denied_table_blocked():
    # Expands to customers.id, customers.name, customers.ssn, ... -> ssn denied.
    blocked("SELECT * FROM customers", status=Decision.DENIED)


def test_star_over_clean_table_allowed():
    # Every column of orders is permitted -> wildcard allowed after expansion.
    allowed("SELECT * FROM orders")


def test_qualified_star_over_denied_table_blocked():
    blocked("SELECT c.* FROM customers c", status=Decision.DENIED)


def test_qualified_star_over_clean_table_allowed():
    allowed("SELECT o.* FROM orders o")


def test_star_over_join_exposing_denied_blocked():
    blocked("SELECT * FROM customers c JOIN orders o ON c.id = o.customer_id",
            status=Decision.DENIED)


def test_qualified_star_selects_only_clean_table_in_join_allowed():
    # o.* exposes only orders columns even though customers (with denied cols)
    # is joined -- the wildcard is scoped to the orders alias.
    allowed("SELECT o.* FROM customers c JOIN orders o ON c.id = o.customer_id")


def test_star_in_subquery_cannot_hide_denied():
    blocked("SELECT c.ssn FROM (SELECT * FROM customers) c", status=Decision.DENIED)


def test_nested_star_over_star_cannot_hide_denied():
    # Inner `SELECT *` exposes customers.ssn; the outer `SELECT *` must not launder it.
    blocked("SELECT * FROM (SELECT * FROM customers) AS c", status=Decision.DENIED)


def test_qualified_star_cannot_hide_denied():
    blocked("SELECT c.* FROM customers c", status=Decision.DENIED)


def test_star_in_cte_cannot_hide_denied():
    blocked("WITH c AS (SELECT * FROM customers) SELECT * FROM c", status=Decision.DENIED)


def test_star_over_denied_table_in_union_branch_blocked():
    blocked("SELECT id FROM orders UNION SELECT * FROM customers", status=Decision.DENIED)


def test_star_over_unknown_table_fails_closed():
    # Cannot be expanded (table not in schema) -> must NOT be assumed safe.
    blocked("SELECT * FROM ghosts", status=Decision.UNRESOLVED)


# ============ MULTIPLE / STACKED STATEMENTS ============================
# Exactly one statement per request. The complete input is parsed and its
# statement boundaries counted (never naive ";"-splitting) BEFORE execution,
# so a stacked/injected second statement is refused with MULTIPLE_STATEMENTS.

def test_two_selects_blocked_as_multiple():
    blocked("SELECT name FROM customers; SELECT name FROM orders",
            status=Decision.MULTIPLE_STATEMENTS)


def test_select_then_drop_blocked_as_multiple():
    # The first (safe) statement must never be validated-then-executed while the
    # second is ignored: the whole input is rejected.
    blocked("SELECT name FROM customers; DROP TABLE customers",
            status=Decision.MULTIPLE_STATEMENTS)


def test_select_then_delete_blocked_as_multiple():
    blocked("SELECT name FROM customers; DELETE FROM customers",
            status=Decision.MULTIPLE_STATEMENTS)


def test_select_then_update_blocked_as_multiple():
    blocked("SELECT name FROM customers; UPDATE customers SET ssn = 'x'",
            status=Decision.MULTIPLE_STATEMENTS)


def test_select_then_second_select_of_denied_blocked_as_multiple():
    # Even two read-only SELECTs are refused -- one statement per request.
    blocked("SELECT name FROM customers; SELECT ssn FROM customers",
            status=Decision.MULTIPLE_STATEMENTS)


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2; SELECT 3",
    "  SELECT name FROM customers ;  SELECT 1  ",
    "SELECT name FROM customers;\nSELECT ssn FROM customers;",
])
def test_various_stacked_inputs_blocked(sql):
    blocked(sql, status=Decision.MULTIPLE_STATEMENTS)


def test_trailing_semicolon_is_single_statement():
    # A single statement with a trailing semicolon is exactly one statement.
    allowed("SELECT name FROM customers;")


def test_semicolon_inside_string_literal_is_single_statement():
    # The parser must not treat a ';' inside a string literal as a boundary --
    # naive sql.split(";") would wrongly see two statements here.
    allowed("SELECT name FROM customers WHERE country = 'a; b'")


def test_semicolon_in_string_plus_real_split_is_multiple():
    blocked("SELECT name FROM customers WHERE country = 'a; b'; DROP TABLE customers",
            status=Decision.MULTIPLE_STATEMENTS)


def test_multiple_statements_never_execute_in_pipeline():
    # End-to-end: a stacked query is blocked before the DB is ever touched.
    from app.config import Settings
    from app.pipeline import Pipeline
    from app.ports import ExecutionResult, ExplainCost, JudgeVerdict
    from app.providers.fake import FakeProvider

    class RecordingDB:
        def __init__(self): self.executed = []
        def load_schema(self): return SCHEMA
        def explain_cost(self, sql): return ExplainCost(ok=True, total_cost=1.0, plan_rows=1.0)
        def execute(self, sql):
            self.executed.append(sql)
            return ExecutionResult(ok=True, columns=["x"], rows=[[1]], row_count=1)

    settings = Settings(cache_enabled=False, num_samples=1, judge_mode="never",
                        groq_api_key="x", denied_columns=DENIED)
    db = RecordingDB()
    prov = FakeProvider(candidates=["SELECT name FROM customers; DROP TABLE customers"],
                        verdict=JudgeVerdict(True, 1.0, "ok"))
    ans = Pipeline(prov, db, settings=settings).answer("q")
    assert ans.blocked
    assert db.executed == []


# ============ SET OPERATIONS ===========================================

@pytest.mark.parametrize("op", ["UNION", "UNION ALL", "INTERSECT", "EXCEPT"])
def test_set_op_branch_denied_blocks_all(op):
    blocked(f"SELECT name FROM customers {op} SELECT ssn FROM customers", status=Decision.DENIED)


@pytest.mark.parametrize("op", ["UNION", "INTERSECT", "EXCEPT"])
def test_set_op_clean_allowed(op):
    allowed(f"SELECT name FROM customers {op} SELECT name FROM employees")


def test_set_op_denied_in_second_branch_other_table_ok():
    # employees.ssn is allowed even though customers.ssn is denied
    allowed("SELECT name FROM customers UNION SELECT ssn FROM employees")


# ============ AMBIGUITY (fail-closed only when a candidate is denied) ==

def test_ambiguous_denied_blocked():
    r = blocked("SELECT ssn FROM customers JOIN employees ON employees.id = customers.id",
                status=Decision.AMBIGUOUS)
    assert set(r.candidates) == {"customers.ssn", "employees.ssn"}


def test_ambiguous_nondenied_allowed():
    # 'id' is ambiguous across the join but not denied -> not a security concern.
    allowed("SELECT id FROM customers JOIN orders ON orders.customer_id = customers.id")


# ============ UNRESOLVED (fail-closed) =================================

def test_unknown_alias_blocked():
    r = blocked("SELECT x.ssn FROM customers c", status=Decision.UNRESOLVED)
    assert "x" in (r.reason or "")


def test_unknown_table_blocked():
    blocked("SELECT g.name FROM ghosts g", status=Decision.UNRESOLVED)


def test_duplicate_alias_blocked():
    blocked("SELECT c.name FROM customers c JOIN employees c ON TRUE",
            status=Decision.UNRESOLVED)


def test_parse_error_blocked():
    blocked("SELECT FROM WHERE", status=Decision.UNRESOLVED)


# ============ READ-ONLY ENFORCEMENT ====================================

@pytest.mark.parametrize("sql", [
    "INSERT INTO customers(name) VALUES ('x')",
    "UPDATE customers SET name = 'x'",
    "DELETE FROM customers",
    "DROP TABLE customers",
    "ALTER TABLE customers ADD COLUMN x INT",
    "TRUNCATE customers",
    "CREATE TABLE t (id INT)",
    "GRANT SELECT ON customers TO bob",
])
def test_non_read_statements_blocked(sql):
    # Even with NO column policy, the resolver refuses to analyze a write/DDL
    # statement, and the policy blocks it fail-closed.
    r = Policy(denied_columns=[]).check(sql, SCHEMA)
    assert not r.ok and r.status is Decision.UNRESOLVED


# ============ TABLE ALLOW / DENY =======================================

def test_denied_table():
    r = Policy(denied_tables=["employees"]).check("SELECT * FROM employees", SCHEMA)
    assert not r.ok and r.status is Decision.DENIED


def test_allow_list_blocks_other_table():
    p = Policy(allowed_tables=["customers"])
    assert p.check("SELECT name FROM customers", SCHEMA).ok
    assert not p.check("SELECT * FROM orders", SCHEMA).ok


def test_cte_not_treated_as_base_table():
    p = Policy(allowed_tables=["customers"])
    assert p.check("WITH t AS (SELECT id FROM customers) SELECT * FROM t", SCHEMA).ok


# ============ FALSE-POSITIVE GUARDS (AST, not string matching) =========

def test_literal_containing_sensitive_word_not_flagged():
    allowed("SELECT name FROM customers WHERE name = 'ssn information'")
    allowed("SELECT name FROM customers WHERE country = 'has ssn in text'")


def test_substring_column_not_matched():
    # Only 'id' denied must NOT match 'customer_id'.
    p = Policy(denied_columns=["customers.id"])
    assert p.check("SELECT customer_id FROM orders", SCHEMA).ok
    assert not p.check("SELECT id FROM customers", SCHEMA).ok


def test_denied_id_does_not_leak_to_other_tables():
    p = Policy(denied_columns=["customers.id"])
    assert p.check("SELECT id FROM orders", SCHEMA).ok            # orders.id != customers.id
    assert p.check("SELECT o.id FROM orders o", SCHEMA).ok


def test_column_named_like_denied_in_other_table_allowed():
    # customers.ssn denied; employees.ssn (same bare name) is fine.
    allowed("SELECT e.name, e.ssn FROM employees e")


# ============ CASE-INSENSITIVITY =======================================

def test_case_insensitive_match():
    blocked("SELECT C.SSN FROM CUSTOMERS AS C", status=Decision.DENIED)


# ============ RESOLVER-LEVEL SANITY ====================================

def test_resolver_marks_status_correctly():
    rep = analyze("SELECT c.ssn, x.name FROM customers c", SCHEMA)
    by_ref = {r.sql_reference: r for r in rep.refs}
    assert by_ref["c.ssn"].status is RefStatus.RESOLVED
    assert by_ref["c.ssn"].canonical == "customers.ssn"
    assert by_ref["x.name"].status is RefStatus.UNRESOLVED


def test_resolver_provenance_lists():
    r = pol().check("SELECT c.name, c.ssn FROM customers c", SCHEMA)
    assert "customers.ssn" in r.denied_refs
    assert "customers.name" in r.resolved_refs


def test_explanation_present_on_block():
    r = blocked("SELECT ssn FROM customers", status=Decision.DENIED)
    assert r.explanation and "BLOCKED" in r.explanation
