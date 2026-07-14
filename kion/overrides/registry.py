from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from kion.import_ import (
    account_project_payload,
    account_cache_payload,
    find_root_ou_id,
    nkey,
    order_ous,
)

@dataclass
class Hooks:
    build_create_payload: Callable | None = None
    identity_ok: Callable | None = None
    post_create: Callable | None = None
    # -- optional hooks used by the self-referential OU pass (10c). All default
    # None so non-hooked resources and the generic path are unaffected; the
    # reconciler invokes each only when present.
    order_records: Callable | None = None   # (records, ctx) -> records (parent-first)
    pre_reconcile: Callable | None = None    # (records, ctx) -> None (seed ctx/id_map)
    adopt_key: Callable | None = None        # (fields, ctx) -> key tuple | None

def _account_payload(rec, ctx):
    payer_new = ctx.id_map["billing_source"].get(str(rec.get("__srcid__payer_id")))
    if payer_new is None:
        return None  # caller will skip (payer unresolved)
    proj_src = rec.get("__srcid__project_id")
    proj_new = ctx.id_map["project"].get(str(proj_src)) if proj_src not in (None, 0) else None
    if proj_new is not None:
        path, payload = account_project_payload(rec, proj_new, payer_new)
    else:
        path, payload = account_cache_payload(rec, payer_new)
    return [path], payload

def _funding_source_payload(rec, ctx):
    label = f"funding source '{rec.get('name')}'"
    ou_new = ctx.id_map["ou"].get(str(rec.get("__srcid__ou_id"))) or ctx.target_root_id
    if ou_new is None:
        return None  # caller will skip (no target OU)
    sid, status = ctx.resolve_scheme(rec.get("permission_scheme_name"), "funding", label)
    if status == "unresolved":
        return None
    uids, gids = ctx.resolve_owners(rec, label)
    payload = {
        "name": rec.get("name"),
        "description": rec.get("description") or "",
        "amount": str(rec.get("amount")) if rec.get("amount") is not None else "0",
        "start_datecode": rec.get("start_datecode"),
        "end_datecode": rec.get("end_datecode"),
        "ou_id": ou_new,
        "permission_scheme_id": sid,
        "owner_user_ids": uids,
        "owner_user_group_ids": gids,
    }
    return ["/v3/funding-source"], payload

def _project_payload(rec, ctx):
    label = f"project '{rec.get('name')}'"
    ou_new = ctx.id_map["ou"].get(str(rec.get("__srcid__ou_id")))
    if ou_new is None:
        return None  # caller will skip (required OU unresolved)
    sid, status = ctx.resolve_scheme(rec.get("permission_scheme_name"), "project", label)
    if status == "unresolved":
        return None
    uids, gids = ctx.resolve_owners(rec, label)
    payload = {
        "name": rec.get("name"),
        "description": rec.get("description") or "",
        "ou_id": ou_new,
        "permission_scheme_id": sid,
        "owner_user_ids": uids,
        "owner_user_group_ids": gids,
    }
    if rec.get("auto_pay") is not None:
        payload["auto_pay"] = rec["auto_pay"]
    if rec.get("default_aws_region"):
        payload["default_aws_region"] = rec["default_aws_region"]
    return ["/v3/project", "/v3/project/with-budget"], payload

# -- OU (10c) --------------------------------------------------------------
# OU is the only self-referential (hierarchical) entity: a parent OU must be
# reconciled before its children, the source root maps onto the target root by
# POSITION (not name), and non-root parents are bridged source->target via
# id_map. This mirrors ``Importer._reconcile_ous`` (the oracle) through the
# generic reconciler's hook points.

def _ou_order(records, ctx):
    """Parent-before-child. Reuse ``order_ous`` (keys on ``id``/``parent_ou_id``,
    which the OU inventory records carry) so a parent is reconciled — and thus in
    ``id_map`` — before any child needs to resolve it."""
    adapted = [{"id": r["source_id"],
                "parent_ou_id": r["fields"].get("parent_ou_id"),
                "__rec": r} for r in records]
    return [a["__rec"] for a in order_ous(adapted)]


def _ou_pre_reconcile(records, ctx):
    """Seed the source-root -> target-root mapping BEFORE the record loop, so the
    root is caught by the reconciler's OK check (mapped + present) and never
    created or adopted — regardless of the two roots' names. If the target has no
    root, leave it unseeded: the root is minted top-level in the loop and anchored
    by ``_ou_post_create``. Mirrors the pre-loop block of ``_reconcile_ous``."""
    src_root = find_root_ou_id(
        [{"id": r["source_id"], "parent_ou_id": r["fields"].get("parent_ou_id")}
         for r in records])
    if src_root is not None and ctx.target_root_id is not None:
        ctx.id_map.setdefault("ou", {})[str(src_root)] = ctx.target_root_id


def _ou_parent_new(fields, ctx):
    """Resolve the create-time parent id: a top-level OU (source parent null/0)
    hangs off the target root, or 0 to mint a new top-level OU on a rootless
    target; otherwise the source parent id is remapped via id_map."""
    praw = fields.get("parent_ou_id")
    if praw in (None, 0):
        return ctx.target_root_id if ctx.target_root_id is not None else 0
    return ctx.id_map["ou"].get(str(praw))


def _ou_adopt_key(fields, ctx):
    """Adoption key = ``(target parent id, name)`` — the same shape the target OU
    index uses (which keys on the raw target ``parent_ou_id``). The source parent
    id is bridged to a target id through id_map, so adoption survives roots being
    named differently. Returns None when the parent isn't resolvable yet (no
    adopt — the create path will skip)."""
    praw = fields.get("parent_ou_id")
    parent_new = ctx.target_root_id if praw in (None, 0) else ctx.id_map["ou"].get(str(praw))
    if parent_new is None:
        return None
    return (parent_new, nkey(fields.get("name")))


def _ou_payload(fields, ctx):
    label = f"ou '{fields.get('name')}'"
    parent_new = _ou_parent_new(fields, ctx)
    if parent_new is None:
        return None  # non-root parent unresolved -> caller skips
    sid, status = ctx.resolve_scheme(fields.get("permission_scheme_name"), "ou", label)
    if status == "unresolved":
        return None
    uids, gids = ctx.resolve_owners(fields, label)
    payload = {
        "name": fields.get("name"),
        "description": fields.get("description") or "",
        "parent_ou_id": parent_new,
        "permission_scheme_id": sid,
        "owner_user_ids": uids,
        "owner_user_group_ids": gids,
    }
    return ["/v3/ou"], payload


def _ou_post_create(fields, new_id, ctx):
    """Rootless target: the first top-level OU created becomes the anchor root for
    the rest of the run (mirrors ``_reconcile_ous``' ``self.target_root_id = new_id``)."""
    if ctx.target_root_id is None and fields.get("parent_ou_id") in (None, 0):
        ctx.target_root_id = new_id


HOOKS = {
    "account": Hooks(
        build_create_payload=_account_payload,
        identity_ok=lambda rec, ctx: bool(rec.get("account_number")),
    ),
    "funding_source": Hooks(build_create_payload=_funding_source_payload),
    "project": Hooks(build_create_payload=_project_payload),
    "ou": Hooks(
        order_records=_ou_order,
        pre_reconcile=_ou_pre_reconcile,
        adopt_key=_ou_adopt_key,
        build_create_payload=_ou_payload,
        post_create=_ou_post_create,
    ),
    # billing_source, budget, scope hooks are added in Task 10d as those
    # entities are onboarded (kept here so the registry is the single seam).
}
