"""Tests for the result-set comparison oracle."""

from eval.compare import compare_results


def test_identical():
    assert compare_results([["US", 2]], [["US", 2]])


def test_row_order_ignored_by_default():
    gold = [["US", 2], ["UK", 1]]
    pred = [["UK", 1], ["US", 2]]
    assert compare_results(gold, pred)


def test_row_order_enforced_when_requested():
    gold = [["Alice", 2], ["Bob", 1]]
    pred = [["Bob", 1], ["Alice", 2]]
    assert compare_results(gold, pred) is True
    assert compare_results(gold, pred, order_matters=True) is False


def test_column_order_ignored_when_unordered():
    assert compare_results([["US", 2]], [[2, "US"]])


def test_number_formatting_equivalence():
    assert compare_results([[49.99]], [["49.990"]])
    assert compare_results([[2]], [[2.0]])


def test_numeric_string_matches_number():
    assert compare_results([[19.99]], [["19.99"]])


def test_string_whitespace_trimmed():
    assert compare_results([["US"]], [[" US "]])


def test_case_is_not_folded():
    assert compare_results([["US"]], [["us"]]) is False


def test_different_row_count_fails():
    assert compare_results([["US", 2]], [["US", 2], ["UK", 1]]) is False


def test_different_values_fail():
    assert compare_results([["US", 2]], [["US", 3]]) is False


def test_none_handling():
    assert compare_results([[None, 1]], [[1, None]])
    assert compare_results([[None]], [[0]]) is False


def test_empty_results_equal():
    assert compare_results([], [])
