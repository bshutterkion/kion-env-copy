"""Task 10e — budget via a per-resource ``reconcile_override``.

budget does NOT fit the generic list+natural-key adoption model: its identity is
``(target scope, start_datecode, end_datecode)`` and target budgets are read PER
OU / PER project (``/v3/{ou,project}/{id}/budget``), not from one global list. So
budget gets a dedicated ``Hooks.reconcile_override`` that owns the whole reconcile
for the resource. These tests exercise that override with a stub client and an
injected id_map (no network), mirroring the Importer oracle (_reconcile_budgets).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kion.engine.reconcile as reconcile_mod
from kion.engine.reconcile import EngineReconciler
from kion.client import KionAPIError
from kion.overrides.registry import Hooks


class M:
    pass


def _budget_meta():
    m = M()
    m.create_path = "/v3/budget"
    m.create_method = "POST"
    m.read_path = None            # budget is never listed globally; override reads per-scope
    m.ignores = []
    m.archetype = "entity"
    m.name = "budget"
    return {"budget": m}


_BUDGET_NK = {"budget": {"kind": "date_range"}}


def _budget_rec(source_id, name, src_ou=None, src_proj=None,
                start="2024-01", end="2024-12", data=None):
    """An engine (Task 10f) budget inventory record: export-shaped ``fields`` with
    ``__srcid__ou_id``/``__srcid__project_id`` and raw source funding ids inside
    ``data[]``."""
    fields = {
        "name": name,
        "ou_id": None,
        "project_id": None,
        "start_datecode": start,
        "end_datecode": end,
        "data": data or [],
        "__srcid__ou_id": src_ou,
        "__srcid__project_id": src_proj,
    }
    return {"source_id": source_id, "natural_key": (start, end), "fields": fields}


class StubClient:
    """Returns canned target-scope budgets on GET and records POSTs."""
    def __init__(self, budgets_by_path=None, post_error=None):
        self.budgets_by_path = budgets_by_path or {}
        self.post_error = post_error
        self.posts = []
        self._n = 2000

    def get(self, path, params=None):
        return self.budgets_by_path.get(path, [])

    def post(self, path, json=None):
        self.posts.append((path, json))
        if self.post_error is not None:
            raise self.post_error
        self._n += 1
        return {"record_id": self._n}


def _reconciler(inv, client, id_map, apply=True):
    r = EngineReconciler(client=client, config=None, inventory=inv,
                         meta=_budget_meta(), refs={"budget": []},
                         nkeys=_BUDGET_NK, apply=apply, id_map=id_map)
    # bypass _index_target (network); the override reads target budgets itself.
    r._t_key = {"budget": {}}
    r._t_ids = {"budget": set()}
    return r


# -- dispatch mechanism ----------------------------------------------------

def test_reconcile_override_bypasses_generic_loop(monkeypatch):
    """A resource WITH a reconcile_override never enters the generic per-record
    loop: the override owns the reconcile and the generic adopt/create path is
    not taken (would otherwise ADOPT here)."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    m = M(); m.create_path = "/v3/x"; m.read_path = "/x/{id}"; m.ignores = []; m.name = "thing"
    r = EngineReconciler(client=None, config=None, inventory=inv, meta={"thing": m},
                         refs={"thing": []}, nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {("a",): 77}}    # generic path would ADOPT to id 77
    r._t_ids = {"thing": {77}}

    seen = {}
    def override(ctx, records):
        seen["ctx"] = ctx
        seen["records"] = records
    monkeypatch.setitem(reconcile_mod.HOOKS, "thing", Hooks(reconcile_override=override))

    r.run()
    assert seen["ctx"] is r
    assert seen["records"] == inv["thing"]
    # generic adopt path was bypassed
    assert r.counts["thing"]["adopt"] == 0
    assert "1" not in r.id_map["thing"]


def test_resource_without_override_unaffected():
    """A resource WITHOUT a reconcile_override still adopts via the generic path."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    m = M(); m.create_path = "/v3/x"; m.read_path = "/x/{id}"; m.ignores = []; m.name = "thing"
    r = EngineReconciler(client=None, config=None, inventory=inv, meta={"thing": m},
                         refs={"thing": []}, nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {("a",): 77}}
    r._t_ids = {"thing": {77}}
    r.run()
    assert r.counts["thing"]["adopt"] == 1
    assert r.id_map["thing"]["1"] == 77


# -- budget override behavior ---------------------------------------------

def test_budget_create_remaps_funding_scope_and_dates():
    data = [
        {"amount": "100", "datecode": "2024-01", "funding_source_id": 7, "priority": 0},
        {"amount": "50", "datecode": "2024-02", "funding_source_id": 7, "priority": 3},
    ]
    rec = _budget_rec(1, "B1", src_proj=30, data=data)
    client = StubClient()  # no existing target budgets
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {}, "project": {"30": 300},
                            "funding_source": {"7": 700}})
    r.run()

    assert len(client.posts) == 1
    path, payload = client.posts[0]
    assert path == "/v3/budget"
    assert payload["project_id"] == 300
    assert "ou_id" not in payload
    assert payload["start_datecode"] == "2024-01"
    assert payload["end_datecode"] == "2024-12"
    assert payload["funding_source_ids"] == [700]
    assert [row["funding_source_id"] for row in payload["data"]] == [700, 700]
    assert payload["data"][0]["priority"] == 0 and payload["data"][1]["priority"] == 3
    # id_map keyed by the Importer-identical state key
    assert r.id_map["budget"]["project:300:2024-01:2024-12"] == 2001
    assert r.counts["budget"]["create"] == 1


def test_budget_create_ou_scope_uses_ou_id():
    rec = _budget_rec(1, "B1", src_ou=10, data=[])
    client = StubClient()
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {"10": 100}, "project": {},
                            "funding_source": {}})
    r.run()
    # data has no funding rows, but a row with no funding is still usable
    # (no rows at all here -> skipped). Verify with a real row instead:
    assert r.skipped["budget"] == 1  # empty data -> no usable rows


def test_budget_create_ou_scope_with_unfunded_row():
    rec = _budget_rec(1, "B1", src_ou=10,
                      data=[{"amount": "10", "datecode": "2024-01",
                             "funding_source_id": None, "priority": 0}])
    client = StubClient()
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {"10": 100}, "project": {},
                            "funding_source": {}})
    r.run()
    assert len(client.posts) == 1
    _, payload = client.posts[0]
    assert payload["ou_id"] == 100
    assert "project_id" not in payload
    assert "funding_source_ids" not in payload  # no resolved fs ids
    assert "funding_source_id" not in payload["data"][0]
    assert r.id_map["budget"]["ou:100:2024-01:2024-12"] == 2001


def test_budget_adopts_matching_target_budget():
    rec = _budget_rec(1, "B1", src_ou=10, start="2024-01", end="2024-12")
    client = StubClient({"/v3/ou/100/budget": [
        {"config": {"id": 555, "start_datecode": "2024-01", "end_datecode": "2024-12"}}]})
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {"10": 100}, "project": {},
                            "funding_source": {}})
    r.run()
    assert client.posts == []
    assert r.counts["budget"]["adopt"] == 1
    assert r.id_map["budget"]["ou:100:2024-01:2024-12"] == 555


def test_budget_ok_when_state_key_present_and_still_on_target():
    rec = _budget_rec(1, "B1", src_ou=10, start="2024-01", end="2024-12")
    client = StubClient({"/v3/ou/100/budget": [
        {"config": {"id": 555, "start_datecode": "2024-01", "end_datecode": "2024-12"}}]})
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {"ou:100:2024-01:2024-12": 555},
                            "ou": {"10": 100}, "project": {}, "funding_source": {}})
    r.run()
    assert client.posts == []
    assert r.counts["budget"]["ok"] == 1
    assert r.counts["budget"]["adopt"] == 0


def test_budget_skip_unresolved_scope():
    rec = _budget_rec(1, "B1", src_ou=10)  # ou 10 not in id_map
    client = StubClient()
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {}, "project": {}, "funding_source": {}})
    r.run()
    assert client.posts == []
    assert r.skipped["budget"] == 1
    assert any("target OU/project unresolved" in w for w in r.warnings)


def test_budget_skip_when_all_funding_rows_unresolved():
    rec = _budget_rec(1, "B1", src_proj=30,
                      data=[{"amount": "100", "datecode": "2024-01",
                             "funding_source_id": 7, "priority": 0}])
    client = StubClient()
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {}, "project": {"30": 300},
                            "funding_source": {}})  # fs 7 unresolved
    r.run()
    assert client.posts == []
    assert r.skipped["budget"] == 1
    assert any("no usable rows" in w for w in r.warnings)
    assert any("funding source 7 unresolved" in w for w in r.warnings)


def test_budget_recreate_when_state_key_present_but_gone_from_target():
    rec = _budget_rec(1, "B1", src_ou=10,
                      data=[{"amount": "10", "datecode": "2024-01",
                             "funding_source_id": None, "priority": 0}])
    client = StubClient()  # target has NO budgets now
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {"ou:100:2024-01:2024-12": 999},
                            "ou": {"10": 100}, "project": {}, "funding_source": {}})
    r.run()
    assert len(client.posts) == 1
    assert r.counts["budget"]["recreate"] == 1
    assert r.id_map["budget"]["ou:100:2024-01:2024-12"] == 2001


def test_budget_diagnose_insufficient_funds():
    """On an 'insufficient funds' create failure, an over-subscribed funding
    source (allocated across >1 budget beyond its amount) is named in a cause
    warning — ported from Importer._diagnose_budget_failure/_funding_oversubscription."""
    data = [{"amount": "100", "datecode": "2024-01", "funding_source_id": 7, "priority": 0}]
    b1 = _budget_rec(1, "B1", src_proj=30, data=data)
    b2 = _budget_rec(2, "B2", src_proj=31, data=data)
    fs_inv = [{"source_id": 7, "natural_key": ("fs",),
               "fields": {"name": "FS", "amount": "100"}}]
    err = KionAPIError(400, "POST", "/v3/budget", "insufficient funds available on funding source")
    client = StubClient(post_error=err)
    r = _reconciler({"budget": [b1, b2]}, client,
                    id_map={"budget": {}, "ou": {},
                            "project": {"30": 300, "31": 310},
                            "funding_source": {"7": 700}})
    r.inventory["funding_source"] = fs_inv  # visible to _funding_oversubscription
    reconcile_mod.HOOKS["budget"].reconcile_override(r, [b1, b2])
    assert r.failed["budget"] == 2
    assert any("over-subscribed funding source" in w and "'FS'" in w for w in r.warnings)


def test_budget_diagnose_timeframe_not_covered():
    rec = _budget_rec(1, "B1", src_ou=10, start="2024-01", end="2024-03",
                      data=[{"amount": "10", "datecode": "2024-01",
                             "funding_source_id": None, "priority": 0}])  # missing 2024-02
    err = KionAPIError(400, "POST", "/v3/budget", "budget timeframe not fully covered")
    client = StubClient(post_error=err)
    r = _reconciler({"budget": [rec]}, client,
                    id_map={"budget": {}, "ou": {"10": 100}, "project": {},
                            "funding_source": {}})
    r.run()
    assert r.failed["budget"] == 1
    assert any("2024-02" in w and "cause" in w for w in r.warnings)
