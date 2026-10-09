"""DT-19 / FE-13: a coverage percentage never rounds up to 100.0 while one asset is missing, nor down to 0.0 while
one is covered, so a pill, its words and the exact 'N missing' link always agree."""
from app.services import inventory_query as iq


def test_a_single_missing_item_in_a_huge_group_never_reads_one_hundred_percent():
    assert iq._coverage_pct(100_000, 1) == 99.9
    assert iq._coverage_pct(10_000, 3) == 99.9


def test_a_single_covered_item_in_a_huge_group_never_reads_zero_percent():
    assert iq._coverage_pct(100_000, 99_999) == 0.1


def test_the_exact_ends_stay_exact():
    assert iq._coverage_pct(50, 0) == 100.0
    assert iq._coverage_pct(50, 50) == 0.0
    assert iq._coverage_pct(0, 0) is None


def test_ordinary_values_are_rounded_to_one_decimal():
    assert iq._coverage_pct(3, 1) == 66.7
    assert iq._coverage_pct(8, 2) == 75.0
