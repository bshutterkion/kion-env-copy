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


def test_inventory_orders_self_ref_ous_parent_first():
    """A self-referential hierarchy (OU) whose LIST endpoint returns a child
    before its parent must still get a parent-resolved name_in_parent key: the
    parent's key has to be computed first, so build_inventory processes OU records
    parent-first regardless of API order."""
    from kion.meta.load import ResourceMeta
    m = {"ou": ResourceMeta("ou", read_path="/v3/ou", read_method="GET")}
    # child listed BEFORE its parent — the ordering hazard.
    client = StubClient({"/v3/ou": [
        {"id": 2, "name": "Team", "parent_ou_id": 1},
        {"id": 1, "name": "Root", "parent_ou_id": None},
    ]})
    refs = {"ou": []}
    nk = {"ou": {"kind": "name_in_parent", "parent_field": "parent_ou_id"}}
    inv = build_inventory(client, m, refs, nk, ["ou"])
    by_src = {r["source_id"]: r for r in inv["ou"]}
    assert by_src[1]["natural_key"] == ("root",)
    # parent-resolved chain, NOT the raw (1, 'team') you'd get if the child were
    # keyed before its parent was seen.
    assert by_src[2]["natural_key"] == ("root", "team")


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


def test_inventory_reads_billing_sources_via_export_shaping():
    """billing_source records come from export._export_billing_sources (not the
    generic raw list read) -- export shape (type/name/config), natural_key
    (nkey(name),), no references."""
    from kion.meta.load import ResourceMeta
    m = {"billing_source": ResourceMeta("billing_source", read_path="/v4/billing-source",
                                        read_method="GET")}
    client = StubClient({
        "/v4/billing-source": [
            {"id": 7, "aws_payer": {"name": "Payer One"}, "account_creation": True,
             "use_focus_reports": False, "use_proprietary_reports": True},
        ],
    })
    refs = {"billing_source": []}
    nk = {"billing_source": {"kind": "name"}}
    inv = build_inventory(client, m, refs, nk, ["billing_source"])
    recs = inv["billing_source"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["source_id"] == 7
    assert rec["natural_key"] == ("payer one",)
    assert rec["fields"]["type"] == "aws"
    assert rec["fields"]["name"] == "Payer One"
    assert rec["fields"]["config"] == {"name": "Payer One"}


def test_inventory_reads_budgets_via_export_shaping():
    """budget records come from export._export_budgets(client, ous, projects) --
    build_inventory supplies the source OUs/projects itself. fields['data']
    (funding_source_ids) survives untouched, and __srcid__ou_id/__srcid__project_id
    are retained for the 10e budget hook."""
    from kion.meta.load import ResourceMeta
    m = {"ou": ResourceMeta("ou", read_path="/v3/ou", read_method="GET"),
         "project": ResourceMeta("project", read_path="/v3/project", read_method="GET"),
         "budget": ResourceMeta("budget", read_path="/v3/budget/{id}", read_method="GET")}
    client = StubClient({
        "/v3/ou": [{"id": 9, "name": "Root", "parent_ou_id": None}],
        "/v3/project": [{"id": 5, "name": "App", "ou_id": 9}],
        "/v3/ou/9/budget": [
            {"config": {"id": 100, "name": "FY24", "start_datecode": "202401",
                        "end_datecode": "202412"},
             "data": [{"amount": "1000", "datecode": "202401",
                       "funding_source_id": 55, "priority": 1}]},
        ],
        "/v3/project/5/budget": [],
    })
    refs = {"ou": [], "project": [Reference("ou_id", "ou", "name")],
            "budget": [Reference("ou_id", "ou", "name", optional=True),
                      Reference("project_id", "project", "name", optional=True),
                      Reference("funding_source_id", "funding_source", "name", many=True)]}
    nk = {"ou": {"kind": "name_in_parent", "parent_field": "parent_ou_id"},
          "project": {"kind": "name_in_parent", "parent_field": "ou_id"},
          "budget": {"kind": "date_range"}}
    inv = build_inventory(client, m, refs, nk, ["ou", "project", "budget"])
    budgets = inv["budget"]
    assert len(budgets) == 1
    rec = budgets[0]
    assert rec["natural_key"] == ("202401", "202412")
    assert rec["fields"]["data"] == [{"amount": "1000", "datecode": "202401",
                                      "funding_source_id": 55, "priority": 1}]
    assert rec["fields"]["__srcid__ou_id"] == 9
    assert rec["fields"]["ou_id"] == ("root",)   # translated via to_natural
    assert rec["fields"]["__srcid__project_id"] is None
    assert rec["fields"]["project_id"] is None


def test_inventory_reads_scopes_via_export_shaping():
    """scope records come from export._export_scopes -- account_numbers/criteria
    are export-shaped and pass through untouched (already natural), and
    __srcid__project_id is retained since project_id is still a raw source id."""
    from kion.meta.load import ResourceMeta
    m = {"project": ResourceMeta("project", read_path="/v3/project", read_method="GET"),
         "scope": ResourceMeta("scope", read_path="/beta/scope", read_method="GET")}
    client = StubClient({
        "/v3/project": [{"id": 5, "name": "App", "ou_id": 9}],
        "/v3/account": [{"id": 1, "account_number": "111"}],
        "/beta/scope": [
            {"id": 42, "name": "My Scope", "alias": "ms", "description": "",
             "project_id": 5, "start_datecode": "202401", "end_datecode": None,
             "active_criteria_record": {
                 "criteria": {"account_criteria": {"account_ids": [1]}}}},
        ],
    })
    refs = {"project": [Reference("ou_id", "ou", "name")],
            "scope": [Reference("project_id", "project", "name"),
                     Reference("account_numbers", "account", "account_number", many=True)]}
    nk = {"project": {"kind": "name_in_parent", "parent_field": "ou_id"},
          "scope": {"kind": "name_in_parent", "parent_field": "project_id"}}
    inv = build_inventory(client, m, refs, nk, ["project", "scope"])
    scopes = inv["scope"]
    assert len(scopes) == 1
    rec = scopes[0]
    assert rec["fields"]["account_numbers"] == ["111"]
    assert rec["fields"]["criteria"] == {"account_criteria": {}}
    assert rec["fields"]["__srcid__project_id"] == 5
    assert rec["fields"]["project_id"] == ("app",)   # translated via to_natural
