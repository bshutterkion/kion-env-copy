import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.overrides.registry import HOOKS

def test_account_hook_routes_to_cache_without_project():
    ctx = SimpleNamespace(id_map={"billing_source": {"2": 900}, "project": {}},
                          t_acct_by_number={})
    rec = {"provider": "custom", "account_number": "n", "account_name": "x",
           "payer_id": 2, "project_id": None, "__srcid__payer_id": 2,
           "__srcid__project_id": None}
    paths, payload = HOOKS["account"].build_create_payload(rec, ctx)
    assert paths[0].startswith("/v3/account-cache")
    assert "project_id" not in payload

def test_account_hook_identity_rejects_blank_number():
    assert HOOKS["account"].identity_ok({"account_number": ""}, None) is False


def _fake_ctx(id_map, scheme_result=(500, "matched"), owners_result=([1], [2]),
              target_root_id=None):
    return SimpleNamespace(
        id_map=id_map,
        target_root_id=target_root_id,
        resolve_scheme=lambda name, entity_type, label: scheme_result,
        resolve_owners=lambda rec, label: owners_result,
    )


def test_funding_source_hook_builds_payload():
    ctx = _fake_ctx(id_map={"ou": {"10": 100}})
    rec = {
        "name": "FS1", "description": "desc", "amount": 500,
        "start_datecode": "202401", "end_datecode": "202412",
        "__srcid__ou_id": 10, "permission_scheme_name": None,
    }
    paths, payload = HOOKS["funding_source"].build_create_payload(rec, ctx)
    assert paths == ["/v3/funding-source"]
    assert payload == {
        "name": "FS1",
        "description": "desc",
        "amount": "500",
        "start_datecode": "202401",
        "end_datecode": "202412",
        "ou_id": 100,
        "permission_scheme_id": 500,
        "owner_user_ids": [1],
        "owner_user_group_ids": [2],
    }


def test_funding_source_hook_falls_back_to_target_root_when_ou_unmapped():
    ctx = _fake_ctx(id_map={"ou": {}}, target_root_id=999)
    rec = {"name": "FS1", "amount": None, "__srcid__ou_id": None,
           "permission_scheme_name": None}
    paths, payload = HOOKS["funding_source"].build_create_payload(rec, ctx)
    assert payload["ou_id"] == 999
    assert payload["amount"] == "0"
    assert payload["description"] == ""


def test_funding_source_hook_skips_when_scheme_unresolved():
    ctx = _fake_ctx(id_map={"ou": {"10": 100}}, scheme_result=(None, "unresolved"))
    rec = {"name": "FS1", "__srcid__ou_id": 10, "permission_scheme_name": None}
    assert HOOKS["funding_source"].build_create_payload(rec, ctx) is None


def test_project_hook_builds_payload():
    ctx = _fake_ctx(id_map={"ou": {"10": 100}})
    rec = {
        "name": "P1", "description": "desc", "__srcid__ou_id": 10,
        "permission_scheme_name": None, "auto_pay": True,
        "default_aws_region": "us-east-1",
    }
    paths, payload = HOOKS["project"].build_create_payload(rec, ctx)
    assert paths == ["/v3/project", "/v3/project/with-budget"]
    assert payload == {
        "name": "P1",
        "description": "desc",
        "ou_id": 100,
        "permission_scheme_id": 500,
        "owner_user_ids": [1],
        "owner_user_group_ids": [2],
        "auto_pay": True,
        "default_aws_region": "us-east-1",
    }


def test_project_hook_omits_optional_fields_when_absent():
    ctx = _fake_ctx(id_map={"ou": {"10": 100}})
    rec = {"name": "P1", "__srcid__ou_id": 10, "permission_scheme_name": None,
           "auto_pay": None, "default_aws_region": None}
    paths, payload = HOOKS["project"].build_create_payload(rec, ctx)
    assert "auto_pay" not in payload
    assert "default_aws_region" not in payload


def test_project_hook_skips_when_ou_unresolvable():
    ctx = _fake_ctx(id_map={"ou": {}})
    rec = {"name": "P1", "__srcid__ou_id": 10, "permission_scheme_name": None}
    assert HOOKS["project"].build_create_payload(rec, ctx) is None


def test_project_hook_skips_when_scheme_unresolved():
    ctx = _fake_ctx(id_map={"ou": {"10": 100}}, scheme_result=(None, "unresolved"))
    rec = {"name": "P1", "__srcid__ou_id": 10, "permission_scheme_name": None}
    assert HOOKS["project"].build_create_payload(rec, ctx) is None
