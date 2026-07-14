import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.inventory import build_inventory
from kion.meta.load import Reference

class StubClient:
    def __init__(self, data): self.data = data
    def get(self, path, params=None): return self.data.get(path, [])

def test_inventory_translates_refs_to_keys():
    # two resources: ou (name key), project (ref ou by name)
    from kion.meta.load import ResourceMeta
    m = {"ou": ResourceMeta("ou", read_path="/v3/ou", read_method="GET", ignores=["status"]),
         "project": ResourceMeta("project", read_path="/v3/project", read_method="GET", ignores=["status"])}
    client = StubClient({"/v3/ou": [{"id": 9, "name": "Root", "parent_ou_id": None}],
                         "/v3/project": [{"id": 5, "name": "App", "ou_id": 9}]})
    refs = {"ou": [], "project": [Reference("ou_id", "ou", "name")]}
    nk = {"ou": {"kind": "name_in_parent", "parent_field": "parent_ou_id"},
          "project": {"kind": "name_in_parent", "parent_field": "ou_id"}}
    inv = build_inventory(client, m, refs, nk, ["project", "ou"])
    proj = inv["project"][0]
    assert proj["fields"]["ou_id"] == ("root",)   # id 9 -> ou natural key
