"""Reconcile a snapshot into a target Kion install (terraform-style).

``id-map.json`` is the **state file**: it records, per snapshot entity, the target
id it maps to. Re-running only acts on the difference between the snapshot and what
already exists, so ``--apply`` is safe to run repeatedly.

For each entity we choose an action:

  OK       already in the state file and still present on the target -> nothing
  ADOPT    not in state but an entity with the same natural key exists on the
           target -> record the mapping (like ``terraform import``); no write
  CREATE   not in state and not on the target -> POST it
  RECREATE in state but the target id is gone -> POST it again
  DRIFT    present but some fields differ from the snapshot -> reported only
           (existing data is never modified)

Adoption keys on natural identity (name within parent/OU scope), so a target that
already contains the environment -- e.g. re-pointing at the source itself -- yields
zero creates instead of duplicates. The numeric ids in the snapshot come from the
SOURCE and are remapped to target ids throughout.
"""
from __future__ import annotations

import sys

from .client import KionAPIError, KionClient

# ---------------------------------------------------------------------------
# Pure helpers (no client/network — unit tested directly)
# ---------------------------------------------------------------------------

TYPE_DEFAULT_SCHEME = {
    "ou": "Default OU Permissions Scheme",
    "project": "Default Project Permissions Scheme",
    "funding": "Default Funding Source Permissions Scheme",
}


def nkey(name) -> str:
    """Normalized natural key for a name (case/space-insensitive)."""
    return (name or "").strip().lower()


def resolve_scheme(name, schemes: dict, type_default_name, default_id):
    """Resolve a permission scheme id, returning (scheme_id, status).

    Order: exact name -> stock per-type default scheme -> DEFAULT_PERMISSION_SCHEME_ID
    -> unresolved. status: matched | type_default | default | unresolved.
    """
    if name and name in schemes:
        return schemes[name], "matched"
    if type_default_name and type_default_name in schemes:
        return schemes[type_default_name], "type_default"
    if default_id is not None:
        return default_id, "default"
    return None, "unresolved"


def resolve_owners(emails, group_names, users: dict, groups: dict):
    """Return (user_ids, group_ids, dropped) resolving by email/name; skip missing."""
    user_ids, group_ids, dropped = [], [], []
    for e in emails or []:
        uid = users.get((e or "").lower())
        if uid is not None:
            user_ids.append(uid)
        else:
            dropped.append(f"user:{e}")
    for n in group_names or []:
        gid = groups.get(n)
        if gid is not None:
            group_ids.append(gid)
        else:
            dropped.append(f"group:{n}")
    return user_ids, group_ids, dropped


def find_root_ou_id(ous: list[dict]):
    for ou in ous:
        if ou.get("parent_ou_id") in (None, 0):
            return ou["id"]
    return None


def order_ous(ous: list[dict]) -> list[dict]:
    """Topological order: every OU appears after its parent."""
    by_id = {ou["id"]: ou for ou in ous}
    ordered, seen = [], set()

    def visit(ou):
        oid = ou["id"]
        if oid in seen:
            return
        parent = ou.get("parent_ou_id")
        if parent in by_id and parent not in seen:
            visit(by_id[parent])
        seen.add(oid)
        ordered.append(ou)

    for ou in ous:
        visit(ou)
    return ordered


def _amounts_equal(a, b) -> bool:
    """Compare two money values that may be int/str/float."""
    try:
        return round(float(a)) == round(float(b))
    except (TypeError, ValueError):
        return str(a) == str(b)


def _datecode_ordinal(dc) -> int | None:
    """'YYYY-MM' -> month ordinal (0-based month) for gap math, or None."""
    try:
        y, m = str(dc).split("-")
        return int(y) * 12 + (int(m) - 1)
    except (ValueError, AttributeError):
        return None


def _ordinal_to_datecode(o: int) -> str:
    return f"{o // 12:04d}-{o % 12 + 1:02d}"


def _missing_budget_months(budget: dict) -> list[str]:
    """Months in [start, end) with no data row — the 'timeframe not covered' cause."""
    start = _datecode_ordinal(budget.get("start_datecode"))
    end = _datecode_ordinal(budget.get("end_datecode"))
    if start is None or end is None:
        return []
    have = {_datecode_ordinal(d.get("datecode")) for d in budget.get("data", [])}
    return [_ordinal_to_datecode(o) for o in range(start, end) if o not in have]


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

ACTIONS = ("ok", "adopt", "create", "recreate")
KINDS = ("billing_sources", "ous", "funding_sources", "projects", "budgets",
         "accounts", "scopes")

# Billing source types that have an API create path (others can only be set up via
# a provider flow / prerequisite entity and are skipped on import).
BILLING_CREATABLE = {"custom", "aws", "oci"}

# The read account_number holds a provider-specific identifier; map it to the field
# each create endpoint expects.
ACCOUNT_NUMBER_FIELD = {
    "aws": "account_number",
    "custom": "account_number",
    "google-cloud": "google_cloud_project_id",
    "azure": "subscription_uuid",
    "oci": "tenancy_ocid",
}


def account_project_payload(a: dict, proj_new, payer_new):
    """Build (path, payload) to create a cloud account *associated with a project*
    via ``/v3/account?account-type={provider}``. Requires project_id + payer_id +
    start_datecode (verified against the *AccountCreate swagger schemas)."""
    provider = a.get("provider") or "custom"
    num_field = ACCOUNT_NUMBER_FIELD.get(provider, "account_number")
    payload = {
        num_field: a.get("account_number"),
        "account_name": a.get("account_name"),
        "project_id": proj_new,
        "payer_id": payer_new,
        "start_datecode": a.get("start_datecode"),
    }
    if a.get("account_alias"):
        payload["account_alias"] = a["account_alias"]
    if provider != "custom":  # the custom create endpoint rejects account_type_id
        payload["account_type_id"] = a.get("account_type_id")
    if provider in ("aws", "azure", "google-cloud"):
        payload["skip_access_checking"] = True
    if provider == "aws" and a.get("linked_account_number"):
        # required for linked AWS accounts (e.g. GovCloud paired to a commercial account)
        payload["linked_aws_account_number"] = a["linked_account_number"]
        if a.get("include_linked_account_spend") is not None:
            payload["include_linked_account_spend"] = a["include_linked_account_spend"]
    return f"/v3/account?account-type={provider}", payload


def account_cache_payload(a: dict, payer_new):
    """Build (path, payload) to create a cloud account in the target's *account
    cache* (unassociated with any project) via ``/v3/account-cache?account-type=…``.

    The cache create family needs only a payer (billing source) — no project_id or
    start_datecode. Field rules verified against the *AccountCacheCreate swagger
    schemas: ``account_type_id`` is required for aws and accepted for azure/gcp/oci
    but ABSENT for custom; ``skip_access_checking`` exists for all but custom; the
    aws linked field is ``linked_account_number`` (not ``linked_aws_account_number``).
    """
    provider = a.get("provider") or "custom"
    num_field = ACCOUNT_NUMBER_FIELD.get(provider, "account_number")
    payload = {
        num_field: a.get("account_number"),
        "account_name": a.get("account_name"),
        "payer_id": payer_new,
    }
    if a.get("account_alias"):
        payload["account_alias"] = a["account_alias"]
    if provider != "custom":  # custom cache create has no account_type_id/skip_access_checking
        payload["account_type_id"] = a.get("account_type_id")
        payload["skip_access_checking"] = True
    if provider == "aws" and a.get("linked_account_number"):
        payload["linked_account_number"] = a["linked_account_number"]
        if a.get("include_linked_account_spend") is not None:
            payload["include_linked_account_spend"] = a["include_linked_account_spend"]
    return f"/v3/account-cache?account-type={provider}", payload


def parse_only(only, kinds=KINDS):
    """Normalize/validate a ``--only`` selection into a set of kinds, or None (all).

    Raises ValueError on an unknown kind or a selection whose hard dependency is
    unmet (accounts need a payer billing source, so 'accounts' requires
    'billing_sources')."""
    if not only:
        return None
    if isinstance(only, str):
        only = only.split(",")
    sel = {k.strip() for k in only if k and k.strip()}
    if not sel:
        return None
    unknown = sel - set(kinds)
    if unknown:
        raise ValueError(
            f"unknown kind(s): {', '.join(sorted(unknown))}. "
            f"valid kinds: {', '.join(kinds)}")
    if "accounts" in sel and "billing_sources" not in sel:
        raise ValueError(
            "'accounts' requires 'billing_sources' in --only "
            "(accounts need a payer billing source to exist on the target)")
    return sel


class Importer:
    def __init__(self, client: KionClient, config, snapshot: dict, apply: bool,
                 id_map: dict | None = None, only=None):
        self.client = client
        self.config = config
        self.snapshot = snapshot
        self.apply = apply
        self.id_map = id_map or {}
        for k in KINDS:
            self.id_map.setdefault(k, {})
        # None = reconcile all kinds; otherwise a validated subset (see parse_only).
        self.only = parse_only(only)

        # target lookups (populated in run)
        self.schemes: dict = {}
        self.users: dict = {}
        self.groups: dict = {}
        self.target_root_id = None
        self.current_user_id = None
        self._owner_fallback = 0
        self._t_ou_key: dict = {}
        self._t_ou_ids: set = set()
        self._t_ou_by_id: dict = {}
        self._t_fs_key: dict = {}
        self._t_fs_by_id: dict = {}
        self._t_proj_key: dict = {}
        self._t_proj_by_id: dict = {}
        self._t_budget_cache: dict = {}  # budget scope-id -> {natural_key: budget_id}
        self._t_acct_by_number: dict = {}  # account_number -> target account id
        self._t_acct_ids: set = set()
        self._t_scope_key: dict = {}       # (project_id, name) -> scope id
        self._t_scope_ids: set = set()
        self._t_billing_key: dict = {}     # name -> billing source id
        self._t_billing_ids: set = set()

        self._placeholder = 0
        self._last_error = None       # last KionAPIError from _post (for diagnostics)
        self._pinned_path: dict = {}  # kind -> endpoint that worked (avoids re-probing)
        self.warnings: list[str] = []
        self.drift: list[str] = []
        # counts[kind][action]
        self.counts = {k: dict.fromkeys(ACTIONS, 0) for k in KINDS}
        self.failed = dict.fromkeys(KINDS, 0)   # real API errors on create
        self.skipped = dict.fromkeys(KINDS, 0)  # couldn't map a reference (expected)

    # -- low level --------------------------------------------------------
    def _placeholder_id(self, kind: str, src) -> str:
        self._placeholder += 1
        return f"<new:{kind}:{src}>"

    @staticmethod
    def _extract_id(resp):
        if isinstance(resp, dict):
            for key in ("record_id", "id"):
                if key in resp:
                    return resp[key]
        return resp

    def _post(self, kind: str, path, payload: dict, src, action: str, label: str):
        """Create on apply, or return a placeholder in plan mode. Records state.

        ``path`` may be a list of candidate endpoints tried in order — used where
        the right endpoint depends on the install's financial mode (e.g. projects:
        ``/v3/project`` in spend-plan mode vs ``/v3/project/with-budget`` in budget
        mode). The first endpoint that succeeds is pinned for this ``kind`` so the
        rest of the run doesn't re-probe.
        """
        paths = [path] if isinstance(path, str) else list(path)
        self._last_error = None
        if not self.apply:
            print(f"  ~ {action} {label}")
            self.counts[kind][action] += 1
            return self._placeholder_id(kind, src)

        if self._pinned_path.get(kind) in paths:
            paths = [self._pinned_path[kind]] + [p for p in paths if p != self._pinned_path[kind]]
        last_err = None
        for p in paths:
            try:
                new_id = self._extract_id(self.client.post(p, json=payload))
            except KionAPIError as e:
                last_err = e
                continue
            self._pinned_path[kind] = p
            print(f"  + {action} {label} -> id {new_id}")
            self.counts[kind][action] += 1
            return new_id
        self._last_error = last_err
        self.warnings.append(f"{label}: {action} failed: {last_err}")
        self.failed[kind] += 1
        return None

    def _resolve_scheme(self, name, entity_type: str, label: str):
        type_default = TYPE_DEFAULT_SCHEME.get(entity_type)
        sid, status = resolve_scheme(name, self.schemes, type_default,
                                     self.config.default_permission_scheme_id)
        if status == "type_default":
            self.warnings.append(f"{label}: using '{type_default}' (id {sid})")
        elif status == "default":
            self.warnings.append(f"{label}: scheme '{name}' not on target -> DEFAULT id {sid}")
        elif status == "unresolved":
            self.warnings.append(
                f"{label}: no permission scheme resolvable (no '{type_default}', no DEFAULT)"
            )
        return sid, status

    def _resolve_owners(self, rec, label: str):
        uids, gids, dropped = resolve_owners(
            rec.get("owner_user_emails"), rec.get("owner_user_group_names"),
            self.users, self.groups,
        )
        for d in dropped:
            self.warnings.append(f"{label}: dropped owner {d} (not on target)")
        # OU/funding/project create requires at least one owner. When none was
        # captured or none resolved on the target, fall back to the user running
        # the import (the API key's owner).
        if not uids and not gids and self.current_user_id is not None:
            uids = [self.current_user_id]
            self._owner_fallback += 1
        return uids, gids

    # -- target indexing --------------------------------------------------
    def _index_target(self):
        self.schemes = {s["name"]: s["id"] for s in (self.client.get("/v3/permission-scheme") or [])}
        self.users = {u["email"].lower(): u["id"]
                      for u in (self.client.get("/v3/user") or []) if u.get("email")}
        self.groups = {g["name"]: g["id"]
                       for g in (self.client.get("/v3/user-group") or []) if g.get("name")}

        # The user running the import — used as the fallback owner. App API keys
        # are user-scoped, so any key the call returns belongs to this user.
        try:
            keys = self.client.get("/v3/app-api-key") or []
            self.current_user_id = keys[0].get("user_id") if keys else None
        except KionAPIError:
            self.current_user_id = None

        ous = self.client.get("/v3/ou") or []
        self.target_root_id = find_root_ou_id(ous)
        self._t_ou_ids = {o["id"] for o in ous}
        self._t_ou_by_id = {o["id"]: o for o in ous}
        self._t_ou_key = {(o.get("parent_ou_id"), nkey(o.get("name"))): o["id"] for o in ous}

        fss = self.client.get("/v3/funding-source") or []
        self._t_fs_by_id = {f["id"]: f for f in fss}
        self._t_fs_key = {nkey(f.get("name")): f["id"] for f in fss}

        projs = self.client.get("/v3/project") or []
        self._t_proj_by_id = {p["id"]: p for p in projs}
        self._t_proj_key = {(p.get("ou_id"), nkey(p.get("name"))): p["id"] for p in projs}

        accts = self.client.get("/v3/account") or []
        self._t_acct_ids = {a["id"] for a in accts}
        self._t_acct_by_number = {a.get("account_number"): a["id"]
                                  for a in accts if a.get("account_number")}
        # Union in the account cache (unassociated accounts). An account is either
        # associated or cached, never both, so keying both by account_number lets a
        # re-apply adopt an already-copied account wherever it currently sits instead
        # of duplicating it. (Cache ids live in a separate space; used only for the
        # weak liveness check in _reconcile_accounts.)
        for a in self._list_account_cache():
            self._t_acct_ids.add(a["id"])
            if a.get("account_number"):
                self._t_acct_by_number.setdefault(a.get("account_number"), a["id"])

        for s in self._list_scopes():
            self._t_scope_ids.add(s["id"])
            self._t_scope_key[(s.get("project_id"), nkey(s.get("name")))] = s["id"]

        for b in self._list_billing_sources():
            self._t_billing_ids.add(b["id"])
            inner = next((b[k] for k in (
                "aws_payer", "gcp_payer", "azure_payer", "oci_payer",
                "custom_billing_source", "anthropic_billing_source") if k in b), {}) or {}
            name = inner.get("name") or (inner.get("gcp_billing_account") or {}).get("name")
            if name:
                self._t_billing_key[nkey(name)] = b["id"]

        print(f"  target: {len(self.schemes)} schemes, {len(self.users)} users, "
              f"{len(self.groups)} groups, {len(ous)} OUs, {len(fss)} funding, {len(projs)} projects")

    def _list_scopes(self) -> list:
        """All scopes on the target (paginated /beta/scope -> {items,total})."""
        items, page = [], 1
        while True:
            try:
                resp = self.client.get("/beta/scope", params={"page": page, "count": 100})
            except KionAPIError as e:
                self.warnings.append(f"target scope list failed: {e.status}")
                break
            if not isinstance(resp, dict):
                items.extend(resp or [])
                break
            batch = resp.get("items") or []
            items.extend(batch)
            if len(items) >= resp.get("total", len(items)) or not batch:
                break
            page += 1
        return items

    def _list_billing_sources(self) -> list:
        try:
            resp = self.client.get("/v4/billing-source")
        except KionAPIError as e:
            self.warnings.append(f"target billing-source list failed: {e.status}")
            return []
        return (resp.get("items") if isinstance(resp, dict) else resp) or []

    def _list_account_cache(self) -> list:
        """Unassociated accounts on the target (/v3/account-cache). Handles a bare
        list or an {items,total} envelope (the swagger response body is unspecified)."""
        try:
            resp = self.client.get("/v3/account-cache")
        except KionAPIError as e:
            self.warnings.append(f"target account-cache list failed: {e.status}")
            return []
        return (resp.get("items") if isinstance(resp, dict) else resp) or []

    def _target_budgets_for(self, kind: str, tgt_scope_id) -> dict:
        """natural_key -> budget_id for budgets on a target OU/project (cached)."""
        cache_key = f"{kind}:{tgt_scope_id}"
        if cache_key in self._t_budget_cache:
            return self._t_budget_cache[cache_key]
        path = (f"/v3/ou/{tgt_scope_id}/budget" if kind == "ou"
                else f"/v3/project/{tgt_scope_id}/budget")
        index = {}
        try:
            for b in (self.client.get(path) or []):
                cfg = b.get("config") or {}
                # Budget names are auto-generated by Kion from the timeframe (and
                # differ across versions), so identity is the date range, not name.
                index[(cfg.get("start_datecode"), cfg.get("end_datecode"))] = cfg.get("id")
        except KionAPIError as e:
            self.warnings.append(f"could not read target budgets for {cache_key}: {e.status}")
        self._t_budget_cache[cache_key] = index
        return index

    # -- run --------------------------------------------------------------
    def _enabled(self, kind: str) -> bool:
        return self.only is None or kind in self.only

    def run(self) -> dict:
        mode = "APPLY" if self.apply else "PLAN (no changes written)"
        print(f"\n=== Reconcile [{mode}] -> {self.config.url} ===")
        if self.only is not None:
            print(f"  (--only: {', '.join(k for k in KINDS if k in self.only)})")
        self._index_target()
        if self.target_root_id is None:
            self.warnings.append("could not find target root OU; OUs may fail")

        passes = {
            "billing_sources": self._reconcile_billing_sources,
            "ous": self._reconcile_ous,
            "funding_sources": self._reconcile_funding_sources,
            "projects": self._reconcile_projects,
            "budgets": self._reconcile_budgets,
            "accounts": self._reconcile_accounts,
            "scopes": self._reconcile_scopes,
        }
        for kind in KINDS:
            if self._enabled(kind):
                passes[kind]()
        self._summary()
        return self.id_map

    # -- billing sources --------------------------------------------------
    @staticmethod
    def _billing_payload(bs: dict):
        """Build (path, payload) to recreate a billing source as a shell, or
        (None, reason) when the type has no API recreate path. Secrets/trust the
        read API redacts are sent as placeholders the customer replaces on the target.
        """
        PLACEHOLDER = "REPLACE-ON-TARGET"
        cfg = bs.get("config") or {}
        name = bs.get("name")
        common = {
            "skip_validation": True,
            "use_focus_reports": bs.get("use_focus_reports"),
            "use_proprietary_reports": bs.get("use_proprietary_reports"),
        }
        t = bs.get("type")
        if t == "custom":
            return "/v3/billing-source/custom", {
                "name": name,
                "billing_start_date": cfg.get("billing_start_date"),
                "aws_connection": cfg.get("aws_connection") or {},
                "skip_validation": True,
            }
        if t == "aws":
            # AWS govcloud regions use a different account type; default to standard.
            acct_type = 2 if str(cfg.get("billing_region", "")).startswith("us-gov") else 1
            return "/v3/billing-source/aws", {
                **common,
                "account_type_id": acct_type,
                "name": name,
                "aws_account_number": cfg.get("account_number"),
                "billing_start_date": cfg.get("billing_start_date"),
                "billing_region": cfg.get("billing_region"),
                "billing_report_type": cfg.get("billing_report_type"),
                "cur_bucket": cfg.get("billing_report_bucket"),
                "cur_bucket_region": cfg.get("billing_report_bucket_region"),
                "cur_name": cfg.get("billing_report_name"),
                "cur_prefix": cfg.get("billing_report_prefix"),
                "bucket_access_role": cfg.get("bucket_access_role"),
                "billing_bucket_account_number": cfg.get("billing_bucket_account_number"),
                "linked_role": PLACEHOLDER,  # not exposed by the read API
                "account_creation": False,
            }
        if t == "oci":
            return "/v3/billing-source/oci", {
                "name": name,
                "account_type_id": cfg.get("account_type_id"),
                "billing_start_date": cfg.get("billing_start_date"),
                "tenancy_ocid": cfg.get("tenancy_ocid"),
                "user_ocid": cfg.get("user_ocid"),
                "region": cfg.get("region"),
                "is_parent_tenancy": cfg.get("is_parent_tenancy"),
                "fingerprint": cfg.get("fingerprint") or PLACEHOLDER,
                "private_key": cfg.get("private_key") or PLACEHOLDER,
                "skip_validation": True,
            }
        reasons = {
            "gcp": "GCP billing sources require a GCP service account on the target",
            "azure": "Azure billing sources are set up via the CSP/EA registration flow",
            "anthropic": "Anthropic billing sources require an API key (not exposed)",
        }
        return None, reasons.get(t, f"no API recreate path for type '{t}'")

    def _reconcile_billing_sources(self):
        print("\nBilling sources:")
        for bs in self.snapshot.get("billing_sources", []):
            src = bs.get("source_id")
            label = f"billing source '{bs.get('name')}' ({bs.get('type')})"
            key = nkey(bs.get("name"))

            mapped = self.id_map["billing_sources"].get(str(src))
            if mapped is not None and mapped in self._t_billing_ids:
                self._note_ok("billing_sources", label)
                continue
            if key in self._t_billing_key:
                found = self._t_billing_key[key]
                self.id_map["billing_sources"][str(src)] = found
                self.counts["billing_sources"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                continue

            path, payload = self._billing_payload(bs)
            if path is None:
                self.warnings.append(f"{label}: skipped — {payload}")
                self.skipped["billing_sources"] += 1
                continue
            action = "recreate" if mapped is not None else "create"
            new_id = self._post("billing_sources", path, payload, src, action, label)
            if new_id is not None:
                self.id_map["billing_sources"][str(src)] = new_id

    # -- OUs --------------------------------------------------------------
    def _reconcile_ous(self):
        print("\nOUs:")
        ous = self.snapshot.get("ous", [])
        src_root = find_root_ou_id(ous)
        # If the target already has a root, the source root maps onto it and is not
        # recreated. If the target has NO root (e.g. a freshly wiped install), the
        # source root is created as a top-level OU (parent_ou_id=0) below.
        have_target_root = self.target_root_id is not None
        if src_root is not None and have_target_root:
            self.id_map["ous"][str(src_root)] = self.target_root_id

        for ou in order_ous(ous):
            src = ou["id"]
            is_root = src == src_root
            if is_root and have_target_root:
                continue
            # A null/0 parent (or the source root onto a rootless target) is
            # top-level: hang it off the existing target root, or pass 0 to mint
            # a new top-level OU.
            praw = ou.get("parent_ou_id")
            if is_root or praw in (None, 0):
                parent_new = self.target_root_id if self.target_root_id is not None else 0
            else:
                parent_new = self.id_map["ous"].get(str(praw))
            if parent_new is None:
                self.warnings.append(
                    f"OU '{ou.get('name')}': parent {praw} unresolved, skipped")
                self.skipped["ous"] += 1
                continue

            label = f"OU '{ou.get('name')}'"
            key = (parent_new, nkey(ou.get("name")))

            # OK: already mapped and present
            mapped = self.id_map["ous"].get(str(src))
            if mapped is not None and mapped in self._t_ou_ids:
                self._note_ok("ous", label)
                self._ou_drift(ou, self._t_ou_by_id.get(mapped), label)
                continue
            # ADOPT: same parent+name already on target
            if key in self._t_ou_key:
                found = self._t_ou_key[key]
                self.id_map["ous"][str(src)] = found
                self.counts["ous"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                self._ou_drift(ou, self._t_ou_by_id.get(found), label)
                continue
            # CREATE / RECREATE
            action = "recreate" if mapped is not None else "create"
            scheme_id, status = self._resolve_scheme(ou.get("permission_scheme_name"), "ou", label)
            if status == "unresolved":
                self.skipped["ous"] += 1
                continue
            uids, gids = self._resolve_owners(ou, label)
            payload = {
                "name": ou.get("name"),
                "description": ou.get("description") or "",
                "parent_ou_id": parent_new,
                "permission_scheme_id": scheme_id,
                "owner_user_ids": uids,
                "owner_user_group_ids": gids,
            }
            new_id = self._post("ous", "/v3/ou", payload, src, action, label)
            if new_id is not None:
                self.id_map["ous"][str(src)] = new_id
                # Anchor the rest of the import to the freshly-created root.
                if is_root and self.target_root_id is None:
                    self.target_root_id = new_id

    def _ou_drift(self, src_ou, tgt_ou, label):
        if tgt_ou and (src_ou.get("description") or "") != (tgt_ou.get("description") or ""):
            self.drift.append(f"{label}: description differs")

    # -- funding sources --------------------------------------------------
    def _reconcile_funding_sources(self):
        print("\nFunding sources:")
        for fs in self.snapshot.get("funding_sources", []):
            src = fs["id"]
            label = f"funding source '{fs.get('name')}'"
            key = nkey(fs.get("name"))

            mapped = self.id_map["funding_sources"].get(str(src))
            if mapped is not None and mapped in self._t_fs_by_id:
                self._note_ok("funding_sources", label)
                self._fs_drift(fs, self._t_fs_by_id.get(mapped), label)
                continue
            if key in self._t_fs_key:
                found = self._t_fs_key[key]
                self.id_map["funding_sources"][str(src)] = found
                self.counts["funding_sources"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                self._fs_drift(fs, self._t_fs_by_id.get(found), label)
                continue

            action = "recreate" if mapped is not None else "create"
            ou_new = self.id_map["ous"].get(str(fs.get("ou_id"))) or self.target_root_id
            if ou_new is None:
                self.warnings.append(f"{label}: no target OU, skipped")
                self.skipped["funding_sources"] += 1
                continue
            if not self.id_map["ous"].get(str(fs.get("ou_id"))):
                self.warnings.append(f"{label}: OU unknown -> target root OU {ou_new}")
            scheme_id, status = self._resolve_scheme(fs.get("permission_scheme_name"), "funding", label)
            if status == "unresolved":
                self.skipped["funding_sources"] += 1
                continue
            uids, gids = self._resolve_owners(fs, label)
            payload = {
                "name": fs.get("name"),
                "description": fs.get("description") or "",
                "amount": str(fs.get("amount")) if fs.get("amount") is not None else "0",
                "start_datecode": fs.get("start_datecode"),
                "end_datecode": fs.get("end_datecode"),
                "ou_id": ou_new,
                "permission_scheme_id": scheme_id,
                "owner_user_ids": uids,
                "owner_user_group_ids": gids,
            }
            new_id = self._post("funding_sources", "/v3/funding-source", payload, src, action, label)
            if new_id is not None:
                self.id_map["funding_sources"][str(src)] = new_id

    def _fs_drift(self, src_fs, tgt_fs, label):
        if not tgt_fs:
            return
        if not _amounts_equal(src_fs.get("amount"), tgt_fs.get("amount")):
            self.drift.append(f"{label}: amount differs ({src_fs.get('amount')} != {tgt_fs.get('amount')})")
        for f in ("start_datecode", "end_datecode"):
            if src_fs.get(f) != tgt_fs.get(f):
                self.drift.append(f"{label}: {f} differs")

    # -- projects ---------------------------------------------------------
    def _reconcile_projects(self):
        print("\nProjects:")
        for p in self.snapshot.get("projects", []):
            src = p["id"]
            label = f"project '{p.get('name')}'"
            ou_new = self.id_map["ous"].get(str(p.get("ou_id")))
            if ou_new is None:
                self.warnings.append(f"{label}: OU {p.get('ou_id')} unresolved, skipped")
                self.skipped["projects"] += 1
                continue
            key = (ou_new, nkey(p.get("name")))

            mapped = self.id_map["projects"].get(str(src))
            if mapped is not None and mapped in self._t_proj_by_id:
                self._note_ok("projects", label)
                self._proj_drift(p, self._t_proj_by_id.get(mapped), label)
                continue
            if key in self._t_proj_key:
                found = self._t_proj_key[key]
                self.id_map["projects"][str(src)] = found
                self.counts["projects"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                self._proj_drift(p, self._t_proj_by_id.get(found), label)
                continue

            action = "recreate" if mapped is not None else "create"
            scheme_id, status = self._resolve_scheme(p.get("permission_scheme_name"), "project", label)
            if status == "unresolved":
                self.skipped["projects"] += 1
                continue
            uids, gids = self._resolve_owners(p, label)
            payload = {
                "name": p.get("name"),
                "description": p.get("description") or "",
                "ou_id": ou_new,
                "permission_scheme_id": scheme_id,
                "owner_user_ids": uids,
                "owner_user_group_ids": gids,
            }
            if p.get("auto_pay") is not None:
                payload["auto_pay"] = p["auto_pay"]
            if p.get("default_aws_region"):
                payload["default_aws_region"] = p["default_aws_region"]
            # Spend-plan-mode installs accept /v3/project; budget-mode installs
            # require /v3/project/with-budget. Try both and pin the winner.
            new_id = self._post("projects", ["/v3/project", "/v3/project/with-budget"],
                                payload, src, action, label)
            if new_id is not None:
                self.id_map["projects"][str(src)] = new_id

    def _proj_drift(self, src_p, tgt_p, label):
        if not tgt_p:
            return
        for f in ("description", "auto_pay", "default_aws_region"):
            if (src_p.get(f) or "") != (tgt_p.get(f) or ""):
                self.drift.append(f"{label}: {f} differs")

    # -- budgets ----------------------------------------------------------
    def _funding_oversubscription(self) -> dict:
        """source funding_source_id -> (total_allocated, amount, budget_count, name)
        for funding sources whose total allocation across all snapshot budgets
        exceeds their amount (the usual cause of create-time 'insufficient funds')."""
        fs_amount, fs_name = {}, {}
        for f in self.snapshot.get("funding_sources", []):
            try:
                fs_amount[f["id"]] = float(f.get("amount") or 0)
            except (TypeError, ValueError):
                fs_amount[f["id"]] = 0.0
            fs_name[f["id"]] = f.get("name")
        total, count = {}, {}
        for b in self.snapshot.get("budgets", []):
            per = {}
            for d in b.get("data", []):
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

    def _diagnose_budget_failure(self, b: dict, label: str, oversub: dict):
        """Append a specific reason for a budget create failure."""
        err = (self._last_error.body if self._last_error else "") or ""
        if "timeframe not fully covered" in err:
            missing = _missing_budget_months(b)
            if missing:
                shown = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
                self.warnings.append(
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
                self.warnings.append(f"{label}: → cause: over-subscribed funding source — " + "; ".join(named))
            else:
                self.warnings.append(
                    f"{label}: → cause: the funding source(s) it draws from have "
                    f"insufficient unallocated funds in this window on the target")

    def _reconcile_budgets(self):
        print("\nBudgets:")
        oversub = self._funding_oversubscription()
        for i, b in enumerate(self.snapshot.get("budgets", [])):
            ou_new = self.id_map["ous"].get(str(b.get("ou_id"))) if b.get("ou_id") else None
            proj_new = self.id_map["projects"].get(str(b.get("project_id"))) if b.get("project_id") else None
            scope_kind = "ou" if ou_new is not None else "project"
            scope_id = ou_new if ou_new is not None else proj_new
            label = f"budget '{b.get('name') or b.get('source_budget_id')}'"
            if scope_id is None:
                self.warnings.append(f"{label}: target OU/project unresolved, skipped")
                self.skipped["budgets"] += 1
                continue

            state_key = f"{scope_kind}:{scope_id}:{b.get('start_datecode')}:{b.get('end_datecode')}"
            nat = (b.get("start_datecode"), b.get("end_datecode"))
            existing = self._target_budgets_for(scope_kind, scope_id)

            if state_key in self.id_map["budgets"] and nat in existing:
                self._note_ok("budgets", label)
                continue
            if nat in existing:
                self.id_map["budgets"][state_key] = existing[nat]
                self.counts["budgets"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {existing[nat]})")
                continue

            action = "recreate" if state_key in self.id_map["budgets"] else "create"
            data, missing_fs, fs_ids = [], set(), set()
            for row in b.get("data", []):
                entry = {
                    "amount": str(row.get("amount")) if row.get("amount") is not None else "0",
                    "datecode": row.get("datecode"),
                    "priority": row.get("priority") if row.get("priority") is not None else 0,
                }
                fs_src = row.get("funding_source_id")
                if fs_src not in (None, 0):
                    fs_new = self.id_map["funding_sources"].get(str(fs_src))
                    if fs_new is None:
                        missing_fs.add(fs_src)
                        continue
                    entry["funding_source_id"] = fs_new
                    fs_ids.add(fs_new)
                data.append(entry)
            for m in sorted(missing_fs):
                self.warnings.append(f"{label}: funding source {m} unresolved, row(s) dropped")
            if not data:
                self.warnings.append(f"{label}: no usable rows, skipped")
                self.skipped["budgets"] += 1
                continue

            payload = {
                "start_datecode": b.get("start_datecode"),
                "end_datecode": b.get("end_datecode"),
                "data": data,
            }
            if fs_ids:
                payload["funding_source_ids"] = sorted(fs_ids)
            if ou_new is not None:
                payload["ou_id"] = ou_new
            else:
                payload["project_id"] = proj_new
            new_id = self._post("budgets", "/v3/budget", payload, state_key, action, label)
            if new_id is not None:
                self.id_map["budgets"][state_key] = new_id
            else:
                self._diagnose_budget_failure(b, label, oversub)

    # -- accounts ---------------------------------------------------------
    def _reconcile_accounts(self):
        print("\nAccounts:")
        for a in self.snapshot.get("accounts", []):
            src = a.get("source_id")
            num = a.get("account_number")
            label = f"account '{a.get('account_name') or num}' ({a.get('provider')})"

            mapped = self.id_map["accounts"].get(str(src))
            if mapped is not None and mapped in self._t_acct_ids:
                self._note_ok("accounts", label)
                continue
            if num and num in self._t_acct_by_number:
                found = self._t_acct_by_number[num]
                self.id_map["accounts"][str(src)] = found
                self.counts["accounts"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                continue

            # Every create family requires the provider identifier (account_number /
            # subscription_uuid / …). A blank one can never be created (nor adopted),
            # so treat it as an expected skip rather than letting the API 400 it.
            if not num:
                self.warnings.append(f"{label}: no account_number, cannot create, skipped")
                self.skipped["accounts"] += 1
                continue

            # Payer (billing source) is required by BOTH create families; without it
            # the account can't be recreated at all (its billing source type wasn't
            # recreatable — azure/gcp/anthropic).
            payer_new = self.id_map["billing_sources"].get(str(a.get("payer_id")))
            if payer_new is None:
                self.warnings.append(
                    f"{label}: billing source {a.get('payer_id')} not on target "
                    f"(its type wasn't recreatable), skipped")
                self.skipped["accounts"] += 1
                continue

            # Resolve the project. If it resolves, associate the account to it; if it
            # doesn't (projects weren't synced, the source account was already
            # cache-only, or the project failed) put the account in the target's
            # account cache (unassociated) rather than dropping it.
            proj_src = a.get("project_id")
            proj_new = (self.id_map["projects"].get(str(proj_src))
                        if proj_src not in (None, 0) else None)
            action = "recreate" if mapped is not None else "create"
            if proj_new is not None:
                path, payload = account_project_payload(a, proj_new, payer_new)
            else:
                path, payload = account_cache_payload(a, payer_new)
                label += " (→ cache)"

            new_id = self._post("accounts", path, payload, src, action, label)
            if new_id is not None:
                self.id_map["accounts"][str(src)] = new_id
                if num:  # let scopes (which run next) resolve this account by number
                    self._t_acct_by_number[num] = new_id
                    self._t_acct_ids.add(new_id)

    # -- scopes -----------------------------------------------------------
    def _reconcile_scopes(self):
        print("\nScopes:")
        for s in self.snapshot.get("scopes", []):
            src = s.get("source_scope_id")
            label = f"scope '{s.get('name')}'"
            proj_new = self.id_map["projects"].get(str(s.get("project_id")))
            if proj_new is None:
                self.warnings.append(f"{label}: project {s.get('project_id')} unresolved, skipped")
                self.skipped["scopes"] += 1
                continue

            key = (proj_new, nkey(s.get("name")))
            mapped = self.id_map["scopes"].get(str(src))
            if mapped is not None and mapped in self._t_scope_ids:
                self._note_ok("scopes", label)
                continue
            if key in self._t_scope_key:
                found = self._t_scope_key[key]
                self.id_map["scopes"][str(src)] = found
                self.counts["scopes"]["adopt"] += 1
                print(f"  = adopt {label} (existing id {found})")
                continue

            # Account criteria require >=1 real account; remap by account_number.
            numbers = s.get("account_numbers") or []
            acct_ids, missing = [], []
            for n in numbers:
                tid = self._t_acct_by_number.get(n)
                (acct_ids if tid is not None else missing).append(tid if tid is not None else n)
            for m in missing:
                self.warnings.append(f"{label}: account {m} not on target, dropped")
            if not acct_ids:
                self.warnings.append(
                    f"{label}: none of its {len(numbers)} account(s) exist on target "
                    f"(scope needs >=1) — skipped")
                self.skipped["scopes"] += 1
                continue

            criteria = dict(s.get("criteria") or {})
            ac = dict(criteria.get("account_criteria") or {})
            ac["type"] = ac.get("type") or "account_ids"
            ac["account_ids"] = acct_ids
            criteria["account_criteria"] = ac

            action = "recreate" if mapped is not None else "create"
            payload = {
                "name": s.get("name"),
                "alias": s.get("alias") or "",
                "description": s.get("description") or "",
                "project_id": proj_new,
                "start_datecode": s.get("start_datecode"),
                "end_datecode": s.get("end_datecode"),
                "criteria": criteria,
            }
            new_id = self._post("scopes", "/beta/scope", payload, src, action, label)
            if new_id is not None:
                self.id_map["scopes"][str(src)] = new_id
            elif self._last_error and "Invalid scope criteria" in (self._last_error.body or ""):
                self.warnings.append(
                    f"{label}: → cause: a condition references a tag key / region / "
                    f"service the target hasn't ingested billing data for "
                    f"(create only succeeds once that data exists on the target)")

    # -- summary ----------------------------------------------------------
    def _note_ok(self, kind: str, label: str):
        self.counts[kind]["ok"] += 1

    def _summary(self):
        print("\n=== Plan ===" if not self.apply else "\n=== Result ===")
        totals = dict.fromkeys(ACTIONS, 0)
        fail_total = skip_total = 0
        for kind in KINDS:
            c = self.counts[kind]
            for a in ACTIONS:
                totals[a] += c[a]
            fail_total += self.failed[kind]
            skip_total += self.skipped[kind]
            print(f"  {kind:16} create {c['create']:4}  recreate {c['recreate']:3}  "
                  f"adopt {c['adopt']:4}  in-sync {c['ok']:4}  "
                  f"skipped {self.skipped[kind]:3}  failed {self.failed[kind]}")
        verb = "applied" if self.apply else "planned"
        extra = ""
        if skip_total:
            extra += f", {skip_total} skipped"
        if fail_total:
            extra += f", {fail_total} failed"
        print(f"\n  {totals['create']} to create, {totals['recreate']} to recreate, "
              f"{totals['adopt']} to adopt, {totals['ok']} in sync "
              f"({len(self.drift)} drifted){extra}  [{verb}]")
        if self._owner_fallback:
            print(f"  ({self._owner_fallback} entit{'y' if self._owner_fallback == 1 else 'ies'} "
                  f"assigned the importing user as owner — no owner captured/resolved)")

        if self.drift:
            print(f"\n  Drift ({len(self.drift)}) — existing entities differ; not modified:", file=sys.stderr)
            for d in self.drift[:50]:
                print(f"    ~ {d}", file=sys.stderr)
            if len(self.drift) > 50:
                print(f"    ... and {len(self.drift) - 50} more", file=sys.stderr)
        # Failures (and their cause lines) are few and important — always show them
        # in full, ahead of the higher-volume skip/info warnings.
        failures = [w for w in self.warnings if "failed:" in w or "→ cause:" in w]
        others = [w for w in self.warnings if w not in failures]
        if failures:
            print(f"\n  Failures ({len(failures)}):", file=sys.stderr)
            for w in failures:
                print(f"    - {w}", file=sys.stderr)
        if others:
            print(f"\n  Warnings ({len(others)}):", file=sys.stderr)
            for w in others[:50]:
                print(f"    - {w}", file=sys.stderr)
            if len(others) > 50:
                print(f"    ... and {len(others) - 50} more", file=sys.stderr)
        if not self.apply:
            print("\n  Plan only. Re-run with --apply to make these changes.")
