"""Generic reconcile adapter: metadata-driven equivalent of ``Importer``.

``Importer`` (kion/import_.py) hand-codes, per entity kind, the same decision
logic: OK (already mapped + present) / ADOPT (natural key already exists on
the target) / CREATE / RECREATE (mapped id gone from target) / SKIP (a
required reference couldn't be resolved, or the record fails an identity
check). ``EngineReconciler`` reproduces that *shape* of decision generically,
driven by ``ResourceMeta``/``Reference`` metadata instead of one method per
kind. Special cases that don't fit the generic "fields minus ignores, refs
mapped to target ids" payload (e.g. cloud accounts routing to the project or
account-cache create family) go through ``kion.overrides.registry.HOOKS``
instead of being special-cased here.

Known gap (acceptable for this task — see task-8-report.md): resources whose
identity or create payload depends on a *hierarchical* natural key (e.g. an
OU's parent chain) aren't resolved generically here; only ``account`` has a
hook so far (billing_source/budget/scope hooks land in Task 10).
"""
from __future__ import annotations

from kion.client import KionAPIError
from kion.engine.keys import natural_key
from kion.engine.order import order_resources
from kion.engine.refmap import to_target_ids
from kion.import_ import ACTIONS
from kion.overrides.registry import HOOKS


class EngineReconciler:
    def __init__(self, client, config, inventory: dict, meta: dict, refs: dict,
                 nkeys: dict, apply: bool, id_map: dict | None = None):
        self.client = client
        self.config = config
        self.inventory = inventory
        self.meta = meta
        self.refs = refs
        self.nkeys = nkeys
        self.apply = apply
        self.id_map = id_map or {}
        for res in inventory:
            self.id_map.setdefault(res, {})

        # target lookups: natural_key -> target id, and the set of live target ids,
        # per resource. Tests inject these directly (bypassing _index_target /
        # the network) — see the module docstring and the brief.
        self._t_key: dict = {}
        self._t_ids: dict = {}

        self._placeholder = 0
        self._pinned_path: dict = {}
        self.warnings: list[str] = []
        # counts[res][action], skipped[res], failed[res] — same shape as Importer.
        self.counts = {res: dict.fromkeys(ACTIONS, 0) for res in inventory}
        self.skipped = dict.fromkeys(inventory.keys(), 0)
        self.failed = dict.fromkeys(inventory.keys(), 0)

    # -- low level ----------------------------------------------------------
    def _placeholder_id(self, res: str, src) -> str:
        self._placeholder += 1
        return f"<new:{res}:{src}>"

    @staticmethod
    def _extract_id(resp):
        if isinstance(resp, dict):
            for key in ("record_id", "id"):
                if key in resp:
                    return resp[key]
        return resp

    def _post(self, res: str, path, payload: dict, src, action: str, label: str):
        """Create on apply, or return a placeholder in plan mode. Mirrors
        ``Importer._post``: ``path`` may be a list of candidate endpoints tried
        in order, with the first success pinned for the rest of the run."""
        paths = [path] if isinstance(path, str) else list(path)
        if not self.apply:
            print(f"  ~ {action} {label}")
            self.counts[res][action] += 1
            return self._placeholder_id(res, src)

        if self._pinned_path.get(res) in paths:
            paths = [self._pinned_path[res]] + [p for p in paths if p != self._pinned_path[res]]
        last_err = None
        for p in paths:
            try:
                new_id = self._extract_id(self.client.post(p, json=payload))
            except KionAPIError as e:
                last_err = e
                continue
            self._pinned_path[res] = p
            print(f"  + {action} {label} -> id {new_id}")
            self.counts[res][action] += 1
            return new_id
        self.warnings.append(f"{label}: {action} failed: {last_err}")
        self.failed[res] += 1
        return None

    # -- target indexing ------------------------------------------------------
    def _index_target(self):
        """Populate ``_t_key``/``_t_ids`` per resource by listing the target.

        Factored out so tests can set ``_t_key``/``_t_ids`` by hand and skip the
        network entirely (``run()`` only calls this when they're still empty).

        Scoped to ``self.inventory`` (the resources actually being reconciled),
        not all of ``self.meta`` — ``self.meta`` is the full ~30-resource
        generator_config, and most of those aren't in play for a given run.
        """
        for res in self.inventory:
            rm = self.meta[res]
            read_path = getattr(rm, "read_path", None)
            if not read_path:
                self._t_key.setdefault(res, {})
                self._t_ids.setdefault(res, set())
                continue
            list_path = read_path.split("/{")[0]  # strip a trailing /{id} template
            try:
                resp = self.client.get(list_path)
            except KionAPIError as e:
                self.warnings.append(f"target {res} list failed: {e.status}")
                resp = []
            records = resp
            if isinstance(resp, dict):
                records = resp.get("items") if "items" in resp else resp.get("data")
            records = records or []

            key_map, ids = {}, set()
            for rec in records:
                rid = rec.get("id")
                if rid is None:
                    continue
                ids.add(rid)
                try:
                    key_map[natural_key(res, rec, self.nkeys)] = rid
                except (KeyError, ValueError):
                    continue
            self._t_key[res] = key_map
            self._t_ids[res] = ids

    def _key_to_tid(self) -> dict:
        """Flatten ``_t_key`` into the ``(target_resource, key) -> id`` shape
        ``to_target_ids`` expects. Rebuilt from current state each call so
        newly created/adopted records (which update ``_t_key`` as we go, in
        dependency order) are visible to resources processed afterward."""
        return {(res, k): tid for res, kmap in self._t_key.items() for k, tid in kmap.items()}

    # -- reconcile ------------------------------------------------------------
    def _reconcile(self, res: str):
        rm = self.meta[res]
        refs = self.refs.get(res, [])
        hooks = HOOKS.get(res)
        t_key = self._t_key.setdefault(res, {})
        t_ids = self._t_ids.setdefault(res, set())
        id_map = self.id_map.setdefault(res, {})

        print(f"\n{res}:")
        for rec in self.inventory.get(res, []):
            src = rec["source_id"]
            nk = tuple(rec["natural_key"])
            fields = rec["fields"]
            label = f"{res} '{fields.get('name', nk)}'"

            # OK: already mapped and still present on the target.
            mapped = id_map.get(str(src))
            if mapped is not None and mapped in t_ids:
                self.counts[res]["ok"] += 1
                continue

            # ADOPT: same natural key already exists on the target.
            if nk in t_key:
                found = t_key[nk]
                id_map[str(src)] = found
                self.counts[res]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                continue

            action = "recreate" if mapped is not None else "create"

            if hooks and hooks.identity_ok is not None and not hooks.identity_ok(fields, self):
                self.warnings.append(f"{label}: identity check failed, skipped")
                self.skipped[res] += 1
                continue

            if hooks and hooks.build_create_payload is not None:
                built = hooks.build_create_payload(fields, self)
                if built is None:
                    self.warnings.append(f"{label}: hook could not build payload, skipped")
                    self.skipped[res] += 1
                    continue
                paths, payload = built
            else:
                mapped_fields, unresolved = to_target_ids(fields, refs, self._key_to_tid())
                if unresolved:
                    self.warnings.append(
                        f"{label}: unresolved reference(s) {unresolved}, skipped")
                    self.skipped[res] += 1
                    continue
                ignores = set(rm.ignores or [])
                payload = {k: v for k, v in mapped_fields.items()
                           if k not in ignores and not k.startswith("__srcid__")}
                paths = rm.create_path

            new_id = self._post(res, paths, payload, src, action, label)
            if new_id is not None:
                id_map[str(src)] = new_id
                # Newly created/adopted record becomes resolvable to anything
                # depending on this resource that reconciles afterward.
                t_key[nk] = new_id
                t_ids.add(new_id)

    def run(self) -> dict:
        if not self._t_key:
            self._index_target()
        for res in order_resources(list(self.inventory), self.refs):
            self._reconcile(res)
        return self.id_map
