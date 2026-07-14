import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.overrides.registry import HOOKS

def test_account_hook_routes_to_cache_without_project():
    ctx = SimpleNamespace(id_map={"billing_sources": {"2": 900}, "projects": {}},
                          t_acct_by_number={})
    rec = {"provider": "custom", "account_number": "n", "account_name": "x",
           "payer_id": 2, "project_id": None, "__srcid__payer_id": 2,
           "__srcid__project_id": None}
    paths, payload = HOOKS["account"].build_create_payload(rec, ctx)
    assert paths[0].startswith("/v3/account-cache")
    assert "project_id" not in payload

def test_account_hook_identity_rejects_blank_number():
    assert HOOKS["account"].identity_ok({"account_number": ""}, None) is False
