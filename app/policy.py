"""Data governance (ABAC): deterministic, canonical, fail-closed column/table policy.

This is the **authorization** half of the security boundary. It never generates or
trusts SQL semantics from the LLM; it consumes the deterministic analysis produced
by :mod:`app.resolver` (which turns every column reference into a canonical
``table.column`` identity or a precise failure) and applies the policy:

    resolver: SQL  ->  canonical refs (RESOLVED | UNRESOLVED | AMBIGUOUS)
    policy   : canonical refs + deny/allow lists  ->  ALLOW | BLOCK

Policies are expressed with **canonical database identifiers only**::

    denied_columns = ["customers.ssn", "employees.salary"]
    denied_tables  = ["audit_log"]
    allowed_tables = ["customers", "orders", ...]      # empty = allow all

Aliases are never part of policy. ``customers AS c``, ``customers AS cust`` and
``customers AS c1`` all resolve ``*.ssn`` to ``customers.ssn``; the model may use
any valid alias. (A configured ``table_aliases`` map, if present, is only a
*generation hint* for the prompt -- it is deliberately **not** a security gate.)

Fail-closed decision model
--------------------------
Every reference the query touches ends in exactly one of four states, and only the
first can proceed:

    RESOLVED + allowed    -> ALLOW
    RESOLVED + denied     -> BLOCK   (column explicitly denied by policy)
    AMBIGUOUS (denied?)   -> BLOCK   (could be a denied column; never guess)
    UNRESOLVED            -> BLOCK   (cannot prove the source; never assume safe)

Unsupported / un-analyzable structures (parse errors, non-read statements,
duplicate aliases, optimizer failures) are treated as UNRESOLVED and blocked. The
LLM judge can never override any of these decisions -- the pipeline runs this gate
*before* the judge and refuses to execute a blocked query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .ports import SchemaMap
from .resolver import RefStatus, ResolvedRef, analyze


class Decision(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"
    UNRESOLVED = "unresolved"
    AMBIGUOUS = "ambiguous"
    MULTIPLE_STATEMENTS = "multiple_statements"


@dataclass
class PolicyResult:
    # Back-compatible core fields (unchanged shape used by the pipeline/tests).
    ok: bool
    reason: str | None = None
    original_ref: str | None = None      # e.g. "c.ssn" (as written in the query)
    canonical_ref: str | None = None     # e.g. "customers.ssn" (what policy saw)

    # Richer, structured decision surface (additive -- safe for existing callers).
    status: Decision = Decision.ALLOWED
    candidates: list[str] = field(default_factory=list)   # for AMBIGUOUS
    scope: str | None = None
    explanation: str | None = None       # human-readable audit block

    # Full provenance for the audit trail (identifiers only, never values).
    resolved_refs: list[str] = field(default_factory=list)
    denied_refs: list[str] = field(default_factory=list)
    unresolved_refs: list[str] = field(default_factory=list)
    ambiguous_refs: list[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return not self.ok


class Policy:
    def __init__(
        self,
        *,
        allowed_tables: list[str] | None = None,
        denied_tables: list[str] | None = None,
        denied_columns: list[str] | None = None,
        table_aliases: dict[str, str] | None = None,
    ) -> None:
        self.allowed = {t.lower() for t in (allowed_tables or [])}
        self.denied_tables = {t.lower() for t in (denied_tables or [])}
        self.denied_columns = {c.lower() for c in (denied_columns or [])}   # "table.column"
        # Accepted for backward compatibility and used only as a *generation hint*
        # elsewhere. Intentionally NOT enforced here: aliases are SQL syntax, not the
        # canonical identity of data, so requiring a fixed alias would be neither a
        # correct nor a sufficient security mechanism.
        self.table_aliases = {
            t.lower(): a.lower() for t, a in (table_aliases or {}).items()
        }
        self._denied_col_names = {
            c.split(".", 1)[1] for c in self.denied_columns if "." in c
        }

    # ---- public API -------------------------------------------------------

    def check(self, sql: str, schema: SchemaMap, *, dialect: str = "postgres") -> PolicyResult:
        report = analyze(sql, schema, dialect=dialect)

        # (0a) Exactly-one-statement gate. A stacked/injected second statement
        # (`SELECT ...; DROP TABLE ...`) must be blocked with a distinct status,
        # never validated-first-then-ignored. This is enforced before anything
        # else touches the query.
        if report.multiple_statements:
            reason = ("multiple SQL statements are not permitted; the system "
                      "accepts exactly one SQL statement per request")
            return PolicyResult(
                ok=False, reason=reason, status=Decision.MULTIPLE_STATEMENTS,
                explanation=_explain_multiple_statements(report.statement_count),
            )

        # (0b) Any other un-analyzable structure -> fail closed as UNRESOLVED.
        if not report.supported:
            reason = report.reason or "query structure could not be analyzed"
            return self._unresolved_result(
                reason=reason, ref=None,
                explanation=_explain_unresolved(None, reason),
            )

        schema_lower = {t.lower() for t in schema}

        # (1) Table-level allow/deny (unchanged semantics).
        for tbl in sorted(report.base_tables):
            if tbl in self.denied_tables:
                r = f"access to table '{tbl}' is not permitted"
                return PolicyResult(
                    ok=False, reason=r, status=Decision.DENIED,
                    canonical_ref=tbl, explanation=_explain_denied_table(tbl),
                )
            if self.allowed and tbl in schema_lower and tbl not in self.allowed:
                r = f"table '{tbl}' is not in the allow-list"
                return PolicyResult(
                    ok=False, reason=r, status=Decision.DENIED,
                    canonical_ref=tbl, explanation=_explain_denied_table(tbl, allow_list=True),
                )

        # No column policy configured -> nothing further to enforce.
        if not self.denied_columns:
            return self._allowed_result(report.refs)

        # (2) A projection-level `*` we could not expand could hide a denied column.
        if report.has_unexpanded_star:
            reason = ("could not expand '*' to concrete columns; unable to prove no "
                      "denied column is selected")
            return self._unresolved_result(
                reason=reason, ref="*", refs=report.refs,
                explanation=_explain_unresolved("*", reason),
            )

        # (3) Column-level ABAC over canonical references.
        # Precedence for the *reported* reason: an explicit denial is the clearest
        # signal, then an ambiguity that could be a denied column, then any
        # unresolved reference. All of them block.
        denied = self._first_denied(report.refs)
        if denied is not None:
            ref, canonical = denied
            return PolicyResult(
                ok=False, reason=f"column '{canonical}' is not permitted",
                original_ref=ref.sql_reference, canonical_ref=canonical,
                status=Decision.DENIED, scope=ref.scope,
                explanation=_explain_denied_column(ref, canonical),
                **self._provenance(report.refs, extra_denied=[canonical]),
            )

        amb = self._first_ambiguous_denied(report.refs)
        if amb is not None:
            hits = [c for c in amb.candidates if c in self.denied_columns]
            return PolicyResult(
                ok=False,
                reason=f"ambiguous column '{amb.sql_reference}'; unable to determine "
                       f"canonical table",
                original_ref=amb.sql_reference, candidates=amb.candidates,
                status=Decision.AMBIGUOUS, scope=amb.scope,
                explanation=_explain_ambiguous(amb, hits),
                **self._provenance(report.refs),
            )

        unresolved = self._first_unresolved(report.refs)
        if unresolved is not None:
            return self._unresolved_result(
                reason=f"unable to resolve reference '{unresolved.sql_reference}': "
                       f"{unresolved.note}",
                ref=unresolved.sql_reference, refs=report.refs, scope=unresolved.scope,
                explanation=_explain_unresolved(unresolved.sql_reference, unresolved.note),
            )

        return self._allowed_result(report.refs)

    # ---- decision helpers -------------------------------------------------

    def _first_denied(self, refs: list[ResolvedRef]) -> tuple[ResolvedRef, str] | None:
        for r in refs:
            if r.status is RefStatus.RESOLVED and r.canonical \
                    and r.canonical in self.denied_columns:
                return r, r.canonical
        return None

    def _first_ambiguous_denied(self, refs: list[ResolvedRef]) -> ResolvedRef | None:
        for r in refs:
            if r.status is RefStatus.AMBIGUOUS \
                    and any(c in self.denied_columns for c in r.candidates):
                return r
        return None

    def _first_unresolved(self, refs: list[ResolvedRef]) -> ResolvedRef | None:
        for r in refs:
            if r.status is RefStatus.UNRESOLVED:
                return r
        return None

    def _allowed_result(self, refs: list[ResolvedRef]) -> PolicyResult:
        return PolicyResult(
            ok=True, status=Decision.ALLOWED,
            explanation="All column references resolved to permitted columns.",
            **self._provenance(refs),
        )

    def _unresolved_result(self, *, reason: str, ref: str | None,
                           refs: list[ResolvedRef] | None = None,
                           scope: str | None = None,
                           explanation: str | None = None) -> PolicyResult:
        prov = self._provenance(refs or [])
        if ref and ref not in prov["unresolved_refs"]:
            prov["unresolved_refs"] = prov["unresolved_refs"] + [ref]
        return PolicyResult(
            ok=False, reason=reason, original_ref=ref, status=Decision.UNRESOLVED,
            scope=scope, explanation=explanation, **prov,
        )

    def _provenance(self, refs: list[ResolvedRef], *, extra_denied: list[str] | None = None) -> dict:
        resolved, denied, unresolved, ambiguous = [], list(extra_denied or []), [], []
        for r in refs:
            if r.status is RefStatus.RESOLVED and r.canonical:
                if r.canonical in self.denied_columns:
                    if r.canonical not in denied:
                        denied.append(r.canonical)
                elif r.canonical not in resolved:
                    resolved.append(r.canonical)
            elif r.status is RefStatus.UNRESOLVED:
                unresolved.append(r.sql_reference)
            elif r.status is RefStatus.AMBIGUOUS:
                ambiguous.append(r.sql_reference)
        return {
            "resolved_refs": resolved,
            "denied_refs": denied,
            "unresolved_refs": unresolved,
            "ambiguous_refs": ambiguous,
        }


# --------------------------------------------------------------------------- #
# Human-readable explanations (audit / developer output)
# --------------------------------------------------------------------------- #

def _explain_denied_column(ref: ResolvedRef, canonical: str) -> str:
    return (
        "SECURITY: BLOCKED\n"
        f"Reference:  {ref.sql_reference}\n"
        f"Resolved:   {canonical}\n"
        f"Scope:      {ref.scope}\n"
        "Policy:     denied_columns contains "
        f"{canonical}\n"
        "Reason:     The query accesses a column explicitly denied by policy."
    )


def _explain_denied_table(table: str, *, allow_list: bool = False) -> str:
    why = ("table is not in the allow-list" if allow_list
           else "table is explicitly denied by policy")
    return (
        "SECURITY: BLOCKED\n"
        f"Reference:  {table}\n"
        f"Policy:     {why}\n"
        "Reason:     The query accesses a table it is not permitted to read."
    )


def _explain_ambiguous(ref: ResolvedRef, denied_hits: list[str]) -> str:
    sources = "\n            ".join(ref.candidates)
    tail = (f"\nPolicy:     denied_columns contains {', '.join(denied_hits)}"
            if denied_hits else "")
    return (
        "SECURITY: BLOCKED\n"
        f"Reference:  {ref.sql_reference}\n"
        "Status:     AMBIGUOUS\n"
        f"Scope:      {ref.scope}\n"
        f"Possible:   {sources}{tail}\n"
        "Reason:     The unqualified column matches multiple possible source "
        "columns; the system refuses to guess."
    )


def _explain_unresolved(ref: str | None, note: str) -> str:
    head = f"Reference:  {ref}\n" if ref else ""
    return (
        "SECURITY: BLOCKED\n"
        f"{head}"
        "Status:     UNRESOLVED\n"
        f"Reason:     {note}\n"
        "            The security layer operates fail-closed: a reference it cannot "
        "safely\n            resolve is blocked rather than assumed safe."
    )


def _explain_multiple_statements(count: int) -> str:
    return (
        "SECURITY: BLOCKED\n"
        "Status:     MULTIPLE_STATEMENTS\n"
        f"Found:      {count} statements\n"
        "Reason:     Multiple SQL statements are not permitted. The system accepts "
        "exactly\n            one SQL statement per request. The complete input is "
        "parsed and its\n            statement boundaries counted before any execution, "
        "so a stacked or\n            injected second statement "
        "(e.g. '...; DROP TABLE ...') is refused here\n            rather than validated "
        "first and ignored."
    )
