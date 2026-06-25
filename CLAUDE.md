# CLAUDE.md — kion-env-copy

Guidance for Claude (and humans) working in this repo. Read this before changing
export/import logic — much of it encodes Kion API behavior that is **not** obvious
from the swagger and was found by testing against live installs.

## What this tool does

Copies a Kion install's **financial/org structure** to another install over the
REST API:

```
billing sources → OUs → funding sources → projects → budgets → scopes
```

`export` reads a source install into a self-contained `snapshot.json`. `import`
reconciles that snapshot into a target install (terraform-style: plan by default,
`--apply` to write). The two installs have different numeric ids, so the tool
stores cross-references **by name / stable key** and rebuilds id links on import.

## Architecture

| File | Responsibility |
|------|----------------|
| `kion_copy.py` | CLI: `export` / `import` subcommands (argparse) |
| `kion/config.py` | `.env` loader → `Config` dataclass |
| `kion/client.py` | HTTP client: bearer auth, retry/backoff, unwraps `{status,data}` |
| `kion/export.py` | walk source install → `snapshot.json` |
| `kion/import_.py` | reconcile snapshot → target (plan/apply, adoption, id remap, drift) |
| `tests/test_remap.py` | unit tests for the pure helpers (no network) |

`import_.py` is the core. The `Importer` class reconciles each entity kind with
one of these actions: **ok** (in state + present), **adopt** (exists on target by
natural key → record mapping, no write), **create**, **recreate** (in state but
target id gone), **drift** (exists but differs — reported, never modified). Plus
**skipped** (a reference couldn't be mapped — expected) vs **failed** (real API
error), tracked separately so expected gaps don't read as errors.

`id-map.json` is the **state file**: source id → target id per entity. It makes
`--apply` safe to re-run. Adoption (natural-key match) means even with no state
file the tool won't duplicate — pointing at a target that already has the
environment yields all-adopt, zero creates.

## Running it

```sh
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.source   # source install URL + API key
cp .env.example .env.target   # target install URL + API key (+ DEFAULT_PERMISSION_SCHEME_ID)
python kion_copy.py export --env-file .env.source           # -> snapshot.json
python kion_copy.py import --env-file .env.target           # plan (read-only)
python kion_copy.py import --env-file .env.target --apply   # write + id-map.json
python -m pytest tests/                                     # unit tests
```

API key: Kion → User Profile → App API Keys → create.

## Kion API behavior you must know (verified live — swagger is incomplete/misleading)

- **Response envelope**: list/detail responses are `{status, data}`; create
  responses are `{record_id, status}` (no `data` wrapper). `client.py` handles both.
- **API path prefix**: hosted installs serve under `/api`; an app hit directly
  (e.g. `http://localhost:8081`) serves at the **root**. Controlled by
  `KION_API_PREFIX` (default `/api`, set empty for direct).
- **Read API hides fields that create requires**:
  - Projects/funding sources do **not** return their permission scheme or owners.
    Import resolves schemes by the stock per-type default name
    (`Default OU/Project/Funding Source Permissions Scheme`) → `DEFAULT_PERMISSION_SCHEME_ID`.
  - **Every OU/funding/project create requires ≥1 owner.** When none is captured
    or resolved, import falls back to the **running user** (detected via
    `GET /v3/app-api-key` → `user_id`).
- **Funding source OU**: `GET /v3/funding-source` returns `ou_id` only as
  `omitempty`, populated solely when an allocation transaction exists
  (allocations-mode). In **allocations-off** installs funding sources are global at
  the root and have no `ou_id` → import places them on the target root (correct,
  not lossy). `/v2/funding-source/{id}/ous` is availability per OU, **not** ownership.
- **Financial mode**: budget-mode installs reject `/v3/project` ("spend plans not
  available in budget mode") and need `/v3/project/with-budget`; spend-plan-mode is
  the reverse. Import tries both endpoints and pins the winner (`_post` accepts a
  list of candidate paths). The mode is the `budget_mode` flag in `cloudtamer_config`.
- **Budgets**: created via `POST /v3/budget`. Names are **auto-generated from the
  timeframe** and differ across installs/versions, so a budget's identity is
  `(scope, start_datecode, end_datecode)`, **not** its name. Don't key budgets on
  name. Two known create rejections (both are inconsistent **source** data, not
  bugs) — `_diagnose_budget_failure` names the cause:
  - "budget timeframe not fully covered" → a month in `[start, end)` has no row.
  - "insufficient funds available on funding source" → a funding source is
    over-subscribed (its total allocation across budgets exceeds its amount).
- **Scopes** (`/beta/scope`, project cost-allocation rules): paginated
  (`{items, total}`). Criteria reference cloud **accounts** by id. Export
  translates account ids → stable `account_number`; import remaps back via the
  target's accounts. Kion requires a scope to reference **≥1 existing account**, so
  a scope is **skipped** if none of its accounts exist on the target.
- **Billing sources** (`/v4/billing-source`, paginated): the read exposes config
  but **never secrets** (`key_secret`, AWS `linked_role`, OCI `private_key`,
  Azure/GCP creds are redacted). Import recreates **custom/aws/oci** as
  non-functional shells (`skip_validation` + placeholders the customer replaces);
  **gcp/azure/anthropic** are exported but skipped (need a prerequisite service
  account or provider registration flow). Adopted by name.

## Out of scope (not copied)

Cloud accounts, cloud rules, labels, custom variables, compliance, app-role
permission mappings. The tool copies org/financial structure only.

## Conventions

- Match the existing code style; keep `import_.py` helpers pure where they are
  (they're unit-tested without network). Add a test when adding a pure helper.
- Never commit `.env*` (except `.env.example`), `snapshot*.json`, or `id-map*.json`
  — they hold credentials / environment data. `.gitignore` enforces this.
- When you discover new Kion API behavior, document it here.
