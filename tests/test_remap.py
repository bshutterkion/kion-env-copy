"""Unit tests for the pure id/name resolution logic (no network)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from kion.import_ import (  # noqa: E402
    _amounts_equal,
    _missing_budget_months,
    account_cache_payload,
    account_project_payload,
    find_root_ou_id,
    nkey,
    order_ous,
    parse_only,
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


# --- account create payloads -------------------------------------------------

def test_account_project_payload_aws_associates_and_maps_ids():
    a = {"provider": "aws", "account_number": "111122223333", "account_name": "prod",
         "account_type_id": 1, "start_datecode": "2024-01"}
    path, payload = account_project_payload(a, proj_new=42, payer_new=7)
    assert path == "/v3/account?account-type=aws"
    assert payload["project_id"] == 42
    assert payload["payer_id"] == 7
    assert payload["account_number"] == "111122223333"
    assert payload["start_datecode"] == "2024-01"
    assert payload["account_type_id"] == 1
    assert payload["skip_access_checking"] is True


def test_account_cache_payload_aws_has_no_project_and_requires_type_id():
    a = {"provider": "aws", "account_number": "111122223333", "account_name": "prod",
         "account_type_id": 1, "start_datecode": "2024-01"}
    path, payload = account_cache_payload(a, payer_new=7)
    assert path == "/v3/account-cache?account-type=aws"
    assert "project_id" not in payload      # cache accounts are unassociated
    assert "start_datecode" not in payload  # not accepted by the cache endpoint
    assert payload["payer_id"] == 7
    assert payload["account_number"] == "111122223333"
    assert payload["account_type_id"] == 1  # required for aws cache create
    assert payload["skip_access_checking"] is True


def test_account_cache_payload_number_field_by_provider():
    def num_field(provider, number):
        _, p = account_cache_payload(
            {"provider": provider, "account_number": number, "account_name": "x",
             "account_type_id": 3}, payer_new=1)
        return p
    assert num_field("azure", "sub-uuid")["subscription_uuid"] == "sub-uuid"
    assert num_field("google-cloud", "gcp-proj")["google_cloud_project_id"] == "gcp-proj"
    assert num_field("oci", "ocid.tenancy")["tenancy_ocid"] == "ocid.tenancy"


def test_account_cache_payload_custom_omits_type_id_and_skip_check():
    a = {"provider": "custom", "account_number": "acc-1", "account_name": "x",
         "account_type_id": 29}
    _, payload = account_cache_payload(a, payer_new=1)
    # the custom cache-create schema has neither field
    assert "account_type_id" not in payload
    assert "skip_access_checking" not in payload


def test_account_cache_payload_aws_linked_uses_cache_field_name():
    a = {"provider": "aws", "account_number": "n", "account_name": "x",
         "account_type_id": 2, "linked_account_number": "govcloud-123",
         "include_linked_account_spend": True}
    _, payload = account_cache_payload(a, payer_new=1)
    # cache endpoint uses linked_account_number, NOT linked_aws_account_number
    assert payload["linked_account_number"] == "govcloud-123"
    assert "linked_aws_account_number" not in payload
    assert payload["include_linked_account_spend"] is True


# --- --only selector ---------------------------------------------------------

def test_parse_only_none_means_all():
    assert parse_only(None) is None
    assert parse_only("") is None
    assert parse_only("  ") is None


def test_parse_only_normalizes_and_strips():
    assert parse_only("billing_sources, accounts") == {"billing_sources", "accounts"}


def test_parse_only_rejects_unknown_kind():
    with pytest.raises(ValueError, match="unknown kind"):
        parse_only("billing_sources,widgets")


def test_parse_only_accounts_requires_billing_sources():
    with pytest.raises(ValueError, match="requires 'billing_sources'"):
        parse_only("accounts")
    # billing_sources alone (no accounts) is fine
    assert parse_only("billing_sources") == {"billing_sources"}


# --- account routing (drives the real _reconcile_accounts, no network) --------

def _accounts_importer(accounts, billing_map=None, project_map=None):
    """An Importer in plan mode with account routing dependencies pre-seeded and
    _post stubbed to a recorder, so we can assert routing without any network."""
    from kion.import_ import Importer

    imp = Importer(client=None, config=None, snapshot={"accounts": accounts}, apply=False)
    imp.id_map["billing_sources"].update(billing_map or {})
    imp.id_map["projects"].update(project_map or {})
    imp._t_acct_ids = set()
    imp._t_acct_by_number = {}
    calls = []
    imp._post = lambda kind, path, payload, src, action, label: (
        calls.append({"path": path, "payload": payload, "label": label}) or f"<new:{src}>"
    )
    return imp, calls


def test_reconcile_accounts_associates_when_project_resolves():
    acct = {"source_id": 1, "provider": "aws", "account_number": "n1",
            "account_name": "prod", "account_type_id": 1, "project_id": 5, "payer_id": 9}
    imp, calls = _accounts_importer([acct], billing_map={"9": 900}, project_map={"5": 50})
    imp._reconcile_accounts()
    assert len(calls) == 1
    assert calls[0]["path"] == "/v3/account?account-type=aws"
    assert calls[0]["payload"]["project_id"] == 50
    assert calls[0]["payload"]["payer_id"] == 900


def test_reconcile_accounts_routes_to_cache_when_project_unresolved():
    acct = {"source_id": 1, "provider": "aws", "account_number": "n1",
            "account_name": "prod", "account_type_id": 1, "project_id": 5, "payer_id": 9}
    # payer resolves, but project 5 is not in the id-map (projects weren't synced)
    imp, calls = _accounts_importer([acct], billing_map={"9": 900}, project_map={})
    imp._reconcile_accounts()
    assert len(calls) == 1
    assert calls[0]["path"] == "/v3/account-cache?account-type=aws"
    assert "project_id" not in calls[0]["payload"]
    assert "→ cache" in calls[0]["label"]


def test_reconcile_accounts_cache_sourced_goes_to_cache():
    acct = {"source_id": "cache:1", "provider": "custom", "account_number": "n1",
            "account_name": "x", "account_type_id": 29, "project_id": None, "payer_id": 9}
    imp, calls = _accounts_importer([acct], billing_map={"9": 900}, project_map={"5": 50})
    imp._reconcile_accounts()
    assert len(calls) == 1
    assert calls[0]["path"] == "/v3/account-cache?account-type=custom"


def test_reconcile_accounts_skips_when_payer_unrecreatable():
    acct = {"source_id": 1, "provider": "azure", "account_number": "n1",
            "account_name": "x", "account_type_id": 3, "project_id": 5, "payer_id": 9}
    # billing source 9 not on target (e.g. azure payer wasn't recreatable)
    imp, calls = _accounts_importer([acct], billing_map={}, project_map={"5": 50})
    imp._reconcile_accounts()
    assert calls == []
    assert imp.skipped["accounts"] == 1
