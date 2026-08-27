"""Cost guard: reject queries whose plan is too expensive BEFORE running them.

On a large production database, a syntactically-fine query can still trigger a
full-table cartesian scan. Running `EXPLAIN (FORMAT JSON)` (read-only, no ANALYZE)
gives Postgres's estimated total cost and row count; we reject anything above the
configured ceilings. The parser is separated out so it can be unit-tested against
sample plans without a live database.
"""

from __future__ import annotations

from .ports import ExplainCost


def parse_explain_cost(plan_json: list | dict) -> ExplainCost:
    """Extract (total_cost, plan_rows) from an EXPLAIN (FORMAT JSON) result."""
    try:
        root = plan_json[0] if isinstance(plan_json, list) else plan_json
        plan = root["Plan"]
        return ExplainCost(
            ok=True,
            total_cost=float(plan.get("Total Cost", 0.0)),
            plan_rows=float(plan.get("Plan Rows", 0.0)),
        )
    except (KeyError, IndexError, TypeError, ValueError) as e:
        return ExplainCost(ok=False, error=f"could not parse plan: {e}")


def within_budget(cost: ExplainCost, *, max_cost: float, max_rows: float) -> tuple[bool, str | None]:
    if not cost.ok:
        return False, cost.error
    if cost.total_cost > max_cost:
        return False, f"estimated cost {cost.total_cost:.0f} exceeds limit {max_cost:.0f}"
    if cost.plan_rows > max_rows:
        return False, f"estimated rows {cost.plan_rows:.0f} exceeds limit {max_rows:.0f}"
    return True, None
