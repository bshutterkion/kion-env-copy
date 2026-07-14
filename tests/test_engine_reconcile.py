import os, sys
from types import SimpleNamespace
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

def test_index_target_enriches_ctx():
    """_index_target must populate schemes/users/groups/target_root_id/
    current_user_id, and the resolve_scheme/resolve_owners ctx methods (incl. the
    current-user owner fallback) must mirror Importer (concern A)."""
    class Client:
        DATA = {
            "/v3/permission-scheme": [
                {"name": "Default OU Permissions Scheme", "id": 10},
                {"name": "Custom", "id": 11},
            ],
            "/v3/user": [{"email": "A@x.com", "id": 5}, {"id": 6}],  # 2nd has no email
            "/v3/user-group": [{"name": "admins", "id": 7}],
            "/v3/ou": [{"id": 100, "parent_ou_id": None},
                       {"id": 101, "parent_ou_id": 100}],
            "/v3/app-api-key": [{"user_id": 42}],
        }

        def get(self, path, params=None):
            return self.DATA.get(path, [])

    cfg = SimpleNamespace(default_permission_scheme_id=99)
    r = EngineReconciler(client=Client(), config=cfg, inventory={}, meta={},
                         refs={}, nkeys={}, apply=False)
    r._index_target()
    assert r.schemes == {"Default OU Permissions Scheme": 10, "Custom": 11}
    assert r.users == {"a@x.com": 5}
    assert r.groups == {"admins": 7}
    assert r.target_root_id == 100
    assert r.current_user_id == 42

    assert r.resolve_scheme("Custom", "ou", "lbl") == (11, "matched")
    assert r.resolve_scheme("nope", "ou", "lbl") == (10, "type_default")
    # 'project' type default not on target -> DEFAULT_PERMISSION_SCHEME_ID
    assert r.resolve_scheme("nope", "project", "lbl") == (99, "default")

    uids, gids = r.resolve_owners(
        {"owner_user_emails": ["A@x.com"], "owner_user_group_names": ["admins"]}, "lbl")
    assert uids == [5] and gids == [7]
    # empty owners -> current-user fallback
    uids2, gids2 = r.resolve_owners({}, "lbl")
    assert uids2 == [42] and gids2 == []
    assert r._owner_fallback == 1


def test_index_target_skips_ctx_reads_when_config_none():
    """With config=None (some unit tests), _index_target must not attempt the
    ctx enrichment reads (guarded), only the inventory-resource list reads."""
    class M2: pass
    m = M2(); m.read_path = "/x/{id}"; m.ignores = []; m.name = "thing"
    calls = []

    class Client:
        def get(self, path):
            calls.append(path)
            return {"data": []}

    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=Client(), config=None, inventory=inv,
                         meta={"thing": m}, refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._index_target()
    assert calls == ["/x"]  # no /v3/permission-scheme etc.


def test_index_target_billing_source_indexes_by_flattened_name():
    """A RAW /v4/billing-source record has NO top-level 'name' -- it's nested
    under aws_payer/gcp_payer/azure_payer/oci_payer/custom_billing_source/
    anthropic_billing_source (GCP nests further under
    gcp_billing_account.name). Indexing target billing sources via the
    generic list_records + natural_key path (as every other resource is)
    would compute every one's key as ("",), so nothing ever adopts by name
    and an already-present target billing source gets wrongly planned as
    create. _index_target must special-case billing_source like it already
    does account: build the target index from EXPORT-SHAPED records (the
    same export._export_billing_sources reader the inventory uses for the
    SOURCE side) so the natural key computes on the real, flattened name --
    symmetric with the source read."""
    from kion.import_ import nkey

    class Client:
        def get(self, path, params=None):
            if path == "/v4/billing-source":
                return {"items": [
                    {"id": 500, "custom_billing_source": {"name": "Acme Billing"}},
                    {"id": 501, "aws_payer": {"name": "AWS Payer"}},
                ]}
            return []

    m = M(); m.read_path = "/v4/billing-source"; m.ignores = []
    m.archetype = "entity"; m.name = "billing_source"
    r = EngineReconciler(client=Client(), config=None,
                         inventory={"billing_source": []},
                         meta={"billing_source": m}, refs={"billing_source": []},
                         nkeys={"billing_source": {"kind": "name"}}, apply=False)
    r._index_target()
    assert r._t_key["billing_source"] == {
        (nkey("Acme Billing"),): 500,
        (nkey("AWS Payer"),): 501,
    }
    assert r._t_ids["billing_source"] == {500, 501}
    # the bug this guards against: a raw-record index would have collapsed
    # both into a single ("",) key instead of the two flattened-name keys.
    assert ("",) not in r._t_key["billing_source"]


def test_index_target_warns_on_target_list_failure():
    """A failing target-side list read must append a 'list failed' warning
    (restoring pre-Task-10a visibility) rather than silently indexing nothing
    for that resource, which would make every record plan as create instead
    of adopt with no diagnostic (the finding this test guards against)."""
    from kion.client import KionAPIError

    class FailingClient:
        def get(self, path, params=None):
            raise KionAPIError(503, "GET", path, "unavailable")

    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=FailingClient(), config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._index_target()
    assert r._t_key["thing"] == {}
    assert r._t_ids["thing"] == set()
    assert any("target thing list failed: 503" in w for w in r.warnings)


# -- OU hierarchy / root support (10c) ------------------------------------

def _ou_meta():
    m = M(); m.create_path = "/v3/ou"; m.create_method = "POST"
    m.read_path = "/v3/ou/{id}"; m.ignores = []; m.archetype = "entity"; m.name = "ou"
    return {"ou": m}

_OU_NK = {"ou": {"kind": "name_in_parent", "parent_field": "parent_ou_id"}}


def _ou_rec(source_id, parent_ou_id, name, key):
    return {"source_id": source_id, "natural_key": key,
            "fields": {"name": name, "parent_ou_id": parent_ou_id,
                       "permission_scheme_name": None}}


class _RecordingClient:
    """Captures POSTs and hands back synthetic ids so apply-mode tests can
    inspect the create payloads the engine built."""
    def __init__(self):
        self.posts = []
        self._n = 1000

    def post(self, path, json=None):
        self.posts.append((path, json))
        self._n += 1
        return {"record_id": self._n}


def _ou_reconciler(inv, apply=False, id_map=None):
    return EngineReconciler(
        client=None, config=None, inventory=inv, meta=_ou_meta(),
        refs={"ou": []}, nkeys=_OU_NK, apply=apply, id_map=id_map)


def test_ou_root_maps_to_target_root_no_create_regardless_of_name():
    """The source root maps onto the target root (by position, not name) with no
    create and no adopt, and the mapping is recorded in id_map."""
    inv = {"ou": [_ou_rec(1, None, "SourceRootDifferentName", ("sourcerootdifferentname",))]}
    r = _ou_reconciler(inv)
    r._t_key = {"ou": {(None, "tgtroot"): 500}}   # target root named differently
    r._t_ids = {"ou": {500}}
    r.target_root_id = 500
    r.run()
    assert r.id_map["ou"]["1"] == 500
    assert r.counts["ou"]["create"] == 0
    assert r.counts["ou"]["adopt"] == 0


def test_ou_child_payload_parent_is_id_map_target_of_source_parent():
    """A child's create payload carries parent_ou_id = the target id its source
    parent mapped to (pulled parent-first, bridged through id_map)."""
    inv = {"ou": [
        _ou_rec(1, None, "SrcRoot", ("srcroot",)),
        _ou_rec(2, 1, "Team", ("srcroot", "team")),
    ]}
    r = _ou_reconciler(inv, apply=True)
    client = _RecordingClient()
    r.client = client
    r._t_key = {"ou": {}}
    r._t_ids = {"ou": {500}}
    r.target_root_id = 500
    r.schemes = {"Default OU Permissions Scheme": 10}   # type-default resolves
    r.current_user_id = 42
    r.run()
    assert len(client.posts) == 1                        # root not created
    path, payload = client.posts[0]
    assert path == "/v3/ou"
    assert payload["name"] == "Team"
    assert payload["parent_ou_id"] == 500               # id_map["ou"]["1"]
    assert payload["permission_scheme_id"] == 10
    assert payload["owner_user_ids"] == [42]            # current-user fallback
    assert r.id_map["ou"]["2"] == 1001


def test_ou_reconciles_parents_before_children_when_inventory_out_of_order():
    """Given OUs child-first in the inventory, the engine still reconciles the
    parent first, so no child is skipped as 'parent unresolved'."""
    inv = {"ou": [
        _ou_rec(3, 2, "Squad", ("srcroot", "team", "squad")),
        _ou_rec(2, 1, "Team", ("srcroot", "team")),
        _ou_rec(1, None, "SrcRoot", ("srcroot",)),
    ]}
    r = _ou_reconciler(inv, apply=True)
    client = _RecordingClient()
    r.client = client
    r._t_key = {"ou": {}}
    r._t_ids = {"ou": {500}}
    r.target_root_id = 500
    r.schemes = {"Default OU Permissions Scheme": 10}
    r.current_user_id = 42
    r.run()
    assert r.skipped["ou"] == 0
    assert r.id_map["ou"]["2"] and r.id_map["ou"]["3"]
    # Squad's parent must resolve to Team's freshly-created id (not skipped).
    payloads = {p["name"]: p for _, p in client.posts}
    assert payloads["Team"]["parent_ou_id"] == 500
    assert payloads["Squad"]["parent_ou_id"] == r.id_map["ou"]["2"]
    assert not any("unresolved" in w for w in r.warnings)


def test_ou_child_skipped_when_scheme_unresolved():
    inv = {"ou": [
        _ou_rec(1, None, "SrcRoot", ("srcroot",)),
        _ou_rec(2, 1, "Team", ("srcroot", "team")),
    ]}
    r = _ou_reconciler(inv, apply=True)
    r.client = _RecordingClient()
    r._t_key = {"ou": {}}
    r._t_ids = {"ou": {500}}
    r.target_root_id = 500
    r.schemes = {}                # nothing resolves, config is None -> unresolved
    r.run()
    assert r.skipped["ou"] == 1
    assert r.counts["ou"]["create"] == 0


def test_ou_rootless_target_creates_root_top_level_and_anchors():
    """With no target root, the source root is minted top-level (parent_ou_id=0)
    and becomes target_root_id for the rest of the run."""
    inv = {"ou": [
        _ou_rec(1, None, "SrcRoot", ("srcroot",)),
        _ou_rec(2, 1, "Team", ("srcroot", "team")),
    ]}
    r = _ou_reconciler(inv, apply=True)
    client = _RecordingClient()
    r.client = client
    r._t_key = {"ou": {}}
    r._t_ids = {"ou": set()}
    r.target_root_id = None
    r.schemes = {"Default OU Permissions Scheme": 10}
    r.current_user_id = 42
    r.run()
    payloads = {p["name"]: p for _, p in client.posts}
    assert payloads["SrcRoot"]["parent_ou_id"] == 0
    assert r.target_root_id == r.id_map["ou"]["1"]      # anchored to created root
    assert payloads["Team"]["parent_ou_id"] == r.id_map["ou"]["1"]


def test_ou_adopts_existing_child_by_target_parent_and_name():
    """A child that already exists on the target (same target-parent + name) is
    adopted, not duplicated — even though its inventory natural key is a name
    chain, adoption keys on (target parent id, name)."""
    inv = {"ou": [
        _ou_rec(1, None, "SrcRoot", ("srcroot",)),
        _ou_rec(2, 1, "Team", ("srcroot", "team")),
    ]}
    r = _ou_reconciler(inv)
    r._t_key = {"ou": {(500, "team"): 600}}   # existing child under target root 500
    r._t_ids = {"ou": {500, 600}}
    r.target_root_id = 500
    r.run()
    assert r.id_map["ou"]["2"] == 600
    assert r.counts["ou"]["adopt"] == 1
    assert r.counts["ou"]["create"] == 0


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


def test_t_acct_ids_initialized_empty_set():
    """(10d) t_acct_by_number/t_acct_ids exist as safe empties so the account
    hook's post_create never AttributeErrors before _index_target runs."""
    r = EngineReconciler(client=None, config=None, inventory={},
                         meta={}, refs={}, nkeys={}, apply=False)
    assert r.t_acct_by_number == {}
    assert r.t_acct_ids == set()


def test_index_target_prepopulates_t_acct_from_existing_accounts():
    """(10f) t_acct_by_number/t_acct_ids must be populated from the target's
    EXISTING accounts (associated + cached) during _index_target, not just from
    ones this run creates -- so the scope pass can resolve accounts adopted
    here. Associated wins over a same-numbered cache entry (mirrors
    Importer._index_target)."""
    class Client:
        DATA = {
            "/v3/account": [{"id": 1, "account_number": "111"},
                            {"id": 2, "account_number": "222"}],
            "/v3/account-cache": [{"id": 3, "account_number": "333"},
                                  {"id": 4, "account_number": "222"}],  # dup -> assoc wins
        }

        def get(self, path, params=None):
            return self.DATA.get(path, [])

    m = M(); m.read_path = "/v3/account/{id}"; m.ignores = []; m.name = "account"
    inv = {"account": [{"source_id": 1, "natural_key": ("111",), "fields": {}}]}
    r = EngineReconciler(client=Client(), config=None, inventory=inv,
                         meta={"account": m}, refs={"account": []},
                         nkeys={"account": {"kind": "account_number"}}, apply=False)
    r._index_target()
    assert r.t_acct_by_number == {"111": 1, "222": 2, "333": 3}
    assert r.t_acct_ids == {1, 2, 3, 4}
    # the normal target index is unaffected -- both buckets are still adoptable
    # (last-wins on the number-keyed map for a duplicate number, unlike
    # t_acct_by_number's associated-wins setdefault above)
    assert r._t_key["account"] == {("111",): 1, ("222",): 4, ("333",): 3}
    assert r._t_ids["account"] == {1, 2, 3, 4}


def test_reconciler_calls_hook_post_create_after_create(monkeypatch):
    """(10d) A registered hook's post_create fires after a successful create,
    with the created id -- generic wiring already used by the OU hook (10c),
    confirmed here directly against a fake hook (Finding 2 style)."""
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}
    r._t_ids = {"thing": set()}

    seen = {}
    def fake_post_create(fields, new_id, ctx):
        seen["fields"] = fields
        seen["new_id"] = new_id
        seen["ctx"] = ctx

    monkeypatch.setitem(reconcile_mod.HOOKS, "thing", Hooks(
        build_create_payload=lambda fields, ctx: (["/v3/hooked"], {"name": fields["name"]}),
        post_create=fake_post_create,
    ))

    r.run()
    assert seen["fields"] == {"name": "A"}
    assert seen["ctx"] is r
    assert seen["new_id"] == r.id_map["thing"]["1"]
