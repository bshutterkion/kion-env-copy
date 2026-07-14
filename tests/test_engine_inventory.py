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


def test_inventory_reads_accounts_union_with_namespacing():
    """account records come from the UNION of /v3/account (associated) and
    /v3/account-cache (cached); cached ids are namespaced 'cache:<id>', natural
    key is (account_number,), provider is derived, and the payer ref keeps its
    source id in __srcid__payer_id for the account hook (concern B)."""
    from kion.meta.load import ResourceMeta
    m = {"account": ResourceMeta("account", read_path="/v3/account", read_method="GET")}
    refs = {"account": [Reference("payer_id", "billing_source", "name"),
                        Reference("project_id", "project", "name", optional=True)]}
    nk = {"account": {"kind": "account_number"}}
    client = StubClient({
        "/v3/account": [{"id": 1, "account_number": "111", "account_name": "assoc",
                         "account_type_id": 1, "payer_id": 50, "project_id": 9}],
        "/v3/account-cache": [{"id": 2, "account_number": "222", "account_name": "cached",
                               "account_type_id": 15, "payer_id": 51}],
    })
    inv = build_inventory(client, m, refs, nk, ["account"])
    accts = inv["account"]
    assert len(accts) == 2
    by_src = {a["source_id"]: a for a in accts}
    assert 1 in by_src and "cache:2" in by_src
    assert by_src[1]["natural_key"] == ("111",)
    assert by_src["cache:2"]["natural_key"] == ("222",)
    assert by_src[1]["fields"]["provider"] == "aws"
    assert by_src["cache:2"]["fields"]["provider"] == "google-cloud"
    # payer ref keeps source id for the 10c account hook
    assert by_src[1]["fields"]["__srcid__payer_id"] == 50
    # cached account carries no project and is flagged
    assert by_src["cache:2"]["fields"]["cached"] is True
    assert by_src["cache:2"]["fields"]["project_id"] is None
    assert by_src["cache:2"]["fields"]["__srcid__project_id"] is None
