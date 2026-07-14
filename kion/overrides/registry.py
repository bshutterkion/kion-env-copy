from __future__ import annotations
from dataclasses import dataclass
from typing import Callable
from kion.import_ import account_project_payload, account_cache_payload

@dataclass
class Hooks:
    build_create_payload: Callable | None = None
    identity_ok: Callable | None = None
    post_create: Callable | None = None

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

HOOKS = {
    "account": Hooks(
        build_create_payload=_account_payload,
        identity_ok=lambda rec, ctx: bool(rec.get("account_number")),
    ),
    "funding_source": Hooks(build_create_payload=_funding_source_payload),
    "project": Hooks(build_create_payload=_project_payload),
    # billing_source, budget, scope hooks are added in Task 10 as those
    # entities are onboarded (kept here so the registry is the single seam).
}
