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
from kion.engine.paths import list_path
from kion.engine.read import list_records
from kion.engine.refmap import to_target_ids
from kion.import_ import (
    ACTIONS,
    TYPE_DEFAULT_SCHEME,
    find_root_ou_id,
)
from kion.import_ import resolve_owners as _pure_resolve_owners
from kion.import_ import resolve_scheme as _pure_resolve_scheme
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

        # ctx enrichment the per-entity hooks read to build create payloads
        # (schemes/owners/root/current user), mirroring Importer. Populated by
        # _index_ctx (only when config is set); left as safe empties otherwise so
        # the resolve_* methods never AttributeError.
        self.schemes: dict = {}
        self.users: dict = {}
        self.groups: dict = {}
        self.target_root_id = None
        self.current_user_id = None
        self.t_acct_by_number: dict = {}  # account hook fills this as it creates (10d)
        self.t_acct_ids: set = set()      # account hook fills this as it creates (10d)
        self._owner_fallback = 0

        self._placeholder = 0
        self._pinned_path: dict = {}
        self._last_error = None       # last KionAPIError from _post (for diagnostics)
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
        self._last_error = None
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
        self._last_error = last_err
        self.warnings.append(f"{label}: {action} failed: {last_err}")
        self.failed[res] += 1
        return None

    # -- target indexing ------------------------------------------------------
    def _index_ctx(self):
        """Enrich ctx with the target lookups the per-entity hooks need to build
        create payloads — schemes/users/groups by natural key, the target root
        OU, and the importing user (fallback owner). Mirrors the ctx block of
        ``Importer._index_target``.

        Called unconditionally from ``_index_target`` (independent of the
        per-resource inventory loop, so it runs even when the inventory is empty),
        but only when ``config`` is set: unit tests that inject ``_t_key`` skip
        ``_index_target`` entirely, and tests that pass ``config=None`` don't
        need — and must not trigger — these reads."""
        self.schemes = {s["name"]: s["id"]
                        for s in (self.client.get("/v3/permission-scheme") or [])}
        self.users = {u["email"].lower(): u["id"]
                      for u in (self.client.get("/v3/user") or []) if u.get("email")}
        self.groups = {g["name"]: g["id"]
                       for g in (self.client.get("/v3/user-group") or []) if g.get("name")}
        # App API keys are user-scoped, so any key returned belongs to the
        # importing user — used as the fallback owner.
        try:
            keys = self.client.get("/v3/app-api-key") or []
            self.current_user_id = keys[0].get("user_id") if keys else None
        except KionAPIError:
            self.current_user_id = None
        ous = self.client.get("/v3/ou") or []
        self.target_root_id = find_root_ou_id(ous)

    def _index_target(self):
        """Populate ``_t_key``/``_t_ids`` per resource by listing the target, and
        enrich the reconcile ctx (schemes/owners/root/current user).

        Factored out so tests can set ``_t_key``/``_t_ids`` by hand and skip the
        network entirely (``run()`` only calls this when they're still empty).

        The per-resource loop is scoped to ``self.inventory`` (the resources
        actually being reconciled), not all of ``self.meta`` — ``self.meta`` is
        the full ~60-resource generator_config, most of which aren't in play for a
        given run. The ctx enrichment (``_index_ctx``) is separate and
        unconditional, so it still runs when the inventory is empty."""
        if self.config is not None:
            self._index_ctx()
        for res in self.inventory:
            rm = self.meta[res]
            read_path = getattr(rm, "read_path", None)
            if not read_path:
                self._t_key.setdefault(res, {})
                self._t_ids.setdefault(res, set())
                continue
            lp = list_path(read_path)  # shared with kion.engine.inventory

            def _on_error(path, e, res=res):
                self.warnings.append(f"target {res} list failed: {e.status}")

            records = list_records(self.client, lp, on_error=_on_error)  # shared unwrap + pagination
            if res == "account":
                # Union in the target's cached (unassociated) accounts,
                # mirroring Importer._index_target -- an account is either
                # associated (/v3/account) or cached (/v3/account-cache),
                # never both, so a scope referencing an ADOPTED account (either
                # bucket) must be resolvable via the normal adopt index too.
                records = records + list_records(
                    self.client, "/v3/account-cache", on_error=_on_error)

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

            if res == "account":
                # Pre-populate the account-number index from EXISTING target
                # accounts (not just ones this run creates -- see
                # kion.overrides.registry._account_post_create for the
                # create-time half) so the scope pass can resolve accounts
                # adopted here. setdefault (not overwrite) so an associated
                # account wins over a same-numbered cache entry, matching
                # Importer._index_target.
                for rec in records:
                    num, rid = rec.get("account_number"), rec.get("id")
                    if num and rid is not None:
                        self.t_acct_by_number.setdefault(num, rid)
                self.t_acct_ids |= ids

    # -- ctx helpers hooks call (via the reconciler passed as ctx) -------------
    def resolve_scheme(self, name, entity_type: str, label: str):
        """Resolve a permission scheme id for a create, appending the same
        type_default/default/unresolved warnings as ``Importer._resolve_scheme``."""
        type_default = TYPE_DEFAULT_SCHEME.get(entity_type)
        default_id = self.config.default_permission_scheme_id if self.config else None
        sid, status = _pure_resolve_scheme(name, self.schemes, type_default, default_id)
        if status == "type_default":
            self.warnings.append(f"{label}: using '{type_default}' (id {sid})")
        elif status == "default":
            self.warnings.append(f"{label}: scheme '{name}' not on target -> DEFAULT id {sid}")
        elif status == "unresolved":
            self.warnings.append(
                f"{label}: no permission scheme resolvable (no '{type_default}', no DEFAULT)")
        return sid, status

    def resolve_owners(self, rec, label: str):
        """Resolve owner user/group ids by email/name, falling back to the
        importing user when none resolve (OU/funding/project require ≥1 owner) —
        mirrors ``Importer._resolve_owners``."""
        uids, gids, dropped = _pure_resolve_owners(
            rec.get("owner_user_emails"), rec.get("owner_user_group_names"),
            self.users, self.groups)
        for d in dropped:
            self.warnings.append(f"{label}: dropped owner {d} (not on target)")
        if not uids and not gids and self.current_user_id is not None:
            uids = [self.current_user_id]
            self._owner_fallback += 1
        return uids, gids

    def _key_to_tid(self) -> dict:
        """Flatten ``_t_key`` into the ``(target_resource, key) -> id`` shape
        ``to_target_ids`` expects. Rebuilt from current state each call so
        newly created/adopted records (which update ``_t_key`` as we go, in
        dependency order) are visible to resources processed afterward."""
        return {(res, k): tid for res, kmap in self._t_key.items() for k, tid in kmap.items()}

    # -- reconcile ------------------------------------------------------------
    def _note_ok(self, res: str, label: str):
        """OK: already mapped and still present -> nothing. Mirrors
        ``Importer._note_ok`` (used by the budget override, whose OK check runs
        outside the generic per-record loop)."""
        self.counts[res]["ok"] += 1

    def _reconcile(self, res: str):
        rm = self.meta[res]
        refs = self.refs.get(res, [])
        hooks = HOOKS.get(res)

        # Whole-resource override (10e): budget can't be reconciled by the generic
        # list+natural-key path (its identity is (target scope, start, end) and
        # adoption is read per-scope). When present the override owns the entire
        # reconcile (adopt/create/skip + counts) and we return; the generic path
        # below is byte-for-byte unchanged for every resource without one.
        if hooks and hooks.reconcile_override is not None:
            hooks.reconcile_override(self, self.inventory.get(res, []))
            return

        t_key = self._t_key.setdefault(res, {})
        t_ids = self._t_ids.setdefault(res, set())
        id_map = self.id_map.setdefault(res, {})

        # Hierarchical/self-referential resources (OU) need their records ordered
        # parent-first, and may seed ctx/id_map (e.g. the source-root -> target-root
        # mapping) before any record is processed. Both hooks are optional; the
        # generic path is unchanged when they're absent.
        records = self.inventory.get(res, [])
        if hooks and hooks.order_records is not None:
            records = hooks.order_records(records, self)
        if hooks and hooks.pre_reconcile is not None:
            hooks.pre_reconcile(records, self)

        print(f"\n{res}:")
        for rec in records:
            src = rec["source_id"]
            nk = tuple(rec["natural_key"])
            fields = rec["fields"]
            label = f"{res} '{fields.get('name', nk)}'"

            # OK: already mapped and still present on the target.
            mapped = id_map.get(str(src))
            if mapped is not None and mapped in t_ids:
                self.counts[res]["ok"] += 1
                continue

            # ADOPT: same natural key already exists on the target. A resource may
            # override the per-record adoption key (OU keys on the *target* parent
            # id, bridged via id_map, not on its name-chain inventory key); a None
            # override means "not adoptable yet" -> fall through to create/skip.
            adopt_nk = nk
            if hooks and hooks.adopt_key is not None:
                adopt_nk = hooks.adopt_key(fields, self)
            if adopt_nk is not None and adopt_nk in t_key:
                found = t_key[adopt_nk]
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
                # post_create runs first so any ctx anchoring it does (e.g. a
                # rootless target adopting the just-created OU as target_root_id)
                # is visible when we compute this record's index key below.
                if hooks and hooks.post_create is not None:
                    hooks.post_create(fields, new_id, self)
                # Newly created record becomes resolvable to anything depending on
                # this resource that reconciles afterward. Index it under the same
                # key adoption would look it up by (id-based for OU, else the
                # name-chain inventory key).
                index_key = nk
                if hooks and hooks.adopt_key is not None:
                    k = hooks.adopt_key(fields, self)
                    if k is not None:
                        index_key = k
                t_key[index_key] = new_id
                t_ids.add(new_id)

    def run(self) -> dict:
        if not self._t_key:
            self._index_target()
        for res in order_resources(list(self.inventory), self.refs):
            self._reconcile(res)
        return self.id_map
