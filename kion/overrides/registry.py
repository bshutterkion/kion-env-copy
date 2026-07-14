from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from kion.client import KionAPIError
from kion.import_ import (
    account_project_payload,
    account_cache_payload,
    find_root_ou_id,
    nkey,
    order_ous,
    _missing_budget_months,
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
    # -- whole-resource reconcile override (10e). When set, the reconciler
    # delegates the ENTIRE reconcile for this resource to (ctx, records) and
    # returns; the generic per-record loop (and every other hook above) is
    # skipped. Used by budget, whose identity is (target scope, start, end) and
    # whose adoption reads are per-scope -- it doesn't fit the generic
    # list+natural-key model. Default None -> generic path, all else unchanged.
    reconcile_override: Callable | None = None  # (ctx, records) -> None
    # -- when True, EngineReconciler._index_target skips building the generic
    # _t_key/_t_ids index for this resource. Set only for a reconcile_override
    # resource whose override never consumes that index (budget reads target
    # budgets per-scope via _target_budgets_for), so the global-list GET the
    # index would issue is useless -- and on installs where the list endpoint
    # isn't valid it emits a misleading 'target ... list failed' warning every
    # run. NOTE: scope also has a reconcile_override but DOES consume
    # _t_key['scope'], so it must stay indexed -- do NOT set this for scope.
    skip_target_index: bool = False

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

def _account_post_create(fields, new_id, ctx):
    """Register a newly-created account by number so the later scope pass (Task
    10e) can resolve it, mirroring ``Importer._reconcile_accounts``' post-create
    lines (``self._t_acct_by_number[num] = new_id; self._t_acct_ids.add(new_id)``)."""
    num = fields.get("account_number")
    if num:
        ctx.t_acct_by_number[num] = new_id
        ctx.t_acct_ids.add(new_id)


def _billing_source_payload(fields, ctx):
    """Reuse ``Importer._billing_payload`` directly (do not duplicate the
    per-type payloads). custom/aws/oci recreate as shells; gcp/azure/anthropic
    have no API recreate path and are skipped -- mirroring
    ``Importer._reconcile_billing_sources``' skip warning."""
    from kion.import_ import Importer
    path, payload = Importer._billing_payload(fields)
    if path is None:
        reason = payload
        ctx.warnings.append(f"billing source '{fields.get('name')}': skipped — {reason}")
        return None
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


# -- budget (10e) ----------------------------------------------------------
# budget does NOT fit the generic list+natural-key reconcile: its identity is
# ``(target scope, start_datecode, end_datecode)`` and target budgets are read
# PER OU / PER project (``/v3/{ou,project}/{id}/budget``), never from one global
# list. So budget uses a whole-resource ``reconcile_override`` that owns the
# entire pass -- a faithful port of ``Importer._reconcile_budgets`` (+
# ``_target_budgets_for``/``_funding_oversubscription``/``_diagnose_budget_failure``)
# through the engine's machinery (ctx.id_map / ctx._post / ctx.counts / ...).
# NOTE: the engine keys everything on the SINGULAR resource name ``"budget"``
# (Importer used the plural ``"budgets"``); the state-key format is kept
# byte-identical for idempotency: ``f"{scope_kind}:{scope_id}:{start}:{end}"``.

def _target_budgets_for(ctx, kind, scope_id, cache):
    """``(start_datecode, end_datecode) -> budget_id`` for budgets on a target
    OU/project (cached per scope). Ports ``Importer._target_budgets_for``: budget
    names are auto-generated by Kion from the timeframe (and differ across
    versions), so identity is the date range, not the name."""
    cache_key = f"{kind}:{scope_id}"
    if cache_key in cache:
        return cache[cache_key]
    path = (f"/v3/ou/{scope_id}/budget" if kind == "ou"
            else f"/v3/project/{scope_id}/budget")
    index = {}
    try:
        for b in (ctx.client.get(path) or []):
            cfg = b.get("config") or {}
            index[(cfg.get("start_datecode"), cfg.get("end_datecode"))] = cfg.get("id")
    except KionAPIError as e:
        ctx.warnings.append(f"could not read target budgets for {cache_key}: {e.status}")
    cache[cache_key] = index
    return index


def _funding_oversubscription(records, ctx):
    """source funding_source_id -> (total_allocated, amount, budget_count, name)
    for funding sources whose total allocation across all snapshot budgets exceeds
    their amount (the usual cause of a create-time 'insufficient funds'). Ports
    ``Importer._funding_oversubscription`` reading the engine inventory instead of
    the snapshot: ``data[].funding_source_id`` are raw SOURCE ids, matching the
    funding_source inventory records' ``source_id``."""
    fs_amount, fs_name = {}, {}
    for r in ctx.inventory.get("funding_source", []):
        fid = r["source_id"]
        f = r["fields"]
        try:
            fs_amount[fid] = float(f.get("amount") or 0)
        except (TypeError, ValueError):
            fs_amount[fid] = 0.0
        fs_name[fid] = f.get("name")
    total, count = {}, {}
    for rec in records:
        per = {}
        for d in rec["fields"].get("data", []):
            fsid = d.get("funding_source_id")
            if fsid:
                try:
                    per[fsid] = per.get(fsid, 0.0) + float(d.get("amount") or 0)
                except (TypeError, ValueError):
                    pass
        for fsid, amt in per.items():
            total[fsid] = total.get(fsid, 0.0) + amt
            count[fsid] = count.get(fsid, 0) + 1
    over = {}
    for fsid, t in total.items():
        amt = fs_amount.get(fsid, 0.0)
        if t > amt + 0.5 and count.get(fsid, 0) > 1:
            over[fsid] = (t, amt, count[fsid], fs_name.get(fsid))
    return over


def _diagnose_budget_failure(ctx, b, label, oversub):
    """Append a specific reason for a budget create failure. Ports
    ``Importer._diagnose_budget_failure`` -- reads ``ctx._last_error`` (set by
    ``EngineReconciler._post``) and appends the matching cause line. ``b`` is the
    budget's export-shaped ``fields`` (carries start/end_datecode + data)."""
    err = (ctx._last_error.body if ctx._last_error else "") or ""
    if "timeframe not fully covered" in err:
        missing = _missing_budget_months(b)
        if missing:
            shown = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
            ctx.warnings.append(
                f"{label}: → cause: source budget has no row for {shown} "
                f"(Kion requires every month in "
                f"{b.get('start_datecode')}..{b.get('end_datecode')} covered)")
    elif "insufficient funds" in err:
        used = {d.get("funding_source_id") for d in b.get("data", []) if d.get("funding_source_id")}
        named = [
            f"'{oversub[f][3]}' (${oversub[f][0]:,.0f} allocated across "
            f"{oversub[f][2]} budgets vs ${oversub[f][1]:,.0f} available)"
            for f in used if f in oversub
        ]
        if named:
            ctx.warnings.append(f"{label}: → cause: over-subscribed funding source — " + "; ".join(named))
        else:
            ctx.warnings.append(
                f"{label}: → cause: the funding source(s) it draws from have "
                f"insufficient unallocated funds in this window on the target")


def _budget_reconcile(ctx, records):
    """Whole-resource reconcile for budget (see the section header). Faithful port
    of ``Importer._reconcile_budgets``: resolve the target scope via id_map, adopt
    by (start,end) read per-scope, else create with funding ids remapped and
    unresolved rows dropped; diagnose create failures."""
    print("\nbudget:")
    oversub = _funding_oversubscription(records, ctx)
    cache: dict = {}  # per-scope target-budget cache, scoped to this pass
    for rec in records:
        fields = rec["fields"]
        src_ou = fields.get("__srcid__ou_id")
        src_proj = fields.get("__srcid__project_id")
        ou_new = ctx.id_map["ou"].get(str(src_ou)) if src_ou else None
        proj_new = ctx.id_map["project"].get(str(src_proj)) if src_proj else None
        scope_kind = "ou" if ou_new is not None else "project"
        scope_id = ou_new if ou_new is not None else proj_new
        label = f"budget '{fields.get('name') or rec.get('source_id')}'"
        if scope_id is None:
            ctx.warnings.append(f"{label}: target OU/project unresolved, skipped")
            ctx.skipped["budget"] += 1
            continue

        start = fields.get("start_datecode")
        end = fields.get("end_datecode")
        state_key = f"{scope_kind}:{scope_id}:{start}:{end}"
        nat = (start, end)
        existing = _target_budgets_for(ctx, scope_kind, scope_id, cache)

        if state_key in ctx.id_map["budget"] and nat in existing:
            ctx._note_ok("budget", label)
            continue
        if nat in existing:
            ctx.id_map["budget"][state_key] = existing[nat]
            ctx.counts["budget"]["adopt"] += 1
            print(f"  = adopt {label} (existing id {existing[nat]})")
            continue

        action = "recreate" if state_key in ctx.id_map["budget"] else "create"
        data, missing_fs, fs_ids = [], set(), set()
        for row in fields.get("data", []):
            entry = {
                "amount": str(row.get("amount")) if row.get("amount") is not None else "0",
                "datecode": row.get("datecode"),
                "priority": row.get("priority") if row.get("priority") is not None else 0,
            }
            fs_src = row.get("funding_source_id")
            if fs_src not in (None, 0):
                fs_new = ctx.id_map["funding_source"].get(str(fs_src))
                if fs_new is None:
                    missing_fs.add(fs_src)
                    continue
                entry["funding_source_id"] = fs_new
                fs_ids.add(fs_new)
            data.append(entry)
        for m in sorted(missing_fs):
            ctx.warnings.append(f"{label}: funding source {m} unresolved, row(s) dropped")
        if not data:
            ctx.warnings.append(f"{label}: no usable rows, skipped")
            ctx.skipped["budget"] += 1
            continue

        payload = {
            "start_datecode": start,
            "end_datecode": end,
            "data": data,
        }
        if fs_ids:
            payload["funding_source_ids"] = sorted(fs_ids)
        if ou_new is not None:
            payload["ou_id"] = ou_new
        else:
            payload["project_id"] = proj_new
        new_id = ctx._post("budget", "/v3/budget", payload, state_key, action, label)
        if new_id is not None:
            ctx.id_map["budget"][state_key] = new_id
        else:
            _diagnose_budget_failure(ctx, fields, label, oversub)


# -- scope (10g) -----------------------------------------------------------
# scope does NOT fit the generic list+natural-key create path: adoption is
# ``(target_project, name)`` and the create remaps account *numbers* to target
# account ids inside ``criteria.account_criteria.account_ids`` (>=1 must exist)
# with an "Invalid scope criteria" failure diagnostic. So scope uses a
# whole-resource ``reconcile_override`` -- a faithful port of
# ``Importer._reconcile_scopes`` through the engine machinery
# (ctx.id_map / ctx._t_key / ctx._t_ids / ctx.t_acct_by_number / ctx._post /
# ctx._last_error / ctx.counts / ...). The engine keys everything on the
# SINGULAR resource name ``"scope"`` (Importer used the plural ``"scopes"``).
# The source scope id lives in ``rec["source_id"]`` (the export field
# ``source_scope_id`` popped by inventory._finish_export_record) -- the standard
# inventory contract every override reads; its value is the source scope id.

def _scope_reconcile(ctx, records):
    """Whole-resource reconcile for scope (see the section header). Faithful port
    of ``Importer._reconcile_scopes``: resolve the target project via id_map,
    OK/adopt against the engine-built target scope index (keyed
    ``(target_project_id, nkey(name))``), else create with account numbers
    remapped to target ids (>=1 required) and diagnose an "Invalid scope
    criteria" create failure."""
    print("\nscope:")
    t_key = ctx._t_key.setdefault("scope", {})
    t_ids = ctx._t_ids.setdefault("scope", set())
    id_map = ctx.id_map.setdefault("scope", {})
    for rec in records:
        fields = rec["fields"]
        src = rec["source_id"]
        label = f"scope '{fields.get('name')}'"
        src_proj = fields.get("__srcid__project_id")
        proj_new = ctx.id_map["project"].get(str(src_proj))
        if proj_new is None:
            ctx.warnings.append(f"{label}: project {src_proj} unresolved, skipped")
            ctx.skipped["scope"] += 1
            continue

        key = (proj_new, nkey(fields.get("name")))
        mapped = id_map.get(str(src))
        if mapped is not None and mapped in t_ids:
            ctx._note_ok("scope", label)
            continue
        if key in t_key:
            found = t_key[key]
            id_map[str(src)] = found
            ctx.counts["scope"]["adopt"] += 1
            print(f"  = adopt {label} (existing id {found})")
            continue

        # Account criteria require >=1 real account; remap by account_number.
        numbers = fields.get("account_numbers") or []
        acct_ids, missing = [], []
        for n in numbers:
            tid = ctx.t_acct_by_number.get(n)
            (acct_ids if tid is not None else missing).append(tid if tid is not None else n)
        for m in missing:
            ctx.warnings.append(f"{label}: account {m} not on target, dropped")
        if not acct_ids:
            ctx.warnings.append(
                f"{label}: none of its {len(numbers)} account(s) exist on target "
                f"(scope needs >=1) — skipped")
            ctx.skipped["scope"] += 1
            continue

        criteria = dict(fields.get("criteria") or {})
        ac = dict(criteria.get("account_criteria") or {})
        ac["type"] = ac.get("type") or "account_ids"
        ac["account_ids"] = acct_ids
        criteria["account_criteria"] = ac

        action = "recreate" if mapped is not None else "create"
        payload = {
            "name": fields.get("name"),
            "alias": fields.get("alias") or "",
            "description": fields.get("description") or "",
            "project_id": proj_new,
            "start_datecode": fields.get("start_datecode"),
            "end_datecode": fields.get("end_datecode"),
            "criteria": criteria,
        }
        new_id = ctx._post("scope", "/beta/scope", payload, src, action, label)
        if new_id is not None:
            id_map[str(src)] = new_id
        elif ctx._last_error and "Invalid scope criteria" in (ctx._last_error.body or ""):
            ctx.warnings.append(
                f"{label}: → cause: a condition references a tag key / region / "
                f"service the target hasn't ingested billing data for "
                f"(create only succeeds once that data exists on the target)")


HOOKS = {
    "account": Hooks(
        build_create_payload=_account_payload,
        identity_ok=lambda rec, ctx: bool(rec.get("account_number")),
        post_create=_account_post_create,
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
    "billing_source": Hooks(build_create_payload=_billing_source_payload),
    "budget": Hooks(reconcile_override=_budget_reconcile, skip_target_index=True),
    "scope": Hooks(reconcile_override=_scope_reconcile),
}
