"""Generic inventory reader: metadata-driven equivalent of ``export_install``.

Walks a source install into ``{resource: [{source_id, natural_key, fields}, ...]}``
records, in dependency order (least-depended-on first, via ``order_resources``),
so that when a resource is read, every resource its ``Reference``s point at has
already been read and indexed by ``natural_key`` — letting reference fields be
translated from source ids to portable natural keys as we go (``to_natural``).

Reads the LIST endpoint for each resource (derived from ``read_path`` via
``kion.engine.paths.list_path`` — the SAME derivation ``EngineReconciler``
uses to index the target, so read and reconcile can't diverge on which
endpoint a resource's records come from).
"""
from __future__ import annotations

import sys

from kion.engine.keys import natural_key
from kion.engine.order import order_resources
from kion.engine.paths import list_path
from kion.engine.read import list_records
from kion.engine.refmap import to_natural
from kion.export import _account_record
from kion.import_ import order_ous


def _on_read_error(path, exc) -> None:
    """Surface a list-read failure to stderr, in the style of ``export._warn``
    (``build_inventory`` has no warnings list to append to)."""
    print(f"  ! {path} list failed: {exc.status}", file=sys.stderr)


def _parent_target(res: str, parent_field: str, refs: dict) -> str:
    """Which resource a ``name_in_parent`` hierarchy's parent field points at.
    Prefer the declared ``Reference`` (e.g. project.ou_id -> ou); fall back to
    the resource itself for a self-referential hierarchy (e.g. ou.parent_ou_id,
    which has no ``references.yaml`` entry — an OU's parent is another OU)."""
    for r in refs.get(res, []):
        if r.field == parent_field:
            return r.target
    return res


def _order_self_ref(res: str, records: list, nkeys: dict, refs: dict) -> list:
    """Parent-before-child ordering for a *self-referential* name_in_parent
    resource, so each record's parent key is already in ``id_to_key`` when the
    child's key is computed. OU is the only such entity; its parent field is
    ``parent_ou_id``, which ``order_ous`` keys on. Non-self-referential
    hierarchies (project/scope, whose parent is another resource) are untouched —
    their parent is fully read before them by ``order_resources``."""
    spec = nkeys.get(res) or {}
    if (spec.get("kind") == "name_in_parent"
            and spec.get("parent_field") == "parent_ou_id"
            and _parent_target(res, "parent_ou_id", refs) == res):
        return order_ous(records)
    return records


def _record_key(res: str, record: dict, nkeys: dict, refs: dict, id_to_key: dict) -> tuple:
    """The record's own natural key. For a ``name_in_parent`` resource, the
    parent component is resolved to the parent's already-computed key (via the
    accumulating ``id_to_key`` map) and flattened into the result — a record
    with no parent (e.g. a root OU, ``parent_ou_id`` is None) collapses to a
    plain ``(name,)`` rather than carrying a placeholder parent component."""
    spec = nkeys[res]

    def parent_key_of(resource, parent_field, rec):
        val = rec.get(parent_field)
        if val in (None, 0):
            return ()
        target = _parent_target(resource, parent_field, refs)
        return id_to_key.get((target, val), ())

    raw = natural_key(res, record, nkeys, parent_key_of=parent_key_of)
    if spec.get("kind") != "name_in_parent":
        return raw

    parent, name = raw
    if isinstance(parent, tuple):
        return parent + (name,)
    if parent is None:
        return (name,)
    return (parent, name)


def _read_accounts(client, res_refs, id_to_key) -> list[dict]:
    """Cloud-account inventory records: the UNION of project-associated accounts
    (``/v3/account``) and cached, unassociated accounts (``/v3/account-cache``),
    mirroring ``export._export_accounts`` / ``_account_record``.

    ``account`` has no ``generator_config.yaml`` read entry (a vendor gap) and
    its records don't fit the generic list path: cached ids are namespaced
    ``cache:<id>`` so they never collide with associated ids, and the record's
    portable shape comes from ``_account_record`` (provider derived via
    ``ACCOUNT_PROVIDER``, cached accounts carry no project). Reference fields
    (``payer_id``/``project_id``) are ``to_natural``-translated so the source
    ids survive as ``__srcid__payer_id``/``__srcid__project_id`` for the 10c
    account hook. Natural key is ``(account_number,)``."""
    raw = [_account_record(a, cached=False)
           for a in (list_records(client, "/v3/account", on_error=_on_read_error) or [])]
    raw += [_account_record(a, cached=True)
            for a in (list_records(client, "/v3/account-cache", on_error=_on_read_error) or [])]

    out = []
    for rec in raw:
        source_id = rec.pop("source_id")  # already 'cache:<id>' for cached accounts
        key = (rec.get("account_number"),)
        id_to_key[("account", source_id)] = key
        fields = to_natural(rec, res_refs, id_to_key)
        out.append({"source_id": source_id, "natural_key": key, "fields": fields})
    return out


def build_inventory(client, meta: dict, refs: dict, nkeys: dict,
                     resources: list[str]) -> dict[str, list[dict]]:
    """Read ``resources`` off ``client`` in dependency order and return
    ``{resource: [{source_id, natural_key, fields}, ...]}``. ``fields`` is the
    raw record minus ``meta[res].ignores``, with reference fields (per
    ``refs.get(res, [])``) translated from source ids to natural keys via
    ``to_natural``."""
    id_to_key: dict[tuple[str, object], tuple] = {}
    inventory: dict[str, list[dict]] = {}

    for res in order_resources(resources, refs):
        res_refs = refs.get(res, [])
        # account is a union of two endpoints with namespaced cached ids — it
        # can't go through the generic single-list path (see _read_accounts).
        if res == "account":
            inventory[res] = _read_accounts(client, res_refs, id_to_key)
            continue

        rm = meta[res]
        path = list_path(getattr(rm, "read_path", None))
        records = list_records(client, path, on_error=_on_read_error) if path else []
        # A self-referential hierarchy (OU) must be walked parent-first so the
        # name_in_parent key resolves the parent's already-computed key.
        records = _order_self_ref(res, records, nkeys, refs)
        ignores = set(getattr(rm, "ignores", None) or [])

        out = []
        for record in records:
            source_id = record.get("id")
            key = _record_key(res, record, nkeys, refs, id_to_key)
            id_to_key[(res, source_id)] = key

            fields = {k: v for k, v in record.items() if k not in ignores}
            fields = to_natural(fields, res_refs, id_to_key)

            out.append({"source_id": source_id, "natural_key": key, "fields": fields})
        inventory[res] = out

    return inventory
