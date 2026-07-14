import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import kion.engine.reconcile as reconcile_mod
from kion.engine.reconcile import EngineReconciler
from kion.meta.load import Reference
from kion.overrides.registry import Hooks

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

def test_hook_build_create_payload_used_when_present(monkeypatch):
    """When a hook is registered and build_create_payload returns
    (paths, payload), the reconciler creates via that payload (Finding 2)."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}
    r._t_ids = {"thing": set()}

    seen = {}
    def fake_build(fields, ctx):
        seen["fields"] = fields
        return (["/v3/hooked"], {"name": fields["name"], "extra": "hooked"})
    monkeypatch.setitem(reconcile_mod.HOOKS, "thing",
                         Hooks(build_create_payload=fake_build))

    r.run()
    assert r.counts["thing"]["create"] == 1
    assert r.skipped["thing"] == 0
    assert seen["fields"] == {"name": "A"}
    assert "1" in r.id_map["thing"]

def test_hook_build_create_payload_none_skips(monkeypatch):
    """When build_create_payload returns None, the record is skipped and no
    create happens (Finding 2)."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}
    r._t_ids = {"thing": set()}

    monkeypatch.setitem(reconcile_mod.HOOKS, "thing",
                         Hooks(build_create_payload=lambda fields, ctx: None))

    r.run()
    assert r.counts["thing"]["create"] == 0
    assert r.skipped["thing"] == 1
    assert "1" not in r.id_map["thing"]

def test_hook_identity_ok_false_skips(monkeypatch):
    """When identity_ok returns False, the record is skipped before
    build_create_payload is ever consulted (Finding 2)."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}
    r._t_ids = {"thing": set()}

    called = {"build": False}
    def fake_build(fields, ctx):
        called["build"] = True
        return (["/v3/hooked"], {})
    monkeypatch.setitem(reconcile_mod.HOOKS, "thing", Hooks(
        identity_ok=lambda fields, ctx: False,
        build_create_payload=fake_build,
    ))

    r.run()
    assert r.counts["thing"]["create"] == 0
    assert r.skipped["thing"] == 1
    assert called["build"] is False
    assert "1" not in r.id_map["thing"]

def test_index_target_only_scans_inventory_resources():
    """_index_target must only GET resources present in the inventory, not
    every resource in the full generator_config meta (Finding 1)."""
    class FakeClient:
        def __init__(self):
            self.calls = []
        def get(self, path):
            self.calls.append(path)
            return {"data": []}

    meta = _meta()
    irrelevant = M()
    irrelevant.create_path = "/v3/irrelevant"
    irrelevant.create_method = "POST"
    irrelevant.read_path = "/irrelevant/{id}"
    irrelevant.ignores = []
    irrelevant.archetype = "entity"
    irrelevant.name = "irrelevant"
    meta["irrelevant"] = irrelevant  # in self.meta, but NOT in inventory

    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    client = FakeClient()
    r = EngineReconciler(client=client, config=None, inventory=inv,
                         meta=meta, refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r.run()
    assert client.calls == ["/x"]  # only 'thing' was listed, never 'irrelevant'

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
