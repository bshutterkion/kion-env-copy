# Design: billing-sources + accounts-only sync (accounts to the cache)

Date: 2026-07-14
Status: approved (pending spec review)

## Goal

Support copying **billing sources and cloud accounts only** from one Kion install
to another (immediate use case: `demo1.kion.io` → `qa4.kion.io`), without requiring
or creating any OU / project / funding / budget / scope structure on the target.

Today `import` always runs all seven reconcile passes, and an account is **skipped**
unless its project resolves on the target (`import_.py:817-821`), with `project_id`
always pinned in the create payload (`import_.py:833-839`). So there is no way to
copy accounts without also reproducing the OU→project tree they hang off of.

## Key API finding (verified against `kion-sdk-go/spec/master/swagger.json`)

Kion has **two** account-create families:

| | `POST /v3/account?account-type=…` (used today) | `POST /v3/account-cache?account-type=…` (the cache) |
|---|---|---|
| `project_id` | **required** | **not accepted** |
| `start_datecode` | **required** | not accepted |
| `payer_id` (billing source) | required | required |
| number field | per-provider* | per-provider* |
| `account_type_id` | optional | **required for aws**; optional azure/gcp/oci; **absent for custom** |
| `skip_access_checking` | aws/azure/gcp | aws/azure/gcp/oci; **absent for custom** |
| aws linked account field | `linked_aws_account_number` | `linked_account_number` |

\* number field by provider (unchanged from the tool's existing `ACCOUNT_NUMBER_FIELD`):
aws→`account_number`, custom→`account_number`, azure→`subscription_uuid`,
google-cloud→`google_cloud_project_id`, oci→`tenancy_ocid`.

Cache-create request schemas (verified): `AWSAccountCacheCreate`,
`AzureAccountCacheCreate`, `GCPAccountCacheCreate`, `CustomAccountCacheCreate`,
`OCIAccountCacheCreate`.

The target cache read is `GET /v3/account-cache` → model `AccountCache`, which
normalizes the provider identifier back into a single `account_number` field
(plus `id`, `account_name`, `payer_id`, `account_type_id`). The swagger response
body for this GET is empty/incomplete, so the client must handle both a bare list
and a `{items, total}` envelope defensively (as it already does for scopes and
billing sources) and verify the live shape during implementation.

**Consequence:** a project-less account is a *different endpoint*, not
`/v3/account` with `project_id` omitted. It needs only a **payer (billing source)** —
no OU/project/funding — which makes "billing sources + accounts only" genuinely
self-contained.

## Changes

### 1. Account-cache create path — `kion/import_.py`

Add a cache payload builder mirroring the existing `_billing_payload` style, one
branch per provider, producing `(path, payload)` for
`/v3/account-cache?account-type={provider}`:

- Common: `account_name`, `payer_id`, the per-provider number field, optional
  `account_alias`.
- `account_type_id`: included for aws (required), azure, gcp, oci; **omitted for
  custom** (schema has no such field — consistent with the existing note that the
  custom endpoint rejects it).
- `skip_access_checking: True` for aws/azure/gcp/oci; omitted for custom.
- aws linked accounts: send `linked_account_number` (note: cache field name, *not*
  `linked_aws_account_number`) plus `include_linked_account_spend` when present.
- **No** `project_id`, **no** `start_datecode`.

The snapshot already carries every field this needs (`account_number`, `provider`,
`account_type_id`, `payer_id`, `account_alias`, `linked_account_number`), so no new
per-account export fields are required for the create side.

### 2. Routing rule — `_reconcile_accounts`

Replace the hard skip on unresolved project with a **route-to-cache** rule. Unified
behavior (applies in both accounts-only and full-sync runs):

- Resolve `payer_id` → billing source first. If the payer is unresolved, **skip**
  as today (azure/gcp/anthropic billing sources aren't recreatable, so accounts
  under them still can't be copied — unchanged limitation, reported as skipped).
- Resolve `project_id` → target project.
  - **Resolved** → create via `/v3/account?account-type=…` (project-associated,
    exactly as today).
  - **Unresolved or absent** (projects weren't synced, the source account was
    already cache-only, or the project failed to create) → create via
    `/v3/account-cache?account-type=…` (cache).
- Reporting: cache creations are labeled distinctly (e.g. `create account 'X' (→ cache)`)
  so a full-sync run makes clear which accounts landed unassociated.
- After a successful create (either family), the account's number is added to the
  in-memory number index so the scope pass can still resolve it.

**Idempotency / adoption:** extend target indexing so the number index is the union
of `GET /v3/account` (associated) and `GET /v3/account-cache` (cached), both keyed
by `account_number`. An account is either associated or cached, never both, so the
union is unambiguous. Re-running `--apply` then adopts an already-copied account
(whether it currently sits in a project or the cache) instead of duplicating it.

### 3. Kind selector on `import` — `kion_copy.py` + `import_.py`

Add `--only KIND[,KIND...]` to the `import` subcommand. It restricts which reconcile
passes `Importer.run()` executes. Default (flag absent) = all kinds, preserving
current behavior exactly.

- Valid kinds: the existing `KINDS` tuple
  (`billing_sources, ous, funding_sources, projects, budgets, accounts, scopes`).
- Validation (fail fast with a clear message):
  - Unknown kind name → error listing valid kinds.
  - `accounts` selected without `billing_sources` → error (payer would never resolve,
    so every account would skip).
- Target indexing (`_index_target`) stays as-is (read-only; harmless to over-index).
  A later optimization could trim indexing to the selected kinds, but that is out of
  scope here.

For the immediate use case:
`import --env-file .env.target --only billing_sources,accounts`. Because the projects
pass never runs, `id_map["projects"]` stays empty and every account routes to the
cache — the desired "accounts only, unassociated" result.

### 4. Export source cache accounts too — `kion/export.py`

`_export_accounts` currently reads only `GET /v3/account`, so accounts already sitting
in the *source's* cache are invisible to the copy. Add a `GET /v3/account-cache` read
and merge its records into the exported `accounts` list, tagged so import treats them
as project-less (their `project_id` is null/absent → routes to cache by rule #2).

- Preserve existing fields; for cache-sourced records populate what the read exposes
  (`account_number`, `account_name`, `account_type_id`, `payer_id`, provider derived
  from `account_type_id` via the existing `ACCOUNT_PROVIDER` map) and leave
  `project_id` null.
- De-dup defensively by `source_id` in case an id appears in both reads.
- Bump `SCHEMA_VERSION` (currently 4) since the accounts section semantics widen.

### 5. Scaffold `.env` files

Create `.env.source` (demo1) and `.env.target` (qa4) from `.env.example`, URLs
pre-filled, `KION_API_KEY=` left blank for the user to paste. `.env*` (except
`.env.example`) is gitignored, so these are never committed.
`DEFAULT_PERMISSION_SCHEME_ID` can stay blank — accounts-only creates no
OU/project/funding, so no permission scheme is needed.

- `.env.source`: `KION_URL=https://demo1.kion.io`
- `.env.target`: `KION_URL=https://qa4.kion.io`

## Workflow (immediate use case)

```sh
python kion_copy.py export --env-file .env.source                                  # demo1 -> snapshot.json
python kion_copy.py import --env-file .env.target --only billing_sources,accounts            # plan (read-only)
python kion_copy.py import --env-file .env.target --only billing_sources,accounts --apply    # write + id-map.json
```

Expected outcome on qa4: aws/custom/oci billing sources recreated as shells
(secrets replaced by the customer), then their accounts created in qa4's account
cache. Accounts whose payer is an azure/gcp/anthropic billing source are skipped and
reported (those billing sources can't be recreated). From the cache the user can
later convert accounts to projects (`/v3/account-cache/{id}/convert/{project_id}`) —
out of scope for this tool.

## Tests (`tests/test_remap.py`, no network)

- Cache payload builder per provider (aws/azure/gcp/oci/custom): correct endpoint,
  correct number field, `account_type_id` present/absent per provider,
  `skip_access_checking` present/absent, no `project_id`/`start_datecode`, aws
  `linked_account_number` wiring.
- Routing decision: project resolved → `/v3/account`; unresolved/absent → cache;
  payer unresolved → skip.
- `--only` parsing/validation: unknown kind rejected; `accounts` without
  `billing_sources` rejected; subset selection runs only those passes.

Keep new helpers pure (endpoint/payload construction and the routing decision as
functions taking already-resolved ids), consistent with the existing unit-tested
helpers in `import_.py`.

## Out of scope

- Converting cached accounts to projects on the target.
- Trimming target indexing to the selected kinds (perf only).
- Recreating azure/gcp/anthropic billing sources (still requires a provider flow).
- Any `--skip` selector (the `--only` selector covers the need; YAGNI).

## Docs

Update `CLAUDE.md` "Kion API behavior" and "Running it" sections to document the
account-cache create family, the route-to-cache rule, and the `--only` selector.
