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


# -- OU hook (10c) ---------------------------------------------------------

def _rec(source_id, parent_ou_id, name):
    return {"source_id": source_id,
            "fields": {"name": name, "parent_ou_id": parent_ou_id},
            "natural_key": (name.lower(),)}


def test_ou_order_records_parents_before_children():
    """order_records reuses order_ous so a parent record precedes its children,
    even when the inventory lists them child-first."""
    child = _rec(2, 1, "Team")
    root = _rec(1, None, "Root")
    grand = _rec(3, 2, "Squad")
    ordered = HOOKS["ou"].order_records([grand, child, root], None)
    order = [r["source_id"] for r in ordered]
    assert order.index(1) < order.index(2) < order.index(3)


def test_ou_pre_reconcile_seeds_root_to_target_root_regardless_of_name():
    """The source root maps onto the target root by position (target_root_id),
    NOT by name — roots may be named differently across installs."""
    ctx = SimpleNamespace(id_map={"ou": {}}, target_root_id=500)
    records = [_rec(1, None, "SourceRootNamedDifferently"), _rec(2, 1, "Team")]
    HOOKS["ou"].pre_reconcile(records, ctx)
    assert ctx.id_map["ou"]["1"] == 500


def test_ou_pre_reconcile_no_seed_when_target_has_no_root():
    ctx = SimpleNamespace(id_map={"ou": {}}, target_root_id=None)
    HOOKS["ou"].pre_reconcile([_rec(1, None, "Root")], ctx)
    assert ctx.id_map["ou"] == {}


def test_ou_adopt_key_bridges_source_parent_via_id_map():
    """Adoption key = (target parent id, name); the source parent id is remapped
    through id_map so it matches the target index (which keys on target parent id)."""
    ctx = SimpleNamespace(id_map={"ou": {"1": 500}}, target_root_id=500)
    assert HOOKS["ou"].adopt_key({"name": "Team", "parent_ou_id": 1}, ctx) == (500, "team")


def test_ou_adopt_key_top_level_uses_target_root():
    ctx = SimpleNamespace(id_map={"ou": {}}, target_root_id=500)
    assert HOOKS["ou"].adopt_key({"name": "Team", "parent_ou_id": None}, ctx) == (500, "team")


def test_ou_adopt_key_none_when_parent_unresolved():
    ctx = SimpleNamespace(id_map={"ou": {}}, target_root_id=500)
    assert HOOKS["ou"].adopt_key({"name": "X", "parent_ou_id": 7}, ctx) is None


def test_ou_payload_uses_mapped_parent():
    ctx = _fake_ctx(id_map={"ou": {"1": 500}}, target_root_id=500)
    rec = {"name": "Team", "description": "d", "parent_ou_id": 1,
           "permission_scheme_name": None}
    paths, payload = HOOKS["ou"].build_create_payload(rec, ctx)
    assert paths == ["/v3/ou"]
    assert payload == {
        "name": "Team", "description": "d", "parent_ou_id": 500,
        "permission_scheme_id": 500, "owner_user_ids": [1],
        "owner_user_group_ids": [2],
    }


def test_ou_payload_top_level_hangs_off_target_root():
    ctx = _fake_ctx(id_map={"ou": {}}, target_root_id=777)
    rec = {"name": "Top", "parent_ou_id": None, "permission_scheme_name": None}
    _, payload = HOOKS["ou"].build_create_payload(rec, ctx)
    assert payload["parent_ou_id"] == 777
    assert payload["description"] == ""


def test_ou_payload_top_level_mints_when_target_rootless():
    ctx = _fake_ctx(id_map={"ou": {}}, target_root_id=None)
    rec = {"name": "Root", "parent_ou_id": None, "permission_scheme_name": None}
    _, payload = HOOKS["ou"].build_create_payload(rec, ctx)
    assert payload["parent_ou_id"] == 0


def test_ou_payload_skips_when_parent_unresolved():
    ctx = _fake_ctx(id_map={"ou": {}}, target_root_id=500)
    rec = {"name": "Orphan", "parent_ou_id": 42, "permission_scheme_name": None}
    assert HOOKS["ou"].build_create_payload(rec, ctx) is None


def test_ou_payload_skips_when_scheme_unresolved():
    ctx = _fake_ctx(id_map={"ou": {"1": 500}}, target_root_id=500,
                    scheme_result=(None, "unresolved"))
    rec = {"name": "Team", "parent_ou_id": 1, "permission_scheme_name": None}
    assert HOOKS["ou"].build_create_payload(rec, ctx) is None


def test_ou_post_create_anchors_first_top_level_when_rootless():
    ctx = SimpleNamespace(target_root_id=None)
    HOOKS["ou"].post_create({"name": "Root", "parent_ou_id": None}, 900, ctx)
    assert ctx.target_root_id == 900


def test_ou_post_create_noop_when_root_already_known():
    ctx = SimpleNamespace(target_root_id=500)
    HOOKS["ou"].post_create({"name": "Team", "parent_ou_id": 500}, 900, ctx)
    assert ctx.target_root_id == 500
