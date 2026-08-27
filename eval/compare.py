"""Decide whether two SQL result sets are equivalent.

This is the correctness oracle for the whole eval harness. It is a pure function
(no DB, no pipeline) so it can be unit-tested exhaustively on its own.

Equivalence rules:
  * Numbers are compared as numbers (49.99 == 49.990, 2 == 2.0), within a tiny
    tolerance for float rounding.
  * Strings are compared after trimming surrounding whitespace (never case-folded).
  * By default rows are compared UNORDERED (a GROUP BY has no guaranteed order) and
    the values WITHIN a row are compared as an unordered multiset (column order is
    not meaningful). This is the right default for "same information".
  * When order_matters=True, row order is preserved AND column position is kept,
    for questions like "top N by ..." where the sequence is the answer.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

_FLOAT_TOL = 1e-9


def _norm_value(v: object) -> object:
    """Normalize a single cell so equivalent values compare equal."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v  # keep booleans distinct from 0/1
    if isinstance(v, (int, float, Decimal)):
        return round(float(v), 9)
    if isinstance(v, str):
        s = v.strip()
        try:
            return round(float(Decimal(s)), 9)
        except (InvalidOperation, ValueError):
            return s
    return v


def _norm_row_unordered(row: list) -> tuple:
    """Normalize a row and make it order-independent (sorted multiset of cells)."""
    normed = [_norm_value(v) for v in row]
    return tuple(sorted(normed, key=lambda x: (x is not None, str(x))))


def _norm_row_ordered(row: list) -> tuple:
    """Normalize a row but keep column positions (order matters)."""
    return tuple(_norm_value(v) for v in row)


def compare_results(
    gold: list[list],
    pred: list[list],
    *,
    order_matters: bool = False,
) -> bool:
    """Return True if the predicted result set is equivalent to the gold one."""
    if gold is None or pred is None:
        return False
    if len(gold) != len(pred):
        return False

    if order_matters:
        g = [_norm_row_ordered(r) for r in gold]
        p = [_norm_row_ordered(r) for r in pred]
        return g == p

    g = sorted((_norm_row_unordered(r) for r in gold), key=str)
    p = sorted((_norm_row_unordered(r) for r in pred), key=str)
    return g == p
