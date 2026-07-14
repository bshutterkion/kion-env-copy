import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.reconcile import EngineReconciler
from kion.meta.load import Reference

# Minimal meta/refs/nkeys for a standalone 'billing_source'-like entity.
class M: pass
def _meta():
    m = M(); m.create_path="/v3/x"; m.create_method="POST"; m.read_path="/x/{id}"
    m.ignores=["status"]; m.archetype="entity"; m.name="thing"
    return {"thing": m}

def test_plan_creates_when_absent(monkeypatch):
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}          # nothing on target
    r._t_ids = {"thing": set()}
    result = r.run()
    assert r.counts["thing"]["create"] == 1

def test_plan_adopts_when_present():
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {("a",): 77}}
    r._t_ids = {"thing": {77}}
    r.run()
    assert r.counts["thing"]["adopt"] == 1
    assert r.id_map["thing"]["1"] == 77

def test_plan_skips_on_unresolved_required_ref():
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",),
                       "fields": {"name": "A", "payer_id": ("missing",)}}]}
    refs = {"thing": [Reference(field="payer_id", target="billing_source", key="name")]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs=refs,
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}, "billing_source": {}}
    r._t_ids = {"thing": set(), "billing_source": set()}
    r.run()
    assert r.skipped["thing"] == 1
    assert r.counts["thing"]["create"] == 0
    assert "1" not in r.id_map["thing"]

def test_plan_recreates_when_mapped_id_missing_from_target():
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False,
                         id_map={"thing": {"1": 999}})
    r._t_key = {"thing": {}}           # not adoptable by natural key
    r._t_ids = {"thing": set()}        # previously-mapped id 999 no longer present
    r.run()
    assert r.counts["thing"]["recreate"] == 1
    assert r.counts["thing"]["create"] == 0
