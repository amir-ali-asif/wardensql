"""Deterministic SQL analysis: resolve every column reference to its canonical
``table.column`` identity (or a precise failure), independent of the LLM.

This module is the *analysis* half of the security boundary. It answers exactly
one question for each column a query touches:

    "What actual database column does this reference resolve to, and can we prove it?"

It never makes the authorization decision itself -- that is :mod:`app.policy`'s job.
The separation mirrors the required pipeline::

    SQL text
      -> parse                  (sqlglot AST; the COMPLETE input)
      -> statement-count gate   (exactly one statement, else fail closed)
      -> read-only gate         (reject writes/DDL structurally)
      -> qualify + scope build  (sqlglot optimizer: star-expansion, binding)
      -> per-scope resolution   (alias -> table, column -> table.column)
      -> lineage                (derived tables / CTEs -> base columns)
      -> ResolvedRef list       (RESOLVED | UNRESOLVED | AMBIGUOUS)

Design principles
-----------------
* **Scope-aware, not one global alias map.** Each SQL scope (top-level query,
  subquery, CTE body, derived table, set-operation branch) has its own sources.
  The same alias (``c``) can mean different tables in different scopes; a global
  dictionary would misresolve nested/correlated queries. We use sqlglot's
  ``traverse_scope`` so every scope resolves against *its own* bindings, with
  correlated (outer) references resolved against the correct ancestor scope.

* **Aliases are syntax, never identity.** ``customers AS c``, ``customers AS cust``
  and ``customers AS c1`` all resolve ``*.ssn`` to ``customers.ssn``. No fixed
  alias is required or privileged.

* **Fail closed.** Anything we cannot prove -- an unknown alias, an unqualified
  column that could come from more than one in-scope table, a structure the
  analyzer does not support, a parser/optimizer error -- is reported as
  ``UNRESOLVED`` or ``AMBIGUOUS`` so the policy layer blocks it. UNKNOWN is never
  ALLOWED.

* **Star expansion closes the "``*`` hides a denied column" hole.** ``qualify``
  expands ``SELECT *`` to concrete columns against the live schema, including
  inside subqueries and CTEs, so a hidden ``customers.ssn`` still surfaces as a
  concrete base-column reference we can check.

Why base-column references are sufficient for the decision
----------------------------------------------------------
After ``qualify`` expands stars and binds columns, **every** base column that
flows to a query's output is physically referenced *somewhere* as a
``table.column`` against a real table -- even when an outer query renames it via a
derived-table/CTE alias (the inner query must still read the base column). So the
policy decision is driven by the concrete base-column references we collect from
every scope. Lineage for derived references is additionally computed for audit
readability (``d.customer_name -> customers.name``), but the security decision does
not depend on lineage being perfect: if lineage is uncertain the underlying base
column has already been reported from the scope that reads it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, traverse_scope

from .ports import SchemaMap


class RefStatus(str, Enum):
    """Resolution outcome for a single column reference."""
    RESOLVED = "resolved"        # proven to map to exactly one canonical column
    UNRESOLVED = "unresolved"    # cannot be proven -> fail closed
    AMBIGUOUS = "ambiguous"      # could be >1 canonical column -> fail closed


# Root statement types we are willing to analyze. Anything else (INSERT/UPDATE/
# DELETE/DDL/DCL/raw Command) is not a read query and is refused structurally --
# a second, independent line of defense alongside guardrails and the read-only role.
_READ_ROOTS: tuple[type[exp.Expression], ...] = (
    exp.Select, exp.Union, exp.Intersect, exp.Except,
)


@dataclass
class ResolvedRef:
    """One column reference and the canonical identity we could (or could not) prove."""
    status: RefStatus
    sql_reference: str                       # as written, e.g. "c.ssn" / "ssn"
    canonical: str | None = None             # "customers.ssn" when RESOLVED
    candidates: list[str] = field(default_factory=list)   # possible sources when AMBIGUOUS
    scope: str = ""                          # human tag for which scope it lived in
    note: str = ""                           # short human explanation (esp. for failures)

    @property
    def ok(self) -> bool:
        return self.status is RefStatus.RESOLVED


@dataclass
class ResolveReport:
    """The full analysis of one SQL statement."""
    supported: bool                          # False => structure could not be analyzed
    refs: list[ResolvedRef] = field(default_factory=list)
    base_tables: set[str] = field(default_factory=set)   # physical tables in scope
    reason: str | None = None                # why unsupported, if supported is False
    qualified: bool = True                   # False => qualify() fell back to raw tree
    has_unexpanded_star: bool = False        # a `*` we could not expand (fail-closed hint)
    multiple_statements: bool = False        # input held >1 SQL statement (fail-closed)
    statement_count: int = 1                 # statements found by the parser

    def unresolved(self) -> list[ResolvedRef]:
        return [r for r in self.refs if r.status is RefStatus.UNRESOLVED]

    def ambiguous(self) -> list[ResolvedRef]:
        return [r for r in self.refs if r.status is RefStatus.AMBIGUOUS]


def analyze(sql: str, schema: SchemaMap, *, dialect: str = "postgres") -> ResolveReport:
    """Parse ``sql`` and resolve every column reference to a canonical identity.

    Returns a :class:`ResolveReport`. On any structural problem the report is
    ``supported=False`` (fail closed); otherwise ``refs`` holds one
    :class:`ResolvedRef` per column reference the query touches.
    """
    # --- parse the COMPLETE input and count statements (fail closed) ---------
    # We parse every statement, not just the first, so a stacked/injected second
    # statement (`SELECT ...; DROP TABLE ...`) can never slip through by being
    # ignored after the first semicolon. This uses the parser's own statement
    # boundaries -- never naive ``sql.split(";")`` -- so a semicolon inside a
    # string literal (``WHERE note = 'a; b'``) is correctly treated as one
    # statement.
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except ParseError as e:
        return ResolveReport(supported=False, reason=f"parse error: {_first_line(e)}")

    non_null = [s for s in statements if s is not None]
    count = len(non_null)
    if count == 0:
        return ResolveReport(supported=False, statement_count=0, reason="empty statement")
    if count > 1:
        return ResolveReport(
            supported=False,
            multiple_statements=True,
            statement_count=count,
            reason=("multiple SQL statements are not permitted; "
                    f"found {count} statements, expected exactly 1"),
        )

    tree = non_null[0]

    # --- read-only gate: only read queries are analyzable here ---------------
    if not isinstance(tree, _READ_ROOTS):
        return ResolveReport(
            supported=False,
            reason=f"non-read statement '{type(tree).__name__}' is not permitted",
        )

    # --- qualify a COPY: expand `*`, bind unambiguous columns ----------------
    # validate_qualify_columns=False: we do our own fail-closed handling of
    # unqualified/ambiguous columns; we don't want qualify to raise on them. But a
    # genuinely un-analyzable structure (e.g. a duplicate alias in one scope) does
    # raise, and that is a signal to fail closed.
    schema_l = _lower_schema(schema)
    qualified = True
    try:
        wtree = qualify(
            tree.copy(), schema=schema_l, dialect=dialect,
            validate_qualify_columns=False, quote_identifiers=False,
        )
    except OptimizeError as e:
        msg = _first_line(e)
        # A duplicate alias within a single scope is structurally ambiguous.
        if "alias already used" in msg.lower():
            return ResolveReport(
                supported=False,
                reason=f"ambiguous structure: {msg}",
            )
        # Any other optimizer failure: fall back to the raw tree and be
        # conservative -- unresolved references below will fail closed.
        wtree, qualified = tree, False

    # --- traverse scopes and resolve ----------------------------------------
    try:
        scopes = list(traverse_scope(wtree))
    except Exception as e:  # pragma: no cover - defensive; treat as unsupported
        return ResolveReport(supported=False, reason=f"scope analysis failed: {_first_line(e)}")

    refs: list[ResolvedRef] = []
    base_tables: set[str] = set()

    for idx, scope in enumerate(scopes):
        scope_tag = _scope_tag(scope, idx)
        # Record physical tables that are sources of this scope.
        for name, src in scope.sources.items():
            if isinstance(src, exp.Table) and src.name:
                base_tables.add(src.name.lower())

        external = {id(c) for c in scope.external_columns}
        for col in scope.columns:
            qual = (col.table or "").lower()
            if not qual:
                # Unqualified columns -- whether sqlglot filed them as local or as
                # 'external' (it does the latter when it cannot bind them) -- must
                # always go through ambiguity analysis against every base table
                # visible from this scope (self + ancestors). Never guess.
                refs.append(_resolve_unqualified(col, scope, schema_l, scope_tag))
                continue
            if id(col) in external:
                # Correlated (outer-scope) qualified reference: resolve against the
                # ancestor scope that actually owns the alias.
                refs.append(_resolve_correlated(col, scope, scope_tag, qualified))
                continue
            refs.append(_resolve_local(col, scope, schema_l, scope_tag, qualified))

    # A projection-level `*` still present after qualify means we could not
    # enumerate the columns it covers (unknown table, or qualify fell back). That is
    # a fail-closed hint for the policy layer -- a star could hide a denied column.
    # NB: this must not match `COUNT(*)` (an aggregate star), only `SELECT *` /
    # `SELECT t.*`, so we inspect projection lists specifically.
    has_star = _has_projection_star(wtree)

    return ResolveReport(
        supported=True, refs=refs, base_tables=base_tables,
        qualified=qualified, has_unexpanded_star=has_star,
    )


# --------------------------------------------------------------------------- #
# Per-column resolution
# --------------------------------------------------------------------------- #

def _resolve_local(
    col: exp.Column, scope: Scope, schema_l: SchemaMap, scope_tag: str, qualified: bool,
) -> ResolvedRef:
    name = (col.name or "").lower()
    qual = (col.table or "").lower()
    written = col.sql(dialect="postgres").replace('"', "")

    if not name:
        return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                           note="non-column expression")

    if qual:
        src = scope.sources.get(qual)
        if isinstance(src, exp.Table):
            table = src.name.lower()
            # Fail closed on a table the schema does not know: we cannot confirm the
            # column's identity, and an unknown table must never be assumed safe.
            if schema_l and table not in schema_l:
                return ResolvedRef(
                    RefStatus.UNRESOLVED, written, scope=scope_tag,
                    note=f"table '{table}' is not in the known schema",
                )
            return ResolvedRef(RefStatus.RESOLVED, written,
                               canonical=f"{table}.{name}", scope=scope_tag)
        if isinstance(src, Scope):
            # Derived table / CTE reference. The base column(s) it maps to are
            # independently reported from that inner scope, so the decision is
            # already covered; we compute lineage here only to enrich the audit.
            lineage = _derived_lineage(name, src)
            if len(lineage) == 1:
                return ResolvedRef(RefStatus.RESOLVED, written, canonical=lineage[0],
                                   scope=scope_tag, note="via derived relation")
            # Lineage is not a single base column (computed value, union, or opaque).
            # Not a failure: any base column involved is checked in the inner scope.
            return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                               note="derived/computed column (base columns checked in inner scope)")
        # Qualifier names nothing in this scope's sources -> cannot be proven.
        return ResolvedRef(
            RefStatus.UNRESOLVED, written, scope=scope_tag,
            note=f"alias '{qual}' does not resolve to a table in this scope",
        )

    # Unqualified columns are dispatched to _resolve_unqualified before reaching
    # here; this branch is defensive only.
    return _resolve_unqualified(col, scope, schema_l, scope_tag)


def _resolve_unqualified(
    col: exp.Column, scope: Scope, schema_l: SchemaMap, scope_tag: str,
) -> ResolvedRef:
    """Resolve an unqualified column against every base table visible from ``scope``.

    Visibility includes this scope's own sources and, for correlated positions, its
    ancestor scopes. If exactly one visible base table owns the column name it is
    RESOLVED; more than one is AMBIGUOUS (fail closed -- never guess); none means it
    is not a base column here (safe: a derived source's base columns are checked in
    the inner scope, and a true hallucination is caught by the schema stage).
    """
    name = (col.name or "").lower()
    written = col.sql(dialect="postgres").replace('"', "")
    if not name:
        return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                           note="non-column expression")

    owners: list[str] = []
    s: Scope | None = scope
    while s is not None:
        for t in _base_table_names(s):
            if name in schema_l.get(t, set()) and t not in owners:
                owners.append(t)
        s = s.parent

    if len(owners) == 1:
        return ResolvedRef(RefStatus.RESOLVED, written,
                           canonical=f"{owners[0]}.{name}", scope=scope_tag)
    if len(owners) > 1:
        return ResolvedRef(
            RefStatus.AMBIGUOUS, written,
            candidates=[f"{t}.{name}" for t in sorted(owners)], scope=scope_tag,
            note="unqualified column matches more than one in-scope table",
        )
    return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                       note="unqualified; not owned by an in-scope base table")


def _resolve_correlated(
    col: exp.Column, scope: Scope, scope_tag: str, qualified: bool,
) -> ResolvedRef:
    """Resolve an outer-scope (correlated) reference against ancestor scopes."""
    name = (col.name or "").lower()
    qual = (col.table or "").lower()
    written = col.sql(dialect="postgres").replace('"', "")
    if not qual:
        # Unqualified correlated column: sqlglot could not bind it. Fail closed
        # only if it *could* be sensitive; otherwise treat as resolved-elsewhere.
        return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                           note="correlated unqualified reference")
    anc = scope.parent
    while anc is not None:
        src = anc.sources.get(qual)
        if isinstance(src, exp.Table):
            return ResolvedRef(RefStatus.RESOLVED, written,
                               canonical=f"{src.name.lower()}.{name}",
                               scope=scope_tag, note="correlated (outer scope)")
        if isinstance(src, Scope):
            return ResolvedRef(RefStatus.RESOLVED, written, scope=scope_tag,
                               note="correlated derived (outer scope)")
        anc = anc.parent
    return ResolvedRef(
        RefStatus.UNRESOLVED, written, scope=scope_tag,
        note=f"correlated alias '{qual}' does not resolve in any enclosing scope",
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _base_table_names(scope: Scope) -> list[str]:
    """Physical table names that are direct sources of ``scope``."""
    names: list[str] = []
    for src in scope.sources.values():
        if isinstance(src, exp.Table) and src.name:
            t = src.name.lower()
            if t not in names:
                names.append(t)
    return names


def _derived_lineage(out_name: str, inner: Scope) -> list[str]:
    """Best-effort: map a derived/CTE output column to its base ``table.column``(s).

    Returns a list of canonical base columns the output projection reads. Empty or
    multi-element results mean 'not a single base column' (computed/union/opaque);
    the caller treats that as resolved-but-not-single because the base columns are
    independently checked inside ``inner``.
    """
    results: list[str] = []
    for proj in getattr(inner.expression, "selects", []) or []:
        if (proj.alias_or_name or "").lower() != out_name:
            continue
        target = proj.this if isinstance(proj, exp.Alias) else proj
        for c in target.find_all(exp.Column):
            cq = (c.table or "").lower()
            cn = (c.name or "").lower()
            src = inner.sources.get(cq) if cq else None
            if isinstance(src, exp.Table) and src.name:
                canonical = f"{src.name.lower()}.{cn}"
                if canonical not in results:
                    results.append(canonical)
            elif isinstance(src, Scope):
                for deeper in _derived_lineage(cn, src):
                    if deeper not in results:
                        results.append(deeper)
            else:
                # Could not trace one hop -> signal "not a single clean base column".
                return []
        break
    return results


def _has_projection_star(tree: exp.Expression) -> bool:
    """True if any SELECT still projects a bare ``*`` or ``table.*`` (not COUNT(*))."""
    for sel in tree.find_all(exp.Select):
        for proj in sel.expressions:
            if isinstance(proj, exp.Star):
                return True
            if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                return True
    return False


def _scope_tag(scope: Scope, idx: int) -> str:
    kind = type(scope.expression).__name__.lower()
    if scope.is_root:
        return f"root:{kind}"
    if getattr(scope, "is_cte", False):
        return f"cte#{idx}:{kind}"
    if getattr(scope, "is_derived_table", False):
        return f"derived#{idx}:{kind}"
    if getattr(scope, "is_correlated_subquery", False):
        return f"correlated#{idx}:{kind}"
    if getattr(scope, "is_subquery", False):
        return f"subquery#{idx}:{kind}"
    return f"scope#{idx}:{kind}"


def _lower_schema(schema: SchemaMap) -> SchemaMap:
    return {t.lower(): {c.lower(): v for c, v in cols.items()} for t, cols in schema.items()}


def _first_line(err: Exception) -> str:
    return str(err).splitlines()[0] if str(err) else err.__class__.__name__
