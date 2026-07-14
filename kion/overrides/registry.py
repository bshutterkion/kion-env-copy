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
    payer_new = ctx.id_map["billing_sources"].get(str(rec.get("__srcid__payer_id")))
    if payer_new is None:
        return None  # caller will skip (payer unresolved)
    proj_src = rec.get("__srcid__project_id")
    proj_new = ctx.id_map["projects"].get(str(proj_src)) if proj_src not in (None, 0) else None
    if proj_new is not None:
        path, payload = account_project_payload(rec, proj_new, payer_new)
    else:
        path, payload = account_cache_payload(rec, payer_new)
    return [path], payload

HOOKS = {
    "account": Hooks(
        build_create_payload=_account_payload,
        identity_ok=lambda rec, ctx: bool(rec.get("account_number")),
    ),
    # billing_source, budget, scope hooks are added in Task 10 as those
    # entities are onboarded (kept here so the registry is the single seam).
}
