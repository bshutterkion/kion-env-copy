"""Export a Kion install's OUs, funding sources, projects, and budgets.

References to things we do NOT copy (permission schemes, owner users/groups) are
stored *by name* so the snapshot is self-contained and portable to an install
where the numeric ids differ. Source numeric ids are kept only for traceability
and to wire up the old->new id map during import.

The swagger response definitions are incomplete (e.g. the live project object
returns ``permission_scheme_id`` and owners even though swagger omits them), so we
read fields defensively with ``.get`` and degrade to empty/None when absent.
"""
from __future__ import annotations

import datetime as _dt
import sys

from .client import KionAPIError, KionClient

SCHEMA_VERSION = 3  # v2 added scopes; v3 added billing sources


def _warn(msg: str) -> None:
    print(f"  ! {msg}", file=sys.stderr)


def _scheme_name(scheme_id, id_to_name) -> str | None:
    if scheme_id in (None, 0):
        return None
    return id_to_name.get(scheme_id)


def _owner_emails(owner_users) -> list[str]:
    return [u.get("email") for u in (owner_users or []) if u.get("email")]


def _owner_group_names(owner_groups) -> list[str]:
    return [g.get("name") for g in (owner_groups or []) if g.get("name")]


def export_install(client: KionClient) -> dict:
    # --- lookup maps for name-based references --------------------------------
    schemes = client.get("/v3/permission-scheme") or []
    scheme_id_to_name = {s["id"]: s["name"] for s in schemes}
    print(f"  permission schemes: {len(schemes)}")

    snapshot: dict = {
        "schema_version": SCHEMA_VERSION,
        "source_url": client.base.rsplit("/api", 1)[0],
        "exported_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "permission_schemes": [
            {"name": s["name"], "type": s.get("type")} for s in schemes
        ],
        "ous": _export_ous(client, scheme_id_to_name),
    }
    snapshot["billing_sources"] = _export_billing_sources(client)
    snapshot["funding_sources"] = _export_funding_sources(client)
    snapshot["projects"] = _export_projects(client)
    snapshot["budgets"] = _export_budgets(client, snapshot["ous"], snapshot["projects"])
    snapshot["scopes"] = _export_scopes(client)
    return snapshot


# Maps the type-specific key in a /v4/billing-source record to a short type name.
BILLING_TYPE_KEYS = {
    "aws_payer": "aws",
    "gcp_payer": "gcp",
    "azure_payer": "azure",
    "oci_payer": "oci",
    "custom_billing_source": "custom",
    "anthropic_billing_source": "anthropic",
}


def _export_billing_sources(client) -> list[dict]:
    """Export billing sources (/v4/billing-source). The read API exposes connection
    config but never secrets (keys/role trust are redacted), so these recreate as
    non-functional shells — see import for which types can be recreated at all.
    """
    try:
        resp = client.get("/v4/billing-source")
    except KionAPIError as e:
        _warn(f"billing-source list failed: {e.status}")
        return []
    items = resp.get("items") if isinstance(resp, dict) else resp
    out = []
    for b in items or []:
        type_key = next((k for k in BILLING_TYPE_KEYS if k in b), None)
        inner = b.get(type_key) or {}
        # GCP nests its name under the billing account.
        name = inner.get("name") or (inner.get("gcp_billing_account") or {}).get("name")
        out.append(
            {
                "source_id": b.get("id"),
                "type": BILLING_TYPE_KEYS.get(type_key, "unknown"),
                "name": name,
                "account_creation": b.get("account_creation"),
                "use_focus_reports": b.get("use_focus_reports"),
                "use_proprietary_reports": b.get("use_proprietary_reports"),
                "config": inner or {},
            }
        )
    by_type = {}
    for b in out:
        by_type[b["type"]] = by_type.get(b["type"], 0) + 1
    print(f"  billing sources: {len(out)} {dict(by_type)}")
    return out


def _export_ous(client, scheme_id_to_name) -> list[dict]:
    ous = client.get("/v3/ou") or []
    print(f"  OUs: {len(ous)}")
    out = []
    for ou in ous:
        owners_u, owners_g = [], []
        try:
            detail = client.get(f"/v3/ou/{ou['id']}") or {}
            owners_u = _owner_emails(detail.get("owner_users"))
            owners_g = _owner_group_names(detail.get("owner_user_groups"))
        except KionAPIError as e:
            _warn(f"OU {ou['id']} ({ou.get('name')}): could not read owners: {e.status}")
        out.append(
            {
                "id": ou["id"],
                "name": ou.get("name"),
                "description": ou.get("description"),
                "parent_ou_id": ou.get("parent_ou_id"),
                "permission_scheme_name": _scheme_name(
                    ou.get("permission_scheme_id"), scheme_id_to_name
                ),
                "owner_user_emails": owners_u,
                "owner_user_group_names": owners_g,
            }
        )
    return out


def _export_funding_sources(client) -> list[dict]:
    # The flat list returns ``ou_id`` (omitempty) — the OU the funding source was
    # created against. It is populated only when an allocation transaction exists
    # (i.e. allocations mode); in allocations-off installs funding sources are
    # global at the root and ou_id is absent, so they default to the target root on
    # import. Permission scheme and owners are not exposed by the read API.
    fss = client.get("/v3/funding-source") or []
    out = []
    for fs in fss:
        out.append(
            {
                "id": fs["id"],
                "name": fs.get("name"),
                "description": fs.get("description"),
                "amount": str(fs.get("amount")) if fs.get("amount") is not None else None,
                "start_datecode": fs.get("start_datecode"),
                "end_datecode": fs.get("end_datecode"),
                "ou_id": fs.get("ou_id") or None,
                "permission_scheme_name": None,
                "owner_user_emails": [],
                "owner_user_group_names": [],
            }
        )
    print(f"  funding sources: {len(out)} ({sum(1 for f in out if f['ou_id'])} with explicit OU)")
    return out


def _export_projects(client) -> list[dict]:
    # As with funding sources, the read API does not expose a project's permission
    # scheme or owners; resolved to the target's default project scheme on import.
    projects = client.get("/v3/project") or []
    print(f"  projects: {len(projects)}")
    out = []
    for p in projects:
        out.append(
            {
                "id": p["id"],
                "name": p.get("name"),
                "description": p.get("description"),
                "ou_id": p.get("ou_id"),
                "auto_pay": p.get("auto_pay"),
                "default_aws_region": p.get("default_aws_region"),
                "permission_scheme_name": None,
                "owner_user_emails": [],
                "owner_user_group_names": [],
            }
        )
    return out


def _normalize_budget(raw: dict, ou_id, project_id) -> dict:
    """Map a read Budget ({config, data}) into a portable record."""
    config = raw.get("config") or {}
    data = []
    for d in raw.get("data") or []:
        data.append(
            {
                "amount": str(d.get("amount")) if d.get("amount") is not None else None,
                "datecode": d.get("datecode"),
                "funding_source_id": d.get("funding_source_id"),
                "priority": d.get("priority"),
            }
        )
    return {
        "source_budget_id": config.get("id"),
        "name": config.get("name"),
        "ou_id": ou_id,
        "project_id": project_id,
        "start_datecode": config.get("start_datecode"),
        "end_datecode": config.get("end_datecode"),
        "data": data,
    }


def _export_budgets(client, ous, projects) -> list[dict]:
    out = []
    for ou in ous:
        try:
            budgets = client.get(f"/v3/ou/{ou['id']}/budget") or []
        except KionAPIError as e:
            _warn(f"OU {ou['id']} budgets read failed: {e.status}")
            continue
        for b in budgets:
            out.append(_normalize_budget(b, ou_id=ou["id"], project_id=None))
    for p in projects:
        try:
            budgets = client.get(f"/v3/project/{p['id']}/budget") or []
        except KionAPIError as e:
            _warn(f"project {p['id']} budgets read failed: {e.status}")
            continue
        for b in budgets:
            out.append(_normalize_budget(b, ou_id=None, project_id=p["id"]))
    print(f"  budgets: {len(out)}")
    return out


def _active_criteria(scope: dict) -> dict:
    """The currently-active criteria JSON for a scope (or the latest record)."""
    rec = scope.get("active_criteria_record")
    if not rec:
        records = scope.get("criteria_records") or []
        rec = records[-1] if records else {}
    return (rec or {}).get("criteria") or {}


def _export_scopes(client) -> list[dict]:
    """Export project scopes (/beta/scope), translating account ids to stable
    account numbers so they can be re-resolved on the target.

    Scope criteria require at least one real cloud account, so on import a scope is
    only created where its accounts (by number) exist on the target.
    """
    # source account id -> account number (the stable cross-install key)
    try:
        accounts = client.get("/v3/account") or []
    except KionAPIError as e:
        _warn(f"account list failed ({e.status}); scope account refs may be lost")
        accounts = []
    acct_id_to_number = {a["id"]: a.get("account_number") for a in accounts}

    items = []
    page = 1
    while True:
        try:
            resp = client.get("/beta/scope", params={"page": page, "count": 100})
        except KionAPIError as e:
            _warn(f"scope list failed: {e.status}")
            break
        if isinstance(resp, dict):
            batch = resp.get("items") or []
            items.extend(batch)
            total = resp.get("total", len(items))
            if len(items) >= total or not batch:
                break
            page += 1
        else:  # unexpected shape; take what we got
            items.extend(resp or [])
            break

    out = []
    for s in items:
        criteria = dict(_active_criteria(s))
        ac = dict(criteria.get("account_criteria") or {})
        src_ids = ac.get("account_ids") or []
        account_numbers = [acct_id_to_number.get(i) for i in src_ids if acct_id_to_number.get(i)]
        # store the account refs as numbers; ids get rebuilt on import
        ac.pop("account_ids", None)
        criteria["account_criteria"] = ac
        out.append(
            {
                "source_scope_id": s.get("id"),
                "name": s.get("name"),
                "alias": s.get("alias"),
                "description": s.get("description"),
                "project_id": s.get("project_id"),
                "start_datecode": s.get("start_datecode"),
                "end_datecode": s.get("end_datecode"),
                "account_numbers": account_numbers,
                "criteria": criteria,
            }
        )
    print(f"  scopes: {len(out)}")
    return out
