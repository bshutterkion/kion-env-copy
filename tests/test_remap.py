"""Unit tests for the pure id/name resolution logic (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kion.import_ import (  # noqa: E402
    _amounts_equal,
    _missing_budget_months,
    find_root_ou_id,
    nkey,
    order_ous,
    resolve_owners,
    resolve_scheme,
)


def test_missing_budget_months_finds_gap():
    # 2018-10..2022-01 with one month (2020-12) absent
    months = []
    for y in range(2018, 2022):
        for m in range(1, 13):
            if (y, m) in ((2018, x) for x in range(1, 10)):
                continue
            months.append(f"{y:04d}-{m:02d}")
    rows = [{"datecode": d} for d in months
            if not (d < "2018-10" or d > "2021-12") and d != "2020-12"]
    b = {"start_datecode": "2018-10", "end_datecode": "2022-01", "data": rows}
    assert _missing_budget_months(b) == ["2020-12"]


def test_missing_budget_months_none_when_full():
    rows = [{"datecode": f"2026-{m:02d}"} for m in range(1, 13)]
    b = {"start_datecode": "2026-01", "end_datecode": "2027-01", "data": rows}
    assert _missing_budget_months(b) == []


def test_missing_budget_months_handles_december_boundary():
    # December must round-trip correctly (no off-by-one wrap)
    rows = [{"datecode": f"2026-{m:02d}"} for m in range(1, 12)]  # missing 2026-12
    b = {"start_datecode": "2026-01", "end_datecode": "2027-01", "data": rows}
    assert _missing_budget_months(b) == ["2026-12"]


def test_nkey_normalizes_case_and_whitespace():
    assert nkey("  Shared Services ") == "shared services"
    assert nkey(None) == ""


def test_amounts_equal_across_types():
    assert _amounts_equal("30000", 30000)
    assert _amounts_equal(30000.0, "30000")
    assert not _amounts_equal("30000", "40000")


def test_resolve_scheme_matched_by_name():
    assert resolve_scheme("Admin", {"Admin": 7}, "Default Project Permissions Scheme", None) == (7, "matched")


def test_resolve_scheme_falls_back_to_type_default():
    schemes = {"Default Project Permissions Scheme": 3}
    assert resolve_scheme(None, schemes, "Default Project Permissions Scheme", 99) == (3, "type_default")


def test_resolve_scheme_falls_back_to_configured_default():
    assert resolve_scheme("Nope", {"Admin": 7}, "Missing Type Scheme", 99) == (99, "default")


def test_resolve_scheme_unresolved_without_any_default():
    assert resolve_scheme("Nope", {"Admin": 7}, "Missing Type Scheme", None) == (None, "unresolved")


def test_resolve_owners_maps_and_skips_missing():
    users = {"a@x.com": 1, "b@x.com": 2}
    groups = {"Eng": 10}
    uids, gids, dropped = resolve_owners(
        ["A@x.com", "missing@x.com"], ["Eng", "Ghost"], users, groups
    )
    assert uids == [1]  # case-insensitive email match
    assert gids == [10]
    assert dropped == ["user:missing@x.com", "group:Ghost"]


def test_find_root_ou_handles_null_and_zero_parent():
    assert find_root_ou_id([{"id": 5, "parent_ou_id": 0}, {"id": 6, "parent_ou_id": 5}]) == 5
    assert find_root_ou_id([{"id": 1, "parent_ou_id": None}]) == 1


def test_order_ous_parents_before_children():
    ous = [
        {"id": 3, "parent_ou_id": 2},
        {"id": 2, "parent_ou_id": 1},
        {"id": 1, "parent_ou_id": None},
    ]
    ordered = [o["id"] for o in order_ous(ous)]
    assert ordered.index(1) < ordered.index(2) < ordered.index(3)
