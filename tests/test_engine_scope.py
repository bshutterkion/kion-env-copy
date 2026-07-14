"""Task 10g — scope via a per-resource ``reconcile_override``.

scope does NOT fit the generic list+natural-key create path: adoption is
``(target_project, name)``, and its create remaps account *numbers* to target
account ids (``criteria.account_criteria.account_ids``) with a >=1-existing-
account requirement and an "Invalid scope criteria" failure diagnostic. So scope
gets a dedicated ``Hooks.reconcile_override`` that owns the whole reconcile,
mirroring the Importer oracle (``_reconcile_scopes``). These tests exercise the
override with a stub client + injected id_map/_t_key/_t_ids/t_acct_by_number (no
network).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kion.engine.reconcile as reconcile_mod
from kion.engine.reconcile import EngineReconciler
from kion.client import KionAPIError
from kion.import_ import nkey


class M:
    pass


def _scope_meta():
    m = M()
    m.create_path = "/beta/scope"
    m.create_method = "POST"
    m.read_path = "/beta/scope/{id}"
    m.ignores = []
    m.archetype = "entity"
    m.name = "scope"
    return {"scope": m}


_SCOPE_NK = {"scope": {"kind": "name_in_parent", "parent_field": "project_id"}}


def _scope_rec(source_id, name, src_proj, account_numbers=None, criteria=None,
               alias="a", description="d", start="2024-01", end="2024-12"):
    """An engine (Task 10f) scope inventory record: the source scope id lives in
    ``rec['source_id']`` (popped from fields by _finish_export_record); fields is
    export-shaped with ``__srcid__project_id`` + account-number strings."""
    fields = {
        "name": name,
        "alias": alias,
        "description": description,
        "project_id": ["pkey"],           # natural key (unused by the override)
        "start_datecode": start,
        "end_datecode": end,
        "account_numbers": account_numbers or [],
        "criteria": criteria if criteria is not None else {"account_criteria": {}},
        "__srcid__project_id": src_proj,
    }
    return {"source_id": source_id, "natural_key": ("pkey", nkey(name)),
            "fields": fields}


class StubClient:
    def __init__(self, post_error=None):
        self.post_error = post_error
        self.posts = []
        self._n = 5000

    def get(self, path, params=None):
        return []

    def post(self, path, json=None):
        self.posts.append((path, json))
        if self.post_error is not None:
            raise self.post_error
        self._n += 1
        return {"record_id": self._n}


def _reconciler(inv, client, id_map, t_key=None, t_ids=None,
                t_acct_by_number=None, apply=True):
    r = EngineReconciler(client=client, config=None, inventory=inv,
                         meta=_scope_meta(), refs={"scope": []},
                         nkeys=_SCOPE_NK, apply=apply, id_map=id_map)
    # bypass _index_target (network); inject the target scope index by hand.
    r._t_key = {"scope": t_key or {}}
    r._t_ids = {"scope": t_ids or set()}
    r.t_acct_by_number = t_acct_by_number or {}
    return r


# -- registration / dispatch ----------------------------------------------

def test_scope_override_registered():
    from kion.overrides.registry import HOOKS
    assert HOOKS["scope"].reconcile_override is not None


def test_scope_dispatch_bypasses_generic_loop():
    """scope with a reconcile_override never enters the generic per-record loop:
    the generic path would ADOPT on the name natural key, but the override owns
    the reconcile and adopts only on (proj_new, name)."""
    rec = _scope_rec(1, "S1", src_proj=30)
    inv = {"scope": [rec]}
    client = StubClient()
    # generic loop, if reached, would adopt on the record's natural_key
    r = _reconciler(inv, client,
                    id_map={"scope": {}, "project": {}},   # project unresolved
                    t_key={("pkey", nkey("S1")): 999},     # generic would adopt -> 999
                    t_ids={999})
    r.run()
    # override skips (project unresolved) instead of generic-adopting to 999
    assert r.counts["scope"]["adopt"] == 0
    assert "1" not in r.id_map["scope"]
    assert r.skipped["scope"] == 1


# -- adopt / ok ------------------------------------------------------------

def test_scope_adopts_by_project_and_name():
    rec = _scope_rec(1, "S1", src_proj=30)
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_key={(300, nkey("S1")): 777}, t_ids={777})
    r.run()
    assert client.posts == []
    assert r.counts["scope"]["adopt"] == 1
    assert r.id_map["scope"]["1"] == 777


def test_scope_ok_when_mapped_and_present():
    rec = _scope_rec(1, "S1", src_proj=30)
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {"1": 777}, "project": {"30": 300}},
                    t_key={(300, nkey("S1")): 777}, t_ids={777})
    r.run()
    assert client.posts == []
    assert r.counts["scope"]["ok"] == 1
    assert r.counts["scope"]["adopt"] == 0


# -- create ----------------------------------------------------------------

def test_scope_create_remaps_accounts_and_criteria():
    crit = {"account_criteria": {"type": "account_ids"},
            "region_criteria": {"regions": ["us-east-1"]}}
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111", "222"],
                     criteria=crit)
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_acct_by_number={"111": 700, "222": 800})
    r.run()
    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/beta/scope"
    assert payload["project_id"] == 300
    assert payload["name"] == "S1"
    assert payload["alias"] == "a"
    assert payload["start_datecode"] == "2024-01"
    assert payload["end_datecode"] == "2024-12"
    ac = payload["criteria"]["account_criteria"]
    assert ac["type"] == "account_ids"
    assert ac["account_ids"] == [700, 800]
    # untouched criteria carried through
    assert payload["criteria"]["region_criteria"] == {"regions": ["us-east-1"]}
    assert r.id_map["scope"]["1"] == 5001
    assert r.counts["scope"]["create"] == 1


def test_scope_create_defaults_account_criteria_type():
    """No pre-existing account_criteria -> type defaults to 'account_ids'."""
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111"], criteria={})
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_acct_by_number={"111": 700})
    r.run()
    _, payload = client.posts[0]
    ac = payload["criteria"]["account_criteria"]
    assert ac["type"] == "account_ids"
    assert ac["account_ids"] == [700]


def test_scope_create_drops_missing_accounts_but_keeps_resolved():
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111", "999"])
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_acct_by_number={"111": 700})  # 999 missing
    r.run()
    _, payload = client.posts[0]
    assert payload["criteria"]["account_criteria"]["account_ids"] == [700]
    assert any("account 999 not on target" in w for w in r.warnings)


def test_scope_recreate_when_state_key_present_but_gone():
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111"])
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {"1": 404}, "project": {"30": 300}},
                    t_ids=set(),  # mapped 404 no longer on target
                    t_acct_by_number={"111": 700})
    r.run()
    assert len(client.posts) == 1
    assert r.counts["scope"]["recreate"] == 1
    assert r.id_map["scope"]["1"] == 5001


# -- skip ------------------------------------------------------------------

def test_scope_skip_unresolved_project():
    rec = _scope_rec(1, "S1", src_proj=30)
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {}})  # 30 unresolved
    r.run()
    assert client.posts == []
    assert r.skipped["scope"] == 1
    assert any("project 30 unresolved" in w for w in r.warnings)


def test_scope_skip_when_no_accounts_on_target():
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111", "222"])
    client = StubClient()
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_acct_by_number={})  # none resolvable
    r.run()
    assert client.posts == []
    assert r.skipped["scope"] == 1
    assert any("none of its 2 account(s) exist on target" in w for w in r.warnings)


# -- create-failure diagnostic --------------------------------------------

def test_scope_criteria_failure_diagnostic():
    rec = _scope_rec(1, "S1", src_proj=30, account_numbers=["111"])
    err = KionAPIError(400, "POST", "/beta/scope", "Invalid scope criteria: bad tag")
    client = StubClient(post_error=err)
    r = _reconciler({"scope": [rec]}, client,
                    id_map={"scope": {}, "project": {"30": 300}},
                    t_acct_by_number={"111": 700})
    r.run()
    assert r.failed["scope"] == 1
    assert "1" not in r.id_map["scope"]
    assert any("tag key / region / service" in w and "cause" in w for w in r.warnings)
