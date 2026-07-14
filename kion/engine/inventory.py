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

from kion.client import KionAPIError
from kion.engine.keys import natural_key
from kion.engine.order import order_resources
from kion.engine.paths import list_path
from kion.engine.refmap import to_natural


def _list_records(client, path: str) -> list[dict]:
    """GET a resource's list endpoint, defensively unwrapping either a bare
    list or an ``{items|data, ...}``-style envelope — the same shapes
    ``kion/export.py`` and ``EngineReconciler._index_target`` already handle."""
    try:
        resp = client.get(path)
    except KionAPIError:
        return []
    if isinstance(resp, dict):
        records = resp.get("items") if "items" in resp else resp.get("data")
        return records or []
    return resp or []


def _parent_target(res: str, parent_field: str, refs: dict) -> str:
    """Which resource a ``name_in_parent`` hierarchy's parent field points at.
    Prefer the declared ``Reference`` (e.g. project.ou_id -> ou); fall back to
    the resource itself for a self-referential hierarchy (e.g. ou.parent_ou_id,
    which has no ``references.yaml`` entry — an OU's parent is another OU)."""
    for r in refs.get(res, []):
        if r.field == parent_field:
            return r.target
    return res


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
        rm = meta[res]
        path = list_path(getattr(rm, "read_path", None))
        records = _list_records(client, path) if path else []
        res_refs = refs.get(res, [])
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
