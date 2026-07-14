# The metadata-driven engine (`kion/engine/`, `kion/meta/`, `kion/overrides/`)

Status: **experimental, opt-in via `--engine`**. The hand-written `kion/export.py` /
`kion/import_.py` (the **oracle**) remain the reference implementation and the
default code path. This doc describes the engine **as built** — read the code before
extending it; the architecture below is what Tasks 1–11 actually converged on, not
the original SP1 sketch (see the "vs. the original spec" note at the end).

Spec: `docs/superpowers/specs/2026-07-14-tf-resource-parity-engine-design.md`.

## Purpose

`export.py`/`import_.py` hand-code the same read → translate → reconcile → write
logic once per entity kind. That doesn't scale to the ~60 resources the Kion
Terraform provider covers, and it silently drifts from what the provider's own
codegen metadata already knows about each resource's CRUD shape.

The engine is a **generic** version of that logic, driven by declarative metadata
instead of one hand-written function per entity: a resource is "added" by describing
its identity and its references to other resources, not by writing a new
export/import pair. It is also the foundation for a later terraform-emit adapter
(HCL + `terraform import` generation) that consumes the same inventory — see the
spec's "Decomposition" section — but that adapter does not exist yet; SP1 (this
engine) only keeps the inventory boundary shaped so it can drop in later.

Today the engine reproduces the same 7 entities the oracle handles
(`billing_source → ou → funding_source → project → budget → account → scope`),
verified equivalent to the oracle live via `scripts/equivalence_check.py`. It is not
yet wired to any resource the oracle doesn't already cover.

## Metadata layers

Three layers, increasing in how much of them is hand-authored:

### (a) Vendored — `kion/meta/vendor/`

Copied verbatim from the Kion Terraform provider's codegen (`…/terraform-provider/
new-terraform-provider/codegen/`) by `scripts/sync-provider-meta.sh
[provider-dir]`, which also stamps the provider's short commit SHA into
`kion/meta/vendor/VERSION` (currently `0874805`). The engine never reaches outside
this repo at runtime — everything it reads is one of these three files:

- **`generator_config.yaml`** — per-resource CRUD op map: `create`/`read`/`update`/
  `delete` `{path, method}` plus `schema.ignores` (fields to drop when building a
  create payload). Covers ~61 resources today; `kion.meta.load.load_resource_meta`
  parses it into one `ResourceMeta` per resource.
- **`crud_archetypes.yaml`** — non-standard identity/read patterns (e.g.
  `compound_key_parent_read` for `scope_criteria`, `no_read` for resources with no
  GET). Layered onto `ResourceMeta.archetype`/`parent_id_field`/`child_id_field`/
  `collection`; unused by any of the 7 entities the engine drives today (all are
  archetype `entity`, the default), but loaded so a future compound-key resource can
  use it.
- **`memberships.yaml`** — owner/member associations set via separate add/remove
  endpoints rather than the main update body. Loaded by the vendor smoke test
  (`tests/test_meta_vendor.py`) but not yet consumed by the engine — no create/
  reconcile path reads it.

### (b) Authored — `kion/meta/`

Hand-written, because the provider (built for a single install, not cross-install
copying) has no concept of "resolve this field by name on a different install":

- **`references.yaml`** — for each resource, which fields are foreign keys to
  another resource and by what natural key they resolve, e.g.
  ```yaml
  project:
    - {field: ou_id, target: ou, key: name}
  scope:
    - {field: project_id, target: project, key: name}
    - {field: account_numbers, target: account, key: account_number, many: true}
  ```
  `optional: true` marks a reference that's allowed to stay unresolved (the record
  still creates); `many: true` marks a list-of-references field. Loaded by
  `load_references` into `{resource: [Reference, ...]}`.
- **`natural_keys.yaml`** — each resource's identity for cross-install adoption/
  dedup. Four `kind`s appear across the 7 entities:
  - `name` — billing_source, funding_source: `(nkey(name),)`.
  - `name_in_parent` — ou (`parent_field: parent_ou_id`), project (`ou_id`), scope
    (`project_id`): `(parent_key, nkey(name))`.
  - `account_number` — account: `(account_number,)`.
  - `date_range` — budget: `(start_datecode, end_datecode)` (scoped to a target OU/
    project by the budget override, not by this key alone — see below).
  Computed by `kion.engine.keys.natural_key`; `nkey` (from `import_.py`) is the same
  case/whitespace-insensitive name normalizer the oracle uses, reused rather than
  reimplemented.

### (c) Python overrides — `READ_OVERRIDES` in `kion/meta/load.py`

A small dict merged onto the vendored `ResourceMeta` for resources whose read the
generator config doesn't describe under the name the engine needs. Today this is
just `account`: it has no `generator_config.yaml` entry (a vendor gap — its create
op is flagged `INCOMPLETE` in the vendored file), so `READ_OVERRIDES["account"]`
supplies `read_path: /v3/account` and a `create_path` (the create path is unused —
account creation goes through the `build_create_payload` hook instead, since it
routes to either `/v3/account` or `/v3/account-cache` depending on whether the
project resolves). `load_resource_meta` creates a `ResourceMeta` from scratch for a
name absent from the vendored config, or overlays the supplied fields onto one that
exists.

`_engine_meta()` (in `kion_copy.py` and duplicated in `scripts/equivalence_check.py`
— not shared, see "surprises" below) takes the **intersection** of
`load_resource_meta()`'s ~61 resources and `load_natural_keys()`'s 7: a resource
with `ResourceMeta` but no natural-key spec would `KeyError` inside
`natural_key()`, so only the 7 onboarded resources are ever walked.

## Inventory core (`kion/engine/inventory.py` — `build_inventory`)

Metadata-driven equivalent of `export_install`. Reads `resources` off the source
install in dependency order (`order_resources`, a DFS topological sort over
`references.yaml` — a resource is visited after every resource it references) and
returns `{resource: [{source_id, natural_key, fields}, ...]}`.

For a resource read through the **generic path** (funding_source, project, ou —
i.e. everything without a bespoke reader below):
1. GET the list endpoint, derived from `ResourceMeta.read_path` by
   `kion.engine.paths.list_path` (strips a trailing `/{id}` template — the SAME
   derivation `EngineReconciler` uses to index the target, so a read and an index
   can't disagree on which endpoint a resource's records come from).
2. OU is walked parent-first (`_order_self_ref`, delegating to `import_.order_ous`)
   because its `name_in_parent` key is self-referential (an OU's parent is another
   OU) — every other `name_in_parent` resource's parent is a *different* resource,
   already fully read by `order_resources` before this one starts.
3. Per record: pop `id` as `source_id`, compute the natural key
   (`_record_key`, resolving the parent component through the accumulating
   `id_to_key` map for `name_in_parent` resources), drop `ResourceMeta.ignores`
   fields, and translate reference fields from source ids to natural keys
   (`to_natural` — see below).

Three resources don't fit that generic path and are read through **bespoke,
export-shaped readers** instead of being re-derived from scratch:

| resource | reader | why |
|---|---|---|
| `account` | `_read_accounts` → `export._account_record` | union of `/v3/account` (project-associated) + `/v3/account-cache` (unassociated); cache ids are namespaced `cache:<id>` so they never collide with associated ids in the same id space. No `generator_config.yaml` read entry either. |
| `billing_source` | `_read_billing_sources` → `export._export_billing_sources` | a raw `/v4/billing-source` record has no top-level `name` (it's nested per-type: `aws_payer.name`, `gcp_payer.gcp_billing_account.name`, ...); the export reader already flattens it. |
| `budget` | `_read_budgets` → `export._export_budgets` | budgets aren't a single global list — they're read per OU/project (`/v3/{ou,project}/{id}/budget`); the export reader already walks that. |

Each reuses `kion.export._export_*`/`_account_record` directly rather than
re-porting the same transform — the docstring in `inventory.py` is explicit that
these "must not be re-ported." The three then run through a shared post-step
(`_finish_export_record`) that mirrors the generic per-record body: pop the export
record's own id field (a different field name per resource —
`_EXPORT_ID_FIELD = {billing_source: source_id, budget: source_budget_id, scope:
source_scope_id}`), compute the natural key, and `to_natural`-translate references.

One field is explicitly **excluded** from that translation:
`scope.account_numbers` (`_EXCLUDE_TO_NATURAL`) — `_export_scopes` already
translates it to stable account-number *strings*, not raw source ids, so running it
through the generic `to_natural` (which looks up `id_to_key` by numeric source id)
would silently drop every entry. A scope override resolves these numbers directly
against the target's account index instead (`t_acct_by_number`).

**Reference translation** (`kion/engine/refmap.py`):
- `to_natural(record, refs, id_to_key)` — used by the inventory reader. For each
  reference field, retains the original source id as `__srcid__<field>` (accounts,
  ou, funding_source hooks all read these to resolve against `id_map` at
  reconcile time) and rewrites the field itself to the referenced resource's
  natural key (or `None`/`[]` if unresolved at read time — a forward reference that
  hasn't been read yet, or truly absent).
- `to_target_ids(record, refs, key_to_tid)` — used by the reconciler's generic
  create path. The inverse: turns a natural key back into a **target** id via the
  live `_t_key` index, collecting any reference that couldn't be resolved
  (`unresolved`) so the caller can skip the record instead of creating it with a
  dangling reference.

## Reconcile adapter (`kion/engine/reconcile.py` — `EngineReconciler`)

Metadata-driven equivalent of `Importer`. Same shape of decision as the oracle —
**ok** / **adopt** / **create** / **recreate** / **skip** (+ **failed** for a real
API error) — computed generically from `ResourceMeta`/`Reference` metadata instead
of one method per entity kind, with `kion.overrides.registry.HOOKS` as the escape
hatch for behavior that resists declaration.

`run()` calls `_index_target()` once (unless a test has already injected `_t_key`/
`_t_ids` directly, bypassing the network — see the module docstring), then
reconciles every resource in dependency order.

**`_index_target`** builds, per resource in the inventory, `_t_key` (natural key →
target id) and `_t_ids` (the set of live target ids) by listing the target the same
way the inventory reader lists the source — reusing `list_path`/`list_records` so
the two can't diverge on which endpoint a resource's records come from. Two special
cases:
- `billing_source` is indexed via the **same** `_export_billing_sources` reader
  `build_inventory` uses for the source side (a raw record has no top-level `name`,
  as above) — so source and target are indexed symmetrically for name-based
  adoption.
- `account`'s index is the union of `/v3/account` + `/v3/account-cache` (mirroring
  the inventory read), and additionally pre-populates `t_acct_by_number` from
  **existing** target accounts (not just ones this run creates) so the scope pass
  can resolve an account that was merely *adopted*, not created, this run.

It also unconditionally runs `_index_ctx()` (when `config` is set) — pulling
permission schemes, users, groups, the target root OU id, and the running user's id
(the owner-of-last-resort) — the same enrichment `Importer._index_target` does, now
generalized into `resolve_scheme`/`resolve_owners` methods the hooks call as `ctx`.

**`_reconcile(res)`** — the per-resource decision loop:
1. If `HOOKS[res].reconcile_override` is set, delegate the *entire* pass to it and
   return (see below) — the generic loop under this point never runs for that
   resource.
2. Otherwise, for each inventory record: check **ok** (already in `id_map` and
   still live on target) → **adopt** (natural key already on target — or the
   hook-supplied `adopt_key`, see OU below) → else **create**/**recreate**, running
   `identity_ok` (optional pre-check), `build_create_payload` (hook) or the generic
   `to_target_ids` + `ignores` payload, then `_post` (mirrors `Importer._post`:
   `create_path` may be a list of candidate endpoints, tried in order, with the
   first success pinned for the rest of the run — this is how the OU/project
   budget-mode-vs-spend-plan-mode endpoint probe generalizes).
3. `post_create` (hook) runs before the new id is indexed into `t_key`/`t_ids`, so
   any ctx anchoring it does (e.g. OU minting a rootless target's root) is visible
   to the indexing that follows.

## Hook types (`kion/overrides/registry.py` — `Hooks`)

All optional (default `None`); the reconciler only invokes a hook when the
resource's `HOOKS` entry sets it. The generic path is byte-for-byte unchanged for a
resource with no hooks (none of the 7 today have zero hooks, but the machinery
supports it).

| hook | signature | used by | purpose |
|---|---|---|---|
| `build_create_payload` | `(fields, ctx) -> (paths, payload) \| None` | funding_source, project, account, ou, billing_source | Build the create request (schemes/owners/routing the generic `to_target_ids` can't express); `None` means skip. |
| `identity_ok` | `(fields, ctx) -> bool` | account | Reject a record before it reaches create — account: blank `account_number`. |
| `post_create` | `(fields, new_id, ctx) -> None` | account, ou | Side effects after a successful create: account registers itself in `t_acct_by_number`/`t_acct_ids` for the later scope pass; ou anchors a minted root as `ctx.target_root_id`. |
| `order_records` | `(records, ctx) -> records` | ou | Reorder before the loop starts — ou: parent-before-child (`order_ous`). |
| `pre_reconcile` | `(records, ctx) -> None` | ou | Seed state before the loop — ou: map source root → target root by *position*, not name, so the root is caught by the `ok` check regardless of naming. |
| `adopt_key` | `(fields, ctx) -> key \| None` | ou | Override the per-record adoption key — ou adopts on `(target parent id, name)` (bridged through `id_map`), not its inventory name-chain key, since a parent OU may itself have just been created/adopted under a different id than the source. `None` means "not adoptable yet." |
| `reconcile_override` | `(ctx, records) -> None` | budget, scope | The resource owns its **entire** reconcile pass — used where the generic list+natural-key model doesn't fit at all: budget's identity is `(target scope, start_datecode, end_datecode)` with adoption reads done *per scope* (`/v3/{ou,project}/{id}/budget`), never from one global list; scope's create remaps `account_numbers` to target account ids and requires ≥1 to resolve, with an "Invalid scope criteria" failure diagnostic tied to target billing-data ingestion. |

`build_create_payload` implementations are thin: they call back into `ctx.resolve_scheme`/
`ctx.resolve_owners` (which wrap the *same* pure `import_.resolve_scheme`/
`resolve_owners` helpers `Importer` calls) rather than reimplementing scheme/owner
resolution, and `_billing_source_payload` calls `Importer._billing_payload` directly
— the per-type billing payload logic is not duplicated, just reused across both
paths.

`reconcile_override` implementations (`_budget_reconcile`, `_scope_reconcile`) are
close ports of `Importer._reconcile_budgets`/`_reconcile_scopes`, rewritten against
`ctx` (`ctx.id_map`, `ctx._post`, `ctx.counts`, `ctx._t_key`, `ctx.t_acct_by_number`,
`ctx._last_error`, ...) instead of `self`. They keep the oracle's diagnostics
verbatim — `_diagnose_budget_failure` (timeframe-not-covered / insufficient-funds)
and the scope "Invalid scope criteria" cause line — so a `--engine` plan/apply gives
the same actionable failure messages the oracle does. **Naming note**: the engine
keys everything on the *singular* resource name (`"budget"`, `"scope"`), where the
oracle used the *plural* (`"budgets"`, `"scopes"`) — intentional, documented in both
files, and one of the two normalizations `equivalence_check.py` accounts for.

## CLI

`--engine` on both `export` and `import` opts into this path; its absence (the
default) runs the oracle unchanged:

```sh
python kion_copy.py export --env-file .env.source --engine            # build_inventory -> snapshot.json
python kion_copy.py import --env-file .env.target --engine            # EngineReconciler, plan
python kion_copy.py import --env-file .env.target --engine --apply    # EngineReconciler, write + id-map.json
```

`--only` (oracle-only entity-kind filtering) is accepted but ignored when `--engine`
is set — the engine always walks the full onboarded resource set (today, the same 7
the oracle covers by default). `id-map.json` is shared state-file format between the
two paths (both key by `str(source_id) -> target_id` per resource), but the oracle
uses plural resource-kind keys and the engine uses singular — **an id-map produced
by one path is not directly reusable by the other** without translating those top-
level keys (this is not currently automated anywhere; a fresh `--apply` under
`--engine` starts from its own `id-map.json` or an empty one, same as switching
`--only` sets under the oracle would).

## Equivalence harness (`scripts/equivalence_check.py`)

The regression gate that proves the engine reproduces the oracle. Reads a source
install **once**, then plans it against a target **twice** — once with the oracle
(`export_install` + `Importer`, plan mode) and once with the engine
(`build_inventory` + `EngineReconciler`, plan mode) — and diffs the two runs'
`counts`/`skipped`/`failed` dicts per entity per action. No writes either way.

Two **known, accepted** surface differences are normalized before diffing (not
hidden — both are printed as `ok (normalized)` rows, not silently dropped):
1. **Plural vs. singular count keys** — the oracle's `counts["billing_sources"]`
   vs. the engine's `counts["billing_source"]`, paired explicitly via the script's
   `PAIRS` list.
2. **OU root +1** — the engine counts the source-root → target-root mapping as an
   `ou.ok`, the oracle maps it uncounted; the engine's `ou/ok` is expected to be
   exactly the oracle's `ous/ok + 1`. Any *other* per-entity per-action mismatch is
   reported as a real divergence.

Run it against two configured installs (defaults `.env.source`/`.env.target`, same
convention as `kion_copy.py`):

```sh
python scripts/equivalence_check.py
python scripts/equivalence_check.py --source-env .env.source --target-env .env.target --verbose
```

Exits 0 and prints `EQUIVALENT` when every row matches after normalization, 1 when
any row diverges, 2 on a config/network error. It was last run demo1 → qa4 and
printed `EQUIVALENT` for all 7 entities.

## Recipe: adding a resource

1. **`kion/meta/natural_keys.yaml`** — add the resource's identity: `kind: name`
   (global uniqueness by name), `kind: name_in_parent` (+ `parent_field`) for a
   hierarchical resource, or a bespoke `kind` if neither fits (add a branch to
   `kion.engine.keys.natural_key` if so — e.g. `account_number`, `date_range`).
2. **`kion/meta/references.yaml`** — add an entry per FK field: `{field, target,
   key}`, plus `many: true` for a list-of-references field and `optional: true` if
   the record should still create with the reference unresolved.
3. **Read path** — if the resource has a `generator_config.yaml` `read` entry that
   returns list-shaped, name-flat records, nothing else is needed; `build_inventory`
   and `_index_target` will pick it up generically via `list_path`. If not (no
   vendor read entry, a compound/paginated shape, or a nested-name field like
   billing_source), either add a `kion.meta.load.READ_OVERRIDES` entry (for a
   missing/incomplete vendor read path) or a bespoke reader in
   `kion/engine/inventory.py` mirroring `_read_accounts`/`_read_billing_sources`
   (reuse an `export._export_*` function if one already exists for this resource;
   don't re-port its transform).
4. **Hooks** — add one only if a behavior resists declaration. Reuse
   `ctx.resolve_scheme`/`ctx.resolve_owners` for scheme/owner resolution rather than
   reimplementing it; reach for `reconcile_override` only when identity or adoption
   genuinely can't be expressed as "natural key on one list read" (composite
   identity scoped to a parent, per-parent reads, id↔external-key remapping inside a
   payload). Most new resources should need `build_create_payload` at most, and many
   need no hook at all if the create payload is just "fields minus ignores, refs
   mapped to target ids."
5. **Tests** — add unit coverage for any new pure helper (`kion/engine/keys.py`,
   `refmap.py`, a new hook) the same way the existing `tests/test_engine_*.py` and
   `tests/test_overrides.py` do — inject `_t_key`/`_t_ids` directly to avoid the
   network (see `EngineReconciler.__init__`'s docstring note on this).
6. **If the resource overlaps the 7** the oracle already handles, run
   `scripts/equivalence_check.py` against two real installs to confirm the engine's
   plan still matches the oracle's after your change. For a genuinely new resource
   (not one of the 7), there is no oracle counterpart to diff against — equivalence
   only applies to the 7.

## Surprises / things worth a second look

Noted here per the task brief rather than smoothed over in the prose above:

- **`_engine_meta()` is duplicated**, not shared, between `kion_copy.py` and
  `scripts/equivalence_check.py` — same body (load the three metadata files, take
  the `meta ∩ natural_keys` intersection), copy-pasted. A future change to that
  logic (e.g. onboarding an 8th resource) has to be made in both places or the two
  will silently diverge on which resources `--engine` walks vs. which the
  equivalence check verifies.
- **`account`'s vendored `create_path`/`create_method` in `READ_OVERRIDES` are dead
  code** — `EngineReconciler` never reads `ResourceMeta.create_path` for `account`
  because `HOOKS["account"].build_create_payload` (`_account_payload`) always
  supplies its own `paths` list (`account_project_payload`/`account_cache_payload`),
  which takes precedence in the reconcile loop (`if hooks and
  hooks.build_create_payload is not None: ... paths, payload = built`). The
  `READ_OVERRIDES` entry exists only for its `read_path`.
- **`crud_archetypes.yaml`/`memberships.yaml` are vendored and parsed but not yet
  consumed** by anything the 7 entities exercise — `ResourceMeta.archetype` is
  loaded but every one of the 7 is the default `entity` archetype, and no hook reads
  `memberships.yaml` at all. They're there because the vendor sync script pulls all
  three provider files uniformly, not because SP1 uses them yet — expect the first
  SP2 resource with owner/member associations (per the spec's decomposition list) to
  be what finally exercises `memberships.yaml`.
- **id-map key casing is asymmetric to the oracle's `--only` story**: the oracle's
  `id-map.json` and the engine's are not cross-compatible (plural vs. singular
  top-level keys, as noted in the CLI section above) — there's no migration or
  detection for this if someone runs `--apply` once without `--engine` and then
  again with it (or vice versa) against the same target using the same
  `--id-map` path. Not currently a documented footgun anywhere else in the repo.
- **`natural_keys.yaml`'s `date_range` kind for budget is a shorthand** — the actual
  identity budget reconciles on is `(target scope, start_datecode, end_datecode)`,
  not just the date range; the scope component is handled entirely inside
  `_budget_reconcile`'s own `state_key = f"{scope_kind}:{scope_id}:{start}:{end}"`
  and never goes through `kion.engine.keys.natural_key`'s `date_range` branch at
  reconcile time (that branch is only exercised for the inventory-side natural key,
  which is scope-less and used solely as the record's own `natural_key` label, not
  for reconcile decisions — `reconcile_override` bypasses `_t_key`/adopt entirely).
  Easy to misread the natural_keys.yaml comment (`# (scope, start_datecode,
  end_datecode)`) as describing what `natural_key()` computes, when it actually only
  computes the two-element `(start, end)` tuple.

## vs. the original spec

The spec (`docs/superpowers/specs/2026-07-14-tf-resource-parity-engine-design.md`)
sketched `kion/adapters/` as the home for a pluggable reconcile/terraform-emit
adapter pair. As built, the reconcile adapter lives at `kion/engine/reconcile.py`
(not a separate `adapters/` package), and `kion/overrides/registry.py` is the
override/hook layer the spec called for — present, but with a richer hook surface
(`order_records`/`pre_reconcile`/`adopt_key` for OU's self-referential hierarchy,
`reconcile_override` for budget/scope's whole-resource takeover) than the spec's
generic "behaviors that can't be declared" language anticipated. `build_inventory`
also turned out to need bespoke, export-reader-reusing paths for 3 of the 7
resources (billing_source/budget/scope) rather than the fully generic "read → strip
ignores → translate refs" the spec's inventory-core section describes — the
generic path only covers funding_source/project/ou today.
