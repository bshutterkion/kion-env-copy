# Design: metadata-driven copy engine (TF-provider resource parity) — SP1

Date: 2026-07-14
Status: draft (pending review)

## Purpose & goals

`kion-env-copy` lets a user copy their environment — and sub-resources within it — to
a **new environment**, for users who don't want to manage their install via
`terraform import` → `terraform apply` elsewhere. It is the **easier-than-Terraform**
path. Two goals build on the same foundation:

- **#1 (this track): copy parity.** Copy **every resource the Kion Terraform provider
  supports** (the codegen provider at
  `…/delivery-support/dev-tools/terraform-provider/new-terraform-provider`: 50
  resources) between installs, keeping the tool's snapshot → plan → apply reconcile
  model and its current billing/budget/scope coverage (a superset).
- **#2 (later): terraform-importer replacement.** Become the successor to the old
  `terraform-importer` (`importer-script/` in the *old* provider — a Python tool that
  reads an install and emits HCL config + `terraform import` commands + module/
  provider files via `templates.py`, but only for a subset: CFTs, IAM/Azure policies,
  Azure/OU/project roles, cloud rules, compliance). The new tool does the same
  `terraform import` job with **full 50-resource coverage** and the **new provider's**
  resource types/schemas.

The unifying insight: **both goals need the same thing first** — a complete,
id-and-natural-key-resolved **inventory** of an install. #1 feeds that inventory to a
*reconcile* adapter (write to a target env); #2 feeds the *same* inventory to a
*terraform-emit* adapter (write HCL + import commands). So the engine is built as an
**inventory core + pluggable output adapters**, and #2 is a second adapter, not a
rewrite.

Doing this by hand-writing 50 exporters would replicate — and drift from — knowledge
the provider already encodes in machine-readable form. Instead the tool becomes
**metadata-driven**: a generic engine reads the provider's codegen metadata + OpenAPI
spec, with a small authored override layer for the cross-install concerns the
provider doesn't model.

Because this is far too large for one spec, it is **decomposed into sub-projects**.
This document specifies **SP1 (the engine foundation)** only. SP2…SPn each get their
own spec.

## Decomposition (recorded for shape; only SP1 is specified here)

- **SP1 — engine foundation (this spec).** The generic reconcile engine + metadata
  loaders + the net-new override layer. **Acceptance: it reproduces the current 7
  entities' behavior** (billing_source → ou → funding_source → project → budget →
  account → scope), matching what we've already vetted live, before any new resource
  is added.
- **SP2…SPn — fan out by group**, each reusing the engine and adding override
  entries + tests, ordered by dependency:
  1. identity & `permission_scheme` (many resources reference them)
  2. access / permission mappings, cloud access roles, saml group associations
  3. IAM / policy artifacts (iam_policy, cft, azure_policy/role/arm, gcp_iam_role, ami)
  4. cloud governance (cloud_rule, project_enforcement, service_control_policy)
  5. compliance (check, standard, family, level, program)
  6. metadata/config (label, custom_variable(+override), app_config, notes, webhook,
     service_catalog, dashboard, gcp_service_account, billing_rule, forecast, category)
- **Later — #2 terraform-emit adapter** (terraform-importer replacement): a second
  output adapter over the same inventory core, emitting HCL + `terraform import`
  commands with full 50-resource coverage. Its own spec.

## What the provider already gives us (vendored, read-only)

From the provider's `codegen/` + `spec/`:

- `generator_config.yaml` — per-resource CRUD op map: `create`/`read`/`update`/
  `delete` `{path, method}` + `schema.ignores`. The backbone: read → export, create
  → import, ignores → drop.
- `crud_archetypes.yaml` — non-standard identity/read patterns:
  `compound_key_parent_read` (e.g. `scope_criteria` = `(scope_id, criteria_id)`),
  `no_read`, entity quirks.
- `memberships.yaml` — owner/member associations set via **separate** add/remove
  endpoints (not the main update body).
- `renames.yaml`, `private_endpoints.yaml`, `schema_overrides.yaml` — field/endpoint
  adjustments.
- `spec/openapi3.json` — request/response schemas (types, required, nesting).

## What the provider does NOT give us (net-new, authored in this repo)

The provider never remaps ids across installs (TF users pass ids in), so two things
must be authored here as override yamls that mirror the codegen-yaml style:

- **`references.yaml`** — for each resource, which fields are references to other
  resources, and by what natural key they resolve. Covers plain FKs
  (`project.ou_id → ou.id`), by-name refs with fallback
  (`*.permission_scheme_id → permission_scheme.name`, with type-default +
  configured-default fallback), owner refs (`owner_user_emails → user.email`,
  `owner_user_group_names → user_group.name`), and list refs
  (`scope.account_numbers → account.account_number`).
- **`natural_keys.yaml`** — each resource's identity for adoption (the key that must
  be unique on the target). Mostly `name` within a parent scope; some compound
  (budget = `(scope, start_datecode, end_datecode)`; account = `account_number`).

Where the spec's naming makes a reference inferable, a `kgen`-style helper can
propose entries; every entry is human-confirmed. These two files are the heart of
SP1.

## Engine architecture

**Inventory core + pluggable output adapters.** The core walks an install into a
normalized, adapter-agnostic inventory; adapters turn that inventory into side
effects. SP1 builds the core + the reconcile adapter; #2 adds the terraform-emit
adapter against the same core.

1. **Metadata layer** (`kion/meta/`, vendored copies + loaders): parse the provider
   yamls + `openapi3.json` into an in-memory `ResourceMeta` per resource (op set,
   ignores, archetype, memberships, references, natural key).
2. **Inventory core** (`kion/engine/`):
   - **Reader/exporter**: resources in dependency order → call `read`/list op → strip
     `ignores` → for each record capture `{resource, source_id, natural_key,
     fields}` where reference fields are resolved to the **target natural key** while
     the **source id is retained**. This dual form is what makes one inventory serve
     both adapters (reconcile needs the natural key to remap; terraform-emit needs
     the source id for `terraform import` and the natural key for HCL references).
3. **Output adapters** (`kion/adapters/`):
   - **Reconcile adapter** (SP1): topological order over the reference graph → per
     record choose ok/adopt/create/recreate/drift/skip (generalizing today's
     `Importer`) → create via the `create` op → record id-map → remap references on
     the way in → plan/apply.
   - **Terraform-emit adapter** (#2, later): same inventory → HCL config + `terraform
     import` commands + module/provider files. The old `importer-script/`
     (`templates.py`, `write_module_file`, `write_provider_file`,
     `write_resource_import_script`) is the reference/oracle for its output shape.
4. **Override/hook layer** (`kion/overrides/`): a registry of Python callables keyed
   by resource for behaviors that can't be declared (see next section). Standard
   resources need none; gnarly ones plug in. This is the escape hatch that keeps the
   core generic without pretending the API is uniform.

`_post`-style candidate-endpoint probing, retry/backoff, and the `{status,data}` /
`{record_id,status}` envelope handling stay in the existing `client.py`.

SP1 does **not** build the terraform-emit adapter — it only keeps the inventory
boundary adapter-agnostic (dual id + natural-key form above) so #2 drops in cleanly.

## Special behaviors SP1 must preserve (the 7 exercise all of these)

Each becomes either a declarative flag in the override yamls or a hook:

- **Permission-scheme resolution** by name → stock per-type default →
  `DEFAULT_PERMISSION_SCHEME_ID` → unresolved-skip (`resolve_scheme` today).
- **Owner fallback** to the running user when none captured/resolved.
- **Account → cache routing** when the project can't be resolved (recent work);
  payer required; blank account_number skipped.
- **Budget** identity by date range (auto-named) and its create diagnostics.
- **Financial-mode endpoint probing** (`/v3/project` vs `/v3/project/with-budget`) —
  generalized as `create.path` candidate lists.
- **Billing-source** type-specific shell payloads (custom functional; aws/oci
  placeholders; gcp/azure/anthropic skipped) and `skip_validation`.
- **Scope** account-criteria id↔number remap and ≥1-existing-account requirement.
- **Funding-source** allocations-off → target-root placement.

SP1's job is to reproduce these; the mechanism (declarative vs hook) is chosen per
behavior during implementation, favoring declarative.

## Vendoring & sync

Copy the needed provider files into `kion/meta/vendor/` (pinned). A
`scripts/sync-provider-meta.sh` refreshes them from a configurable provider-repo
path and records the provider commit/version in `kion/meta/vendor/VERSION`. The
engine never reaches outside the repo at runtime. `openapi3.json` is large; vendor
only the slices we parse if size is a problem (decided in implementation).

## Acceptance criteria (SP1)

1. Engine-driven export produces a snapshot for the 7 entities **equivalent** to the
   current `export.py` output (field-by-field, modulo key ordering) against demo1.
2. Engine-driven import **plan** against a target yields the **same actions**
   (ok/adopt/create/recreate/drift/skip counts and per-record decisions) as the
   current `import_.py` for the 7 — verified against localhost and the demo1-self
   all-adopt case.
3. A live `--apply` of the 7 via the engine against localhost matches the
   hand-written path's result (idempotent re-apply, same cache routing, same skips).
4. `--only` and the existing CLI/flags keep working unchanged.
5. Unit tests for the pure engine helpers (reference translation, topo ordering,
   natural-key adoption, action selection) with no network.

## Validation harness

A `tests/` (or `scripts/`) equivalence check that runs both the hand-written and
engine paths over the same snapshot/target in plan mode and diffs the decisions —
the concrete gate for "reproduces the 7."

## Out of scope (SP1)

- The other 43 resources (SP2…SPn).
- #2 terraform-emit adapter (HCL + `terraform import` generation) — separate spec;
  SP1 only keeps the inventory boundary adapter-ready.
- Removing/retiring `export.py`/`import_.py` (they stay as the reference oracle
  until the engine matches; retirement is a later, separate step).
- Any change to `client.py` transport behavior.

## Risks / open questions

- **Reference inference coverage** — how many FKs are *not* inferable from spec
  naming and must be authored by hand (expected: manageable for the 7; re-assessed
  per group in SP2+).
- **Behaviors that resist declaration** — if too many need hooks, the "generic
  engine" benefit shrinks; SP1 is where we learn the declarative/hook ratio on a
  known-hard set (budgets, billing sources, scopes).
- **openapi3.json size** in-repo (vendoring strategy above).
- **Provider metadata drift** — the sync script + pinned VERSION bound this.
