# Resource Onboarding Report — metadata sweep (feature/engine-onboard-resources)

## Status (corrective pass)

This report was corrected after an initial sweep left a safety gap: it added
`natural_keys.yaml`/`references.yaml` entries for **all** 30 non-skip
resources it analyzed, which made every one of them appear in
`engine_meta()`'s active `--engine` resource set — but only the resources
classified **generic** are actually safe to reconcile that way today (see
"Why the safety gate exists" below). This corrective pass (1) analyzed the 3
resources the original sweep failed to produce a proposal for
(`permission_scheme`, `project_cloud_access_role_exemption`,
`project_enforcement`), bringing the total from 46 to the intended **49**, and
(2) split the metadata into an ACTIVE set (safe for `--engine` today) and a
STAGED set (metadata captured, but inert until a hook is implemented).

| set | count | where | safe for `--engine` today? |
|---|---|---|---|
| **ACTIVE** | 9 generic (+ the original 7 = 16 engine-ready resources total) | `kion/meta/natural_keys.yaml`, `kion/meta/references.yaml` | **yes** |
| **STAGED** | 21 hook + 1 read_transform = 22 | `kion/meta/natural_keys.staged.yaml`, `kion/meta/references.staged.yaml` (NOT loaded by `load.py`/`setup.py`) | **no** — needs a hook first |
| **SKIP** | 18 | this report only (no metadata written anywhere) | n/a — not onboardable as-is |
| **total** | **49** | | |

The 9 ACTIVE generic resources: `app_api_key`, `app_role`, `billing_rule`,
`category`, `compliance_family`, `compliance_level`, `compliance_program`,
`idms`, `webhook`. Together with the original 7 (`billing_source`, `ou`,
`funding_source`, `project`, `budget`, `account`, `scope`) these are the full
16-resource set a live `--engine` run walks today.

Source: `.superpowers/onboard/proposals/*.json`, one proposal file per
candidate resource, each produced by an analysis pass over
`kion/meta/vendor/generator_config.yaml`, `crud_archetypes.yaml`, and
`memberships.yaml` plus (where available) live-API verification notes. This
report synthesizes those 49 proposals into the active + staged metadata files
above.

**Note on the count**: an earlier version of this report noted the task
description referred to "49 Kion provider resources" while only 46 proposal
files existed. That gap is now closed: `permission_scheme`,
`project_cloud_access_role_exemption`, and `project_enforcement` were
analyzed in this corrective pass (see their sections below), bringing
`.superpowers/onboard/proposals/*.json` to exactly **49** files
(`ls .superpowers/onboard/proposals/*.json | wc -l` → 49).

## Classification summary

| classification | count | meaning |
|---|---|---|
| generic | 9 | copyable via the existing metadata-driven engine alone (natural key + `references.yaml` remap, no bespoke code) — **ACTIVE** |
| hook | 21 | needs a `build_create_payload` (and often a paired export-side owner/email capture) hook in `kion/overrides/registry.py` before it can be reconciled correctly — **STAGED** |
| read_transform | 1 | needs a bespoke inventory reader to synthesize an identity field before the generic `natural_key()` applies (`user`: `name := username`) — **STAGED** |
| skip | 18 | not onboardable as-is -- no natural key expressible in the engine's 4 supported kinds (`name`, `name_in_parent`, `account_number`, `date_range`), out of scope per CLAUDE.md, or needs a new engine mechanism (e.g. `no_read`/`association`/`parent_list` archetypes have no generic reconcile path today) |
| **total** | **49** | |

## Why the safety gate exists

`engine_meta()` (`kion/engine/setup.py`) builds the active `--engine` resource
set as `{r for r in meta if r in nkeys}` — i.e. *any* resource with a
`natural_keys.yaml` entry is treated as engine-ready and gets walked by
`kion_copy.py --engine`'s `build_inventory()`/`EngineReconciler`. For the 9
**generic** resources that's the whole story: their create payload is exactly
"fields minus ignores, references remapped by natural key," which is precisely
what the generic reconcile path in `kion/engine/reconcile.py` does — no
bespoke code needed, so they're correctly and safely reconcilable today.

For the 21 **hook** and 1 **read_transform** resources, no hook is registered
for them in `kion/overrides/registry.py` (or a reader in
`kion/engine/inventory.py` for `user`), so the *same* generic reconcile path
would still run if their metadata were active — but it does not know how to
resolve owner emails, reshape nested/exploded fields (e.g.
`permission_scheme`'s `roles`), drop out-of-scope FK fields, or synthesize an
identity field (`user`'s `name := username`). Left active, a live `--engine`
run against them would produce incomplete/wrong payloads (unmapped owner ids,
dropped type-specific nesting) or fail outright on unresolved required
fields. That is exactly what this corrective pass gates out: their
`natural_keys.yaml`/`references.yaml` entries were moved to
`kion/meta/natural_keys.staged.yaml`/`kion/meta/references.staged.yaml` —
files `kion/meta/load.py` and `kion/engine/setup.py` never read (verified: the
loaders open the literal filenames `natural_keys.yaml`/`references.yaml`, no
directory glob) — so they keep the already-completed identity/reference
analysis available for the next phase without being live today. This matches
this repo's own established precedent of metadata-then-hook as a two-phase
pattern (see `kion/engine/reconcile.py`'s docstring on `account`'s hook
landing before billing_source/budget/scope's did).

## What this sweep + corrective pass changed

- `kion/meta/natural_keys.yaml` / `kion/meta/references.yaml`: contain
  **only** the original 7 entries plus the 9 **generic** resources. The
  hook/read_transform entries the original sweep appended here were removed
  and moved to the staged files (see below) — this is the corrective pass's
  core change.
- `kion/meta/natural_keys.staged.yaml` / `kion/meta/references.staged.yaml`
  (**new**): hold the natural-key + reference entries for the 21 hook + 1
  read_transform resources, each annotated with a `# classification:
  hook|read_transform — see ONBOARDING_REPORT.md` pointer comment. Header
  comments in both files explain they are inert until a hook lands. Resources
  with zero references (e.g. `app_api_key`, `azure_arm_template`,
  `compliance_check`) still get no `references.*yaml` entry, same convention
  as the original file. Owner fields (`owner_user_ids`/`owner_user_group_ids`
  and similar, e.g. `car_restricted_user_ids`) are **not** modeled as
  references anywhere -- per CLAUDE.md and the existing `resolve_owners()`
  convention, they're resolved by email/name, not by the id->natural-key
  reference path.
- `kion/meta/load.py` `READ_OVERRIDES`: **no changes**. All 30 non-skip
  resources from the original sweep already have a complete
  `read_path`/`read_method` in the vendored `generator_config.yaml` (verified
  by loading `load_resource_meta()` and checking every onboarded resource
  resolves a non-null `read_path`) — Step 2 of the task was a no-op for this
  batch, and the 3 newly-analyzed resources (`permission_scheme`,
  `project_cloud_access_role_exemption`, `project_enforcement`) likewise
  resolve a `read_path` from the vendored config with no override needed.
- `tests/test_meta_load.py`: `ONBOARDED_HOOK`/`ONBOARDED_READ_TRANSFORM` were
  replaced with `STAGED_HOOK`/`STAGED_READ_TRANSFORM`, and
  `test_engine_meta_returns_the_onboarded_resources` now asserts the active
  set is exactly the original 7 + the 9 generic resources (**not** the staged
  ones). A new `test_no_staged_hook_or_read_transform_resource_is_active` test
  asserts none of the 22 staged resources ever appear in `engine_meta()`'s
  active `resources` list.
- `tests/test_onboarded_metadata.py`: reworked so generic resources are
  asserted to be in the ACTIVE files (and absent from staged), and
  hook/read_transform resources are asserted to be in the STAGED files (and
  absent from active) — plus the same `engine_meta()`-active-set safety guard,
  a `read_path` check, a `parent_field` check for `name_in_parent` kinds, and
  a references-target-is-known-resource check (now checking staged targets
  against the active+staged union, since a staged resource's reference may
  point at another staged resource that isn't active yet either). A new
  `test_proposal_count_and_classification_tally_matches_49` locks in the
  corrected 49-resource, 4-way tally.

## Important: metadata alone does not make the 21 hook / 1 read_transform resources safe for a live `--engine` run yet

See "Status (corrective pass)" and "Why the safety gate exists" at the top of
this report for the full explanation and the ACTIVE/STAGED/SKIP counts. Short
version: adding a `natural_keys.yaml` entry is what makes a resource appear in
`engine_meta()`'s `resources` set, which is what `kion_copy.py --engine` feeds
into `build_inventory()`/`EngineReconciler`. For the 9 **generic** resources
that's the whole story -- they're correctly and safely reconcilable right now,
and their metadata is ACTIVE. For the 21 **hook** and 1 **read_transform**
resources, no hook is registered for them in `kion/overrides/registry.py`
(or a reader in `kion/engine/inventory.py` for `user`) -- so their metadata is
now STAGED (`kion/meta/natural_keys.staged.yaml` /
`kion/meta/references.staged.yaml`, not loaded by `load.py`/`setup.py`)
instead of active, specifically to prevent the incomplete/wrong payloads
(unmapped owner ids, dropped type-specific nesting, etc.) or outright create
failures a live `--engine` run against them would otherwise produce. This
matches this repo's own precedent of metadata-then-hook as a two-phase
pattern -- see `kion/engine/reconcile.py`'s docstring, which already documents
that "only `account` has a hook so far (billing_source/budget/scope hooks
land in Task 10)." The implement-phase worklist below is exactly these 22
staged resources.

## Full resource table

| resource | classification | natural_key | #refs | needs_hook | reason/notes |
|---|---|---|---|---|---|
| `account_linkage` | skip | - | 2 | no | account_linkage has no usable identity expressible with the engine's 4 supported natural-key kinds (name, name_in_parent, account_number, date_range). Its create payload is entirely (azure_object_id,... |
| `ami` | hook | name_in_parent(account_id) | 1 | yes | Standard single-record entity CRUD (POST /v3/ami, GET /v3/ami/{id}/PATCH/DELETE by id) — not listed in crud_archetypes.yaml (so it's the default entity archetype, not compound_key_parent_read/no_read/blended/etc.) and... |
| `app_api_key` | generic | name | 0 | no | create_fields is just {name: required string} with no FK/reference fields and no owner fields (ignores already strip the envelope: status/record_id/data). Identity is a plain unique name, matching the {kind:name}... |
| `app_role` | generic | name | 0 | no | Create payload has exactly one field, name (required, string) — no other create_fields, no FK/reference fields, no owner fields. Natural key {kind: name} fits directly. The vendor archetype is 'blended' (typed public... |
| `aws_resource_tag` | skip | - | 0 | no | crud_archetypes.yaml declares aws_resource_tag as kind: no_read — same bucket as ou_cloud_access_role_exemption / project_cloud_access_role_exemption. There is no GET-by-id / listable read shape suited to building an... |
| `azure_arm_template` | hook | name | 0 | yes | Standalone entity (not in crud_archetypes.yaml, so plain single-record GET /v3/azure-arm-template/{id}; not in memberships.yaml, so owner ids are set directly in the create body rather than via separate add/remove... |
| `azure_policy` | hook | name | 0 | yes | azure_policy is a standard by-id entity (not in crud_archetypes.yaml, so no compound-key/no-read complication) but its create payload differs materially from the generic 'fields minus ignores' shape in two independent... |
| `azure_role` | hook | name | 2 | yes | Standalone entity with a clean by-id read/create (not in crud_archetypes.yaml, so no compound-key/no-read complication) and a simple required `name` field, which alone would be generic. But its create payload also... |
| `billing_rule` | generic | name | 1 | no | Standalone entity: simple GET /v3/billing-rule/{id} read (no compound-key parent-read, no memberships.yaml entry, no crud_archetypes.yaml override). Has a plain required 'name' field for identity and its only FK-shaped... |
| `category` | generic | name | 1 | no | Standalone entity with a simple name identity. Not listed in crud_archetypes.yaml (so it's the plain entity archetype, not compound_key_parent_read/no_read/etc.), not listed in memberships.yaml (no owner add/remove... |
| `cft` | hook | name | 0 | yes | cft is a standalone, org-level entity (no ou_id/project_id on create — it isn't parented like ou/project/scope) so the natural key itself is trivially {kind: name}. But its create body carries owner_user_ids /... |
| `cloud_rule` | hook | name | 14 | yes | Natural key is a clean standalone {kind: name} (name is required, top-level, no parent scoping). But the create payload has 12 id/id-array reference fields plus owner_user_ids/owner_user_group_ids, and one of those... |
| `compliance_check` | hook | name | 0 | yes | Not listed in crud_archetypes.yaml (plain entity archetype: single-record read at /v3/compliance/check/{id}) and not listed in memberships.yaml (no separate owner add/remove endpoints for compliance_check specifically... |
| `compliance_control` | hook | name | 6 | yes | Standalone (flat, non-nested) create/read paths give it a plausible {kind: name} identity, but the payload is dominated by six array-of-ids fields whose target resource types are only partly certain from the extracted... |
| `compliance_family` | generic | name_in_parent(compliance_program_id) | 1 | no | Standard entity CRUD (single-record GET by id, POST create), not listed in crud_archetypes.yaml or memberships.yaml so it needs no special archetype or owner/membership handling. Its create payload is exactly... |
| `compliance_level` | generic | name_in_parent(compliance_program_id) | 1 | no | Standalone entity CRUD: POST /v4/compliance/level to create, GET /v4/compliance/level/{id} to read a single record (not compound-key, not in crud_archetypes.yaml so it uses the default entity archetype). Create fields... |
| `compliance_program` | generic | name | 0 | no | Standard single-record entity: POST /v4/compliance/program creates, GET /v4/compliance/program/{id} reads a single record (not listed in crud_archetypes.yaml, so it uses the default entity archetype — no compound key,... |
| `compliance_standard` | hook | name | 2 | yes | Standalone entity (no ou_id/project_id in create_fields, no compound key) so the natural key is trivially {kind: name} -- not listed in crud_archetypes.yaml, so it is the plain entity archetype. But three fields make... |
| `custom_variable` | hook | name | 0 | yes | Standalone, workspace-global resource with a clean {kind: name} identity and no bespoke identity/compound-key problem — but its create payload cannot be produced by the generic 'fields minus ignores, refs mapped to... |
| `custom_variable_override` | skip | - | 0 | no | Two independent disqualifiers. (1) Identity is not expressible with any of the 4 supported natural_key kinds (name / name_in_parent / account_number / date_range): the create/read path is... |
| `dashboard` | skip | - | 0 | no | The create endpoint (POST /beta/dashboard) has no typed request body at all -- the schema slice shows create_fields: {} and crud_archetypes.yaml independently confirms this at the SDK-generation level ('dashboard: kind:... |
| `funding_source_enforcement` | skip | - | 2 | yes | No usable identity field exists among the create fields (cloud_rule_id, description, overburn, spend_option, threshold, timeframe, ugroup_ids, user_ids) - there is no 'name'/slug. The record's real identity is... |
| `funding_source_permission_mapping` | skip | - | 0 | no | This is Kion's app-role permission mapping for a funding source — CLAUDE.md's 'Out of scope' section already names 'app-role permission mappings' as explicitly not copied by this tool (and the app_role.json proposal... |
| `gcp_iam_role` | hook | name | 0 | yes | gcp_iam_role is a standalone entity ({kind: name}, no parent scoping field in the create body) with a plain GET-by-id read and POST create -- structurally it looks generic. But its create fields include two pairs of... |
| `gcp_service_account` | skip | - | 1 | no | gcp_service_account is a GCP provider-registration prerequisite, not portable org/financial structure. Its required identity fields (email, unique_id) are assigned by Google Cloud when the real GCP service account is... |
| `global_permission_mapping` | skip | - | 3 | yes | No usable natural key among the engine's 4 supported kinds (name, name_in_parent, account_number, date_range). This is not an entity but an association/membership record: crud_archetypes.yaml classifies it as... |
| `iam_policy` | hook | name | 2 | yes | Standalone global entity (name-keyed, no compound key, absent from crud_archetypes.yaml so plain by-id GET/POST entity archetype applies) — but the create body carries two id-list reference families that the existing... |
| `idms` | generic | name | 0 | no | Standard CRUD entity: not listed in crud_archetypes.yaml (so it uses the default 'entity' archetype, not compound_key_parent_read/no_read), and not listed in memberships.yaml (no owner/member add-remove endpoints to... |
| `idms_group_association` | skip | - | 2 | no | No usable natural key among the four supported kinds (name, name_in_parent, account_number, date_range). The create fields are assertion_name, assertion_regex, idms_id, update_on_login, user_group_id — there is no name... |
| `idms_open_id` | hook | name | 0 | yes | Standalone top-level entity with a plain 'name' identity (fits the {kind:name} pattern used by billing_source/funding_source), so on the surface it looks generic-eligible. But (1) the create payload has an... |
| `label` | skip | - | 0 | no | Label's create payload has no name-like field — only color, key, value (plus id). Its true identity is the (key, value) pair (e.g. Environment=Production vs Environment=Staging are distinct labels sharing a key), a... |
| `ou_cloud_access_role` | hook | name_in_parent(ou_id) | 3 | yes | Natural key (name_in_parent under ou_id) is supported and the create endpoint does take user_ids/user_group_ids directly in its body (per crud_archetypes.yaml's own comment: 'Create sets initial values... the create... |
| `ou_cloud_access_role_exemption` | skip | - | 2 | yes | No usable natural key can be expressed with the engine's 4 supported kinds (name, name_in_parent, account_number, date_range). The create fields are only {id, ou_cloud_access_role_id, ou_id, reason} — there is no... |
| `ou_enforcement` | skip | - | 2 | no | ou_enforcement has no name/business-identity field among its create_fields (cloud_rule_id, description, enabled, overburn, service_id, threshold, threshold_type, timeframe, trigger_planned_amount_type, ugroup_ids,... |
| `ou_note` | hook | name_in_parent(ou_id) | 1 | yes | Standalone name_in_parent resource (name + ou_id) but the create payload has a required create_user_id (integer) field that identifies the note's author/creator by user id on the SOURCE install. Users are not a copied... |
| `ou_permission_mapping` | skip | - | 0 | no | Not a candidate for this engine, on both policy and mechanical grounds. (1) CLAUDE.md's 'Out of scope' list explicitly names 'app-role permission mappings' as not copied. (2) The create/read op (PATCH/GET... |
| `permission_scheme` | hook | name | 0 | yes | Natural key is trivially generic ({kind: name}, name required + unique) but crud_archetypes.yaml tags this 'kind: blended' with a DECLARED NESTED READ SHAPE: the private read returns permission_roles as {permission_id, role_ids[]}, exploded, vs the create body's flat 'roles' field -- the generic fields-minus-ignores path has no reshaping step, so roles cannot round-trip without a hook that also remaps role_ids to app_role... |
| `project_cloud_access_role` | hook | name_in_parent(project_id) | 2 | yes | Identity fits a supported natural-key kind (name_in_parent under project_id, same pattern as scope) and the CRUD archetype is plain entity (real by-id GET, not in crud_archetypes.yaml so it defaults to 'entity'). But... |
| `project_cloud_access_role_exemption` | skip | - | 2 | yes | Project-scoped sibling of ou_cloud_access_role_exemption (also skip): no name field among create_fields (id, ou_cloud_access_role_id, project_id, reason), crud_archetypes.yaml tags it 'kind: no_read' (no by-id GET, only a parent-scoped collection read), and its real identity is a compound (project_id, exempted-role-id) which none of the 4 supported natural_key kinds can express... |
| `project_enforcement` | skip | - | 2 | no | Project-scoped sibling of ou_enforcement/funding_source_enforcement (also skip): no name/business-identity field among create_fields (amount_type, cloud_rule_id, description, notification_emails/frequency, overburn, service_id, spend_option, threshold, threshold_type, timeframe, ugroup_ids, user_ids); crud_archetypes.yaml tags it 'kind: parent_list' (opaque EnforcementID, no by-id GET); real identity is a compound (project parent, timeframe, threshold_type, ...) none of the 4 kinds express... |
| `project_line_item` | skip | - | 4 | no | No usable identity. The create fields are amount, category_id, datecode, description, funding_source_id, payer_id, project_id — there is no name-like field at all, so {kind:name} and {kind:name_in_parent} are both out.... |
| `project_note` | hook | name_in_parent(project_id) | 1 | yes | Structurally the project-scoped sibling of ou_note (already onboarded as 'hook'): same 4 create fields (create_user_id, name, project_id, text), same 'blended' archetype in crud_archetypes.yaml (typed public... |
| `project_permission_mapping` | skip | - | 4 | no | This resource has no usable identity for any of the engine's four supported natural_key kinds (name, name_in_parent, account_number, date_range). Its real identity is the compound (project_id, app_role_id) — there is no... |
| `saml_group_association` | skip | - | 2 | no | No usable identity: create fields are assertion_name, assertion_regex, idms_id, update_on_login, user_group_id -- there is no 'name' field, and the engine's only four natural_key kinds (name, name_in_parent,... |
| `service_catalog` | hook | name_in_parent(account_id) | 1 | yes | Identity and account/region reference fields are clean and generic-shaped (name required, account_id is a normal integer FK to account), but two things push this past the generic path. First,... |
| `service_control_policy` | hook | name | 0 | yes | Standalone entity: not in crud_archetypes.yaml (no compound-key/no-read/parent-list complication) and not in memberships.yaml (no separate owners add/remove endpoint for this resource -- grepped; the only hit is... |
| `user` | read_transform | name | 2 | yes | The create/read shape (email, first_name, idms_id, last_name, mfa, phone, user_group_ids, username) has NO field literally named 'name'. kion/engine/keys.py's natural_key() for kind 'name'/'name_in_parent' always reads... |
| `user_group` | hook | name | 1 | yes | Standalone entity, not in crud_archetypes.yaml (grepped for '^user_group:' -- no match, so it uses the default entity archetype: a plain by-id GET matching a flat POST, not compound_key_parent_read/no_read). name is... |
| `webhook` | generic | name | 0 | no | Standalone resource with a required unique `name` field and no parent scoping — identity is a plain {kind: name}, matching billing_source/funding_source. Not present in crud_archetypes.yaml, so it uses the default... |

## Hook / read_transform implementation worklist (next phase)

The following 21 resources are onboarded at the metadata level (natural key +
references, where applicable) but need a hook registered in
`kion/overrides/registry.py`'s `HOOKS` (or, for `user`, a reader in
`kion/engine/inventory.py`) before `--engine` reconciliation is correct for
them. `hook_sketch` below is transcribed verbatim from each resource's
proposal file -- it is a design sketch for the next phase, not implemented
code, and several call out live-API details (read shapes, id-vs-name
semantics, whether an id field is a stable cross-install constant) that are
explicitly flagged as **unverified against a live install** and must be
checked before implementation.

### `ami` (hook)

build_create_payload wraps two things beyond the plain fields-minus-ignores copy: (1) account_id — resolve via the standard reference mechanism against the target's account inventory (keyed by account_number, same as scope/account's existing payer_id/project_id references), and (2) owner_user_ids/owner_user_group_ids — mirror the OU/funding_source/project owner pattern: export should probe the detail read for owner_users/owner_user_groups objects and capture owner_user_emails/owner_user_group_names (falling back to empty lists if the read doesn't expose them, as funding_source/project do today); import calls resolve_owners(emails, group_names, target_users, target_groups) to rebuild owner_user_ids/owner_user_group_ids on the target, dropping/warning on any that don't resolve. Unlike OU (which requires >=1 owner and therefore falls back to the running user), owner_user_ids/owner_user_group_ids are NOT required on ami create (required:false in the schema), so no forced running-user fallback is needed here — an empty owners list should be a legal create.

### `azure_arm_template` (hook)

build_create_payload hook with two responsibilities. (1) Owners: mirror the ou/funding_source/project pattern -- export-time must capture owner_user_ids/owner_user_group_ids translated to owner_user_emails/owner_user_group_names (the shape ctx.resolve_owners/_pure_resolve_owners already expects), then on import call ctx.resolve_owners(rec, label) to remap to target user/group ids with the running-user fallback (Kion likely does not require >=1 owner here the way OU/project/funding do, since owner fields are optional:false=false i.e. not required -- so on empty resolution, omit the fields rather than forcing a fallback, unless testing shows the API rejects the create without an owner). (2) resource_group_region_id: first check (against a live install) whether the azure region catalog is a fixed set of ids identical across all Kion installs (plausible, analogous to account_type_id) -- if so, pass the value through unchanged, no lookup needed. If ids differ per install, the hook needs a region name/label to re-resolve against the target's region catalog (look for a GET regions-listing endpoint analogous to gcp_regions' /v3/gcp-resources/list-regions in generator_config.yaml, or extend the read of a source record to capture a resource_group_region name if the read payload exposes one) and remap accordingly, similar in spirit to account's provider-specific field mapping.

### `azure_policy` (hook)

Two-sided hook, mirroring the OU/project/funding_source pattern in kion/overrides/registry.py: (A) READ/export side -- the pre-extracted schema slice only shows the top-level create_fields (azure_policy, owner_user_groups, owner_users) and does not expand AzurePolicyDefinitionCreate or the read/detail response shape, so it is UNVERIFIED whether the generic list endpoint (/v3/azure-policy, derived by stripping {id} off read_path) returns owner_users/owner_user_groups at all. Every other owner-bearing entity in this repo hides owners from its LIST read: OU's list (/v3/ou) omits them entirely and export._export_ous does a per-id GET /v3/ou/{id} to fetch owner_users/owner_user_groups (then converts to portable owner_user_emails/owner_user_group_names via _owner_emails/_owner_group_names); funding_source and project's read APIs don't expose owners at all. Given azure_policy's read_path IS already a by-id GET (/v3/azure-policy/{id}), the safest assumption is it follows the OU shape (list omits owners, per-id detail includes them) and needs the same per-id detail bespoke reader in kion/engine/inventory.py's _EXPORT_READERS style (or a plain per-record enrichment loop) before owner_user_emails/owner_user_group_names can be populated. (B) CREATE side -- build_create_payload must: resolve the target-scope inner fields (whatever AzurePolicyDefinitionCreate actually requires -- unknown from this schema slice, needs live-API/swagger inspection) and nest them under {'azure_policy': {...}}; call ctx.resolve_owners(fields, label) and place the result under the top-level 'owner_users'/'owner_user_groups' keys (note these are NOT owner_user_ids/owner_user_group_ids like OU/project/funding_source -- the item shape for those two arrays, e.g. bare ids vs {id: ...} objects, must be confirmed against the live AzurePolicyDefinitionCreate schema, since resolve_owners returns bare id lists and this create field may want a different shape).

### `azure_role` (hook)

build_create_payload resolves 4 id-list fields against the target install instead of the generic name->id reference table: owner_user_ids/owner_user_group_ids and car_restricted_user_ids/car_restricted_user_group_ids. Users should be matched by email/username (not display `name` — no natural_keys.yaml entry or engine/keys.py kind exists for `user` yet); user_groups by name. Any id that fails to resolve is simply dropped from the (optional) list rather than failing the whole azure_role create. If owner_user_ids ends up empty after resolution, no running-user fallback is required here (unlike OU/project/funding_source) because owner_user_ids is not marked required in create_fields — but note that in practice Kion azure-role create may still functionally need at least one owner; verify against a live install before assuming an empty list is accepted.

### `cft` (hook)

Mirror the existing ou/funding_source/project owner pattern: (1) export — probe the detail read for owner_users/owner_user_groups objects and, if present, capture owner_user_emails/owner_user_group_names (empty lists if the read doesn't expose them, same as funding_source/project today); (2) import — call resolve_owners(emails, group_names, target_users, target_groups) to map to owner_user_ids/owner_user_group_ids on the target, dropping/warning on unmatched owners. Unlike OU, owner_user_ids/owner_user_group_ids are NOT required on cft create (required:false in the schema), so — unlike the OU case — there is no need to force a running-user fallback when no owner resolves; passing empty arrays should be a legal create. Everything else in the create body (description, policy, region, regions[], sns_arns, tags[], template_parameters, termination_protection) is plain scalar/array data with no id reference to another Kion resource, so once owners are handled the rest of the payload is a straight fields-minus-ignores copy — the hook only needs to wrap the owner fields, not the whole payload.

### `cloud_rule` (hook)

build_create_payload(fields, ctx): (1) run the standard refmap.to_target_ids() over the 13 declared references exactly like the generic path — all are optional (only 'name' is required on create) so an unresolved single ref (post_webhook_id/pre_webhook_id) drops to null and an unresolved many-ref just empties, neither blocks the create; (2) explicitly drop automation_policy_ids from the outgoing payload (no 'automation_policy' resource is in scope for this copy engine — never emit it as a Reference to a nonexistent target, and never forward it unmapped since that would leak raw source ids); (3) resolve owner_user_ids/owner_user_group_ids via ctx.resolve_owners(...), the same call _ou_payload/_project_payload/_funding_source_payload already make, falling back to the importing user when nothing resolves; this requires cloud_rule's inventory read to also carry owner_user_emails/owner_user_group_names (translate at read time the way export.py already does for ou/project/funding_source) unless a shared generic owners mechanism lands first covering every resource with owner_user_ids/owner_user_group_ids; (4) POST the assembled payload to ['/v3/cloud-rule'].

### `compliance_check` (hook)

build_create_payload(fields, ctx): start payload = {name, description, body} plus the presumed install-stable type/enum ids taken as-is (cloud_provider_id, compliance_check_type_id, frequency_type_id, severity_type_id) and the plain scalars (frequency_minutes, is_all_regions, is_auto_archived, regions). Resolve owners via uids, gids = ctx.resolve_owners(fields, label) and set owner_user_ids/owner_user_group_ids (requires export.py to also emit owner_user_emails/owner_user_group_names for compliance_check the same way it does for ou/project/funding_source -- currently missing). Deliberately OMIT azure_policy_id, compliance_standard_id, compliance_control_ids, and created_by_user_id from the payload (all optional per the schema) rather than pass their raw source ids through -- azure_policy/compliance_standard/compliance_control have no natural_keys.yaml entry yet so there is no way to remap them safely, and created_by_user_id is provenance metadata that shouldn't be force-set to a possibly-nonexistent target user id anyway. Once compliance_standard and compliance_control are themselves onboarded (each needs its own proposal -- compliance_standard has owner/association endpoints per memberships.yaml, compliance_control is a compound_key_parent_read per crud_archetypes.yaml and likely needs 'skip' or a new engine mechanism), revisit adding compliance_standard_id/compliance_control_ids as real references.yaml entries with target compliance_standard/compliance_control.

### `compliance_control` (hook)

build_create_payload must: (1) verify against a live install (not just swagger) what 'cloud_provider_policy_ids' actually references -- if it is a single homogeneous resource type, add it as a plain reference in references.yaml (best guess here is iam_policy, since Kion's generic 'IAM Policy' resource spans AWS and is the closest name match, but this is UNVERIFIED and could instead be per-provider heterogeneous, in which case per-element type detection against source records is required before remapping); (2) determine the shape of 'compliance_levels' (array of compliance_level ids vs array of {compliance_level_id, ...} objects) and remap accordingly -- do not pass through source ids unmodified; (3) decide which side owns the compliance_check<->compliance_control link, since compliance_check.compliance_control_ids and compliance_control.compliance_check_ids appear to be two views of the same association -- setting both independently risks conflicting/duplicate writes, pick one direction (recommend owning it from compliance_check, since compliance_check already needs its own hook for owner_user_ids/owner_user_group_ids) and drop compliance_check_ids from this resource's create payload; (4) fall back to 'title' for identity/display if 'name' is blank, since create_fields marks 'name' required:false which is unusual for a natural key and should be confirmed live; (5) the DELETE route's program_id requirement is out of scope for create/import but flags that controls may need a separate program-attach call this engine doesn't yet model -- confirm whether a freshly POSTed control (with only compliance_family_id set, no program_id) is fully usable on the target, or whether it remains orphaned until attached to a program some other way.

### `compliance_standard` (hook)

build_create_payload hook needs three pieces: (1) created_by_user_id = self.current_user_id unconditionally (it's required and there is no source value worth preserving across installs -- same running-user source already used for the owner fallback, just applied to a different, always-required field, not gated on 'no owners resolved'). (2) owners via the existing resolve_owners(owner_user_emails, owner_user_group_names, target_users, target_groups) helper -- confirm on a live install whether GET /v3/compliance/standard/{id} returns owner_users/owner_user_groups objects (enables real copying, like ou) or hides them (falls back to empty, like funding_source/project); either way this field is NOT required here so an empty result is a legal create (no forced running-user fallback needed for owners specifically, only for created_by_user_id). (3) cloud_rule_id and compliance_check_ids: both optional and both point at siblings reconciled in this same onboarding batch with a return reference the other way (compliance_check.compliance_standard_id, cloud_rule.compliance_standard_ids) -- recommend reconciling compliance_standard BEFORE compliance_check and cloud_rule, and simply omitting/nulling cloud_rule_id and compliance_check_ids at compliance_standard create time (let the reverse FK on compliance_check/cloud_rule attach after compliance_standard already has a target id), rather than trying to forward-resolve ids that don't exist yet. If a later pass wants the compliance_check_ids array populated on the standard too (Kion's association endpoint, PostComplianceStandardAssociations, suggests this is normally a post-create attach step, not a create-time list), that's an update-style operation this create-only engine doesn't perform anyway, so omitting at create is the correct, engine-consistent choice, not a shortfall.

### `custom_variable` (hook)

Two pieces, mirroring existing patterns rather than inventing new ones: (a) READ side — add a bespoke reader (kion/engine/inventory.py's _EXPORT_READERS pattern, like _read_billing_sources/_read_budgets/_read_scopes) that lists /v3/custom-variable and keeps 'default_value' in the record instead of letting the generic ignores-filter drop it, PLUS translates owner_user_ids/owner_user_group_ids -> owner_user_emails/owner_user_group_names (id->email/name lookup against the SOURCE install's /v3/user and /v3/user-group, exactly like export._export_ous does for OU owners) so the ids are portable. (b) CREATE side — a build_create_payload hook (kion/overrides/registry.py, alongside _ou_payload/_project_payload/_funding_source_payload) that starts from {name, description, type, key_validation_regex, key_validation_message, value_validation_regex, value_validation_message}, passes default_value through verbatim as raw JSON (its real shape is string/list/map keyed by `type`, so it must NOT be re-typed, just forwarded), and calls ctx.resolve_owners(fields, label) to get target owner_user_ids/owner_user_group_ids (with the same current-user fallback other owned entities use — confirm with a live install whether Kion actually requires ≥ 1 owner on custom-variable create the way it does for OU/funding/project, since the schema marks both owner fields optional; if not required, resolve_owners' fallback-to-current-user may need to be skippable here to avoid manufacturing an owner that wasn't in the source).

### `gcp_iam_role` (hook)

Export (read_transform, mirrors _export_ous): when reading a gcp_iam_role, additionally resolve owner_user_ids/owner_user_group_ids AND car_restricted_user_ids/car_restricted_user_group_ids against the source's /v3/user and /v3/user-group lists, and store the portable record with owner_user_emails/owner_user_group_names plus car_restricted_user_emails/car_restricted_user_group_names (drop the raw *_ids -- they're source-install-local). Verify first whether the live GET /v3/gcp-iam-role/{id} returns raw id lists or embedded owner_users/owner_user_groups objects (like OU does) -- the extracted create-field schema only shows the CREATE shape, not the READ shape. Import (build_create_payload hook registered for gcp_iam_role in kion/overrides/registry.py): resolve both email/name pairs against ctx.users/ctx.groups using the same lookup as kion.import_.resolve_owners's pure matching logic, but do NOT apply resolve_owners' current-user fallback -- unresolved/empty owner or car_restricted lists should just stay empty (with a dropped-reference warning per missing email/group, same warning style as resolve_owners). Pass name, description, gcp_role_launch_stage, role_permissions, role_denials straight through unchanged (ignores: status/record_id/data per the schema slice).

### `iam_policy` (hook)

build_create_payload for iam_policy: (1) pass through name/description/policy/aws_iam_path/car_restricted verbatim (plain scalars, policy is a raw IAM JSON string with no FK content to remap); (2) resolve owner_user_ids/owner_user_group_ids the same way ou/funding_source/project already do via resolve_owners(emails, group_names, users, groups) in import_.py — but unlike those three, owners are NOT required here (create_fields marks both optional:false-required), so on unresolved/empty just omit the fields rather than falling back to the running user; (3) resolve car_restricted_user_ids by user email and car_restricted_user_group_ids by user_group name against the target's user/user_group inventories, dropping any that don't resolve (mirror the 'dropped' handling resolve_owners already does) since these are optional restriction lists, not creation-blocking; (4) if car_restricted is true but both restriction lists end up empty after remap, still create — Kion doesn't require a matching account for this the way scope does for accounts.

### `idms_open_id` (hook)

build_create_payload should: (1) drop or best-effort-translate 'access_rules' -- inspect its actual runtime shape (likely [{idms_group/claim_value, app_role_id, ...}]) against a live install before deciding; since app_role has no natural-key mapping in this engine yet, the safest default is to omit access_rules on create (shell with no group->role mappings) and report it in the plan output as a manual follow-up, rather than silently dropping data with no trace. (2) treat this like billing_source's shell pattern: create with whatever non-secret config fields are present (issuer, authorization_endpoint, jwks_uri, client_id, claim-mapping fields, scopes) and surface a plan/apply note that client secret + access_rules must be reconfigured by hand on the target, since the read API cannot have exposed a secret and access_rules FK targets aren't resolvable yet.

### `ou_cloud_access_role` (hook)

build_create_payload hook that: (1) copies name/ou_id (mapped via existing ou natural-key inventory) and the scalar/boolean fields (aws_iam_path, aws_iam_role_name, long_term_access_keys, short_term_access_keys, web_access) straight through; (2) resolves user_ids/user_group_ids against the target install by a stable user/group identity (email for users, name for groups) via a direct lookup API call (e.g. GET /v3/user, /v3/user-group filtered/searched by that identity) rather than the standard inventory-map lookup, dropping any that don't resolve and treating a fully-unresolved required-member list as a 'skipped' reference (never failing the whole record for missing members, matching this engine's skip-vs-fail philosophy); (3) unconditionally drops aws_iam_permissions_boundary, aws_iam_policies, and azure_role_definitions since they reference Cloud Rules objects this tool does not copy -- surfaced as a documented, expected data-loss note per record (not a drift/fail); (4) passes aws_session_tags and gcp_iam_roles through as opaque literal arrays (no id translation) after confirming via a live read sample that they are not themselves id-bearing.

### `ou_note` (hook)

build_create_payload hook: (1) resolve ou_id via the standard natural-key remap (same as project's ou_id reference) -- this part is generic; (2) override create_user_id with the target install's running-user id, fetched the same way import_.py already does for OU/project/funding_source owner fallback (GET /v3/app-api-key -> user_id), instead of copying the source install's create_user_id verbatim. text and name pass through unchanged.

### `permission_scheme` (hook)

build_create_payload hook: (1) name and type pass through unchanged (plain scalars, type is presumably a fixed enum stable across installs -- confirm live). (2) roles needs a bespoke read-side AND create-side transform: read must un-explode whatever GET /v3/permission-scheme/{id} actually returns (crud_archetypes.yaml says the underlying private read exposes permission_roles as {permission_id, role_ids[]} pairs -- UNVERIFIED whether the public by-id GET in the schema slice returns the same shape or the already-flat 'roles' the create body wants) into a portable record, translating role_ids (almost certainly app_role ids, since app_role is the only role-shaped resource this engine tracks) to app_role natural keys (app_role is already onboarded as generic with {kind: name}) the same way other hooks translate id-lists via ctx's reference-resolution helpers; permission_id is suspected to be a fixed built-in-permission catalog id stable across installs (analogous to account_type_id) and can likely pass through unchanged, but that must be confirmed against a live install before assuming it, since an install-local permission_id would need its own catalog-lookup, not a natural-key remap. (3) Because references.yaml's flat {field, target, key, many, optional} shape cannot express a reference nested inside an array of objects (roles is [{permission_id, role_ids[]}, ...], not a flat id or id-list field), this mapping cannot be added to references.yaml at all -- it must live entirely inside the hook, one more reason this can't be 'generic'. (4) No owner_user_ids/owner_user_group_ids or other owner fields exist on this resource per the schema slice, so no owner-fallback logic is needed. Note: this proposal only concerns permission_scheme as a copied resource in its own right and does not change the existing DEFAULT_PERMISSION_SCHEME_ID convention CLAUDE.md documents for OU/project/funding_source creates.

### `project_cloud_access_role` (hook)

build_create_payload(fields, ctx): 1) resolve project_id via ctx's normal to_target_ids-style lookup (or ctx.id_map['project']) -- return None (skip) if unresolved, since project_id is required. 2) resolve account_ids (optional, many) the same way scope resolves account_numbers -- drop ids that don't match an existing/created target account, no warning needed (mirrors how scope treats missing accounts). 3) resolve user_ids/user_group_ids: at export time, capture the source user_ids as a portable user_emails list and user_group_ids as a portable user_group_names list (new export-side lookup against source /v3/user and /v3/user-group, analogous to how owner_user_emails/owner_user_group_names are already captured for OU/project/funding_source); on import, map those back to target ids via ctx.users (email->id) / ctx.groups (name->id) -- same dicts _index_ctx already builds -- and append a warning per name/email that doesn't resolve on the target (do NOT fall back to the current user the way resolve_owners does for owner_user_ids; an empty user_ids/user_group_ids list is a valid CAR, unlike OU/project/funding which reject zero owners). 4) pass through name, aws_iam_path, aws_iam_role_name, apply_to_all_accounts, future_accounts, long_term_access_keys, short_term_access_keys, web_access unchanged. 5) explicitly OMIT aws_iam_policies, aws_iam_permissions_boundary, azure_role_definitions, gcp_iam_roles, cloud_provider_ids from the payload and push one warning line per non-empty dropped field (e.g. "project cloud access role 'X': dropped aws_iam_policies (N entries) - not supported by this tool") so the plan/apply output makes the degradation visible instead of silent. Return (['/v3/project-cloud-access-role'], payload).

### `project_note` (hook)

build_create_payload hook: (1) resolve project_id via the standard natural-key remap, identical to how account.py/scope already remap project_id -- this part is generic; (2) override create_user_id with the target install's running-user id (same GET /v3/app-api-key -> user_id fallback import_.py already uses for OU/project/funding_source owners), instead of copying the source install's create_user_id verbatim. name and text pass through unchanged.

### `service_catalog` (hook)

build_create_payload(fields, ctx): (1) resolve account_new = ctx.id_map['account'].get(str(fields.get('__srcid__account_id'))) (or via the standard to_target_ids() reference-resolution pass over the single declared account_id reference); if unresolved, return None so the caller records a skip (mirrors every other hook's 'required ref unresolved -> None' contract). (2) resolve owners via uids, gids = ctx.resolve_owners(fields, label) exactly like _ou_payload/_project_payload/_funding_source_payload — this is the SAME shared hook already on EngineReconciler, no new owner-resolution code needed, just the call-site. (3) pass portfolio_id, region, name, description, tag_option straight through unchanged (opaque scalars, no id remapping). (4) POST to ['/v3/service-catalog']. Also needs a bespoke _export_service_catalog() in export.py (service_catalog isn't in crud_archetypes.yaml so it reads as a plain entity list+detail, but the detail GET must be inspected live to see whether it returns owner_users/owner_user_groups the way OU's detail GET does — if yes, mirror _export_ous' owners_u/owners_g extraction; if the read omits them like funding_source/project, export owner_user_emails/owner_user_group_names as [] and accept the importing-user fallback).

### `service_control_policy` (hook)

Minimal hook, modeled directly on _funding_source_payload/_project_payload (the OPTIONAL-owners variant, not OU's required-fallback variant -- service_control_policy has no analogue of OU/project/funding_source's 'create requires >=1 owner' constraint, so no running-user fallback is needed here; resolve_owners already no-ops to empty lists when nothing resolves and nothing else in create_fields requires a non-empty owner list). build_create_payload(fields, ctx): label = f"service control policy '{fields.get('name')}'"; uids, gids = ctx.resolve_owners(fields, label); payload = {'name': fields.get('name'), 'description': fields.get('description') or '', 'policy': fields.get('policy'), 'owner_user_ids': uids, 'owner_user_group_ids': gids}; return (['/v3/service-control-policy'], payload). No identity_ok/post_create/order_records/adopt_key/reconcile_override needed -- generic list+adopt-by-name at /v3/service-control-policy is fine for the read/adopt side.

### `user_group` (hook)

build_create_payload hook needs four pieces, by analogy with the existing OU/project/funding_source owner hooks in kion/overrides/registry.py plus a new pattern for the viewer/member arrays that has no precedent yet: (1) idms_id -- resolve via the generic id->natural-key reference mechanism against the (also newly onboarded) idms resource, by name; required, so an unresolved idms_id means this record is skipped, not defaulted. (2) owner_user_ids/owner_user_group_ids -- call kion.import_.resolve_owners(rec.get('owner_user_emails'), rec.get('owner_user_group_names'), ctx.users, ctx.groups) exactly as _ou_payload/_project_payload/_funding_source_payload already do, dropping unresolved entries (optional field, partial owner list is a legal create). (3) viewer_user_ids/viewer_user_group_ids -- there is no existing helper for a 'viewer' concept, but resolve_owners is generic enough (it only maps emails->ids and names->ids, nothing owner-specific) to reuse verbatim against a second pair of export fields (viewer_user_emails/viewer_user_group_names) that the export side would need to add, mirroring memberships.yaml's separate 'associations' block for viewers. (4) user_ids (plain member list, no groups) -- resolve via a bare email->id lookup against ctx.users (the user half of resolve_owners), sourced from a new export field (member_user_emails or similar); this is the slice_members entry in memberships.yaml. All four of (2)-(4) additionally require confirming the READ shape: per kion/engine/inventory.py, the generic engine reads the LIST endpoint (list_path derived by stripping '/{id}' off read_path -> GET /v3/user-group), and the OU precedent (kion/export.py _export_ous) shows the list read omits owners entirely -- a separate per-id GET /v3/user-group/{id} is required to get owner_users/owner_user_groups (and, presumably, viewer_user_ids/viewer_user_group_ids/user_ids) at all. This is UNVERIFIED here since the pre-extracted schema slice only covers create_fields, not the read/detail response shape -- must be confirmed against a live install before implementing, exactly like the same open question flagged in the azure_policy.json and compliance_standard.json proposals.

### `user` (read_transform)

Add _read_users(client) to kion/engine/inventory.py (peer of _read_billing_sources/_read_budgets/_read_scopes): GET /v3/user (list), and for each raw record set fields['name'] = record.get('username') before the natural-key + to_natural step -- mirroring _finish_export_record's role for billing_source/budget/scope. Register 'user' in an _EXPORT_READERS-style dict so build_inventory routes it through this reader instead of the generic list_records + record.items() loop. Add a matching branch in EngineReconciler._index_target (mirroring the existing `if res == 'billing_source':` special-case) that lists /v3/user, applies the same name := username synthesis, and populates _t_key/_t_ids -- the generic branch at the bottom of _index_target would otherwise index every target user under the same empty key. With that read-side plumbing in place, no build_create_payload hook is needed for the create side itself: the generic to_target_ids(fields, refs, ...) + 'payload = fields minus ignores minus __srcid__' path in reconcile.py's main loop can build the POST /v3/user body once idms_id and user_group_ids are metadata-mapped via references.yaml.


## Skip resources (not onboarded -- reasons)

### `account_linkage`

account_linkage has no usable identity expressible with the engine's 4 supported natural-key kinds (name, name_in_parent, account_number, date_range). Its create payload is entirely (azure_object_id, azure_principal_name, payer_id, user_id) -- no 'name' field, and its true identity is a compound key (payer_id + azure_object_id/user_id), which keys.py explicitly does not support. It also has no entry in crud_archetypes.yaml or memberships.yaml (not a compound_key_parent_read/no_read/entity archetype, not an owner/member membership pair) -- it's an unmodeled one-off. Beyond the mechanical natural-key problem, this resource is Azure-specific delegated-admin plumbing: azure_object_id/azure_principal_name are identifiers inside the *source* customer's own Azure AD tenant and have no meaning in the target install's tenant, so even a hand-rolled hook couldn't produce a valid create payload on the target without external (non-Kion-API) input. It's also downstream of Azure billing sources, which this tool already skips on import per CLAUDE.md ('gcp/azure/anthropic are exported but skipped (need a prerequisite service account or provider registration flow)') -- so payer_id could never resolve to a target billing source anyway, making account_linkage unreachable even if it had a valid key.

### `aws_resource_tag`

crud_archetypes.yaml declares aws_resource_tag as kind: no_read — same bucket as ou_cloud_access_role_exemption / project_cloud_access_role_exemption. There is no GET-by-id / listable read shape suited to building an inventory record for reconcile (adopt/create/drift) the way the engine's entity archetype requires; this is a new engine mechanism the current importer doesn't support (comparable to compound_key_parent_read needing bespoke handling). Independently, its identity is the pair (resource_key, resource_value) — a compound key with no parent scope — which is not expressible in any of the 4 supported natural_key kinds (name, name_in_parent, account_number, date_range): it isn't a single 'name' field (both fields are needed to disambiguate, since multiple values can exist per key), it has no parent_field for name_in_parent, and it's not an account_number or date_range. Both the read-shape problem and the identity problem independently rule out 'generic' or a simple 'hook'.

### `custom_variable_override`

Two independent disqualifiers. (1) Identity is not expressible with any of the 4 supported natural_key kinds (name / name_in_parent / account_number / date_range): the create/read path is /v3/account/{account_id}/custom-variable/{custom_variable_id} in the schema slice, but the vendor archetype note says the real resource is entity-polymorphic (account, OU, or project can each be the parent, via entity_id + custom_variable_id path params), so its true identity is a compound key (entity_type, entity_id, custom_variable_id) with no name field at all -- the only create field is 'value'. That compound/polymorphic shape needs a new engine mechanism (as crud_archetypes.yaml itself acknowledges by giving it its own bespoke 'cv_override' archetype, not one of the generic ones). (2) Custom variables are explicitly listed as out of scope for this copy engine in the project's own CLAUDE.md ('Out of scope (not copied): ... labels, custom variables, compliance, app-role permission mappings.').

### `dashboard`

The create endpoint (POST /beta/dashboard) has no typed request body at all -- the schema slice shows create_fields: {} and crud_archetypes.yaml independently confirms this at the SDK-generation level ('dashboard: kind: raw_http ... the public /beta create+update take NO typed body in the SDK (PostDashboard(ctx)/PatchDashboard(ctx,params))'). PostDashboard takes only ctx: no name, no layout/widgets, no owner/sharing fields -- it appears to spin up a blank/default dashboard whose actual content (name, widget layout, etc.) is then set via PatchDashboard, whose shape is also untyped/unknown from what's available here. With zero create fields there is nothing to key a natural_key on (no name field is exposed to create), nothing to build a create payload from, and no visibility into what the PATCH body needs -- so neither the generic path nor a hook can be written responsibly without first reverse-engineering the PATCH body against a live install (out of scope for this read-only proposal). This is a provider-flow-shaped resource, not a metadata gap.

### `funding_source_enforcement`

No usable identity field exists among the create fields (cloud_rule_id, description, overburn, spend_option, threshold, timeframe, ugroup_ids, user_ids) - there is no 'name'/slug. The record's real identity is structural: (funding_source_id, threshold, timeframe[, spend_option, cloud_rule_id]) - a multi-attribute composite, not a single name or a (start,end) date pair. None of the engine's 4 supported natural_key kinds (name, name_in_parent, account_number, date_range) can express this. Per kion/meta/vendor/crud_archetypes.yaml this resource is declared 'parent_list' (same shape as project_enforcement/ou_enforcement): there is no by-id GET - read enumerates all enforcement rows under the parent funding source and finds the target by numeric id, an id that only exists after create and isn't derivable from source data. That is exactly the 'compound-key parent-read that needs a new engine mechanism' skip case called out in the task instructions.

### `funding_source_permission_mapping`

This is Kion's app-role permission mapping for a funding source — CLAUDE.md's 'Out of scope' section already names 'app-role permission mappings' as explicitly not copied by this tool (and the app_role.json proposal calls this out too). Independent of that policy boundary, it also fails the mechanical requirements: crud_archetypes.yaml classifies it as kind: association (key_field: app_role_id, parent_field: funding_source_id, member_fields: [user_ids, user_groups_ids]) — an SDK-generator archetype not among this engine's supported kinds in crud_archetypes.yaml's own comment header (entity/no_read/compound_key_parent_read/etc. are what this repo's importer models; 'association' isn't handled by keys.py at all). The schema slice shows create and read as the SAME endpoint (PATCH and GET /v3/funding-source/{id}/permission-mapping) with create_fields empty in the pre-extraction — because the real payload is an array of {app_role_id, user_ids, user_groups_ids} tuples replacing the whole mapping set for the funding source, not a flat object with scalar fields. The item identity within that array is the compound pair (funding_source_id, app_role_id), which is not expressible in any of the 4 supported natural_key kinds (name, name_in_parent, account_number, date_range): there is no 'name' field on a mapping entry, name_in_parent needs record.get('name') which doesn't exist here, and app_role_id is an FK not a literal name.

### `gcp_service_account`

gcp_service_account is a GCP provider-registration prerequisite, not portable org/financial structure. Its required identity fields (email, unique_id) are assigned by Google Cloud when the real GCP service account is created -- POSTing arbitrary values to the target install's /v3/gcp/service-account would not make the target actually own or control that GCP service account; a 'shell' copy would either fail Kion/GCP validation or become a dangling, non-functional record pointing at credentials that live in the source customer's GCP org. The create payload also requires oauth_client_secret; following this codebase's established billing_source precedent (CLAUDE.md: 'the read exposes config but never secrets ... key_secret ... redacted'), a credential secret like this is essentially certain not to be returned by GET /v3/gcp/service-account/{id}, so even a hand-rolled hook could not recover a value to replay on create -- read_transform/hook can't paper over a field the read API never gives back. gcp_project_id is likewise a real Google Cloud project identifier, not a Kion resource id, so there is no FK translation that would make it valid on a different install. This resource is exactly the 'prerequisite service account ... provider registration flow' CLAUDE.md already cites as the reason gcp/azure/anthropic billing sources are skipped on import, and this proposal set's own dashboard.json precedent independently names gcp_service_account (alongside idms) as an established provider-flow-prerequisite skip. Mechanically it is unremarkable -- no entry in crud_archetypes.yaml (plain entity archetype: by-id GET/POST/PATCH/DELETE, no compound-key/no-read weirdness) and no entry in memberships.yaml (no owner/member add-remove split) -- so the engine *could* render it generically off the 'name' field, but doing so would silently create broken external-provider registrations on the target, which is worse than not copying it at all.

### `global_permission_mapping`

No usable natural key among the engine's 4 supported kinds (name, name_in_parent, account_number, date_range). This is not an entity but an association/membership record: crud_archetypes.yaml classifies it as kind:association with key_field=app_role_id and member_fields=[user_ids, user_groups_ids] — i.e. 'these users/groups get this app_role's permissions, globally'. It has no name field and no containing parent (it's global-scope, unlike ou/project/funding_source_permission_mapping which at least have a parent_field); its only identity is the app_role_id it's keyed on, which is a reference, not a literal name/date/account-number the 4 kinds can express. It would need a new natural_key kind (e.g. 'keyed by a single FK') that keys.py does not support, so per instructions it must be skip/hook rather than inventing one. Additionally its two references — app_role (via app_role_id) and user/user_group (via user_ids/user_groups_ids) — are resources this engine does not copy: users and user_groups are identity/SSO-managed and out of scope entirely, and this repo's CLAUDE.md explicitly lists 'app-role permission mappings' under 'Out of scope (not copied)'. Onboarding this resource would require both a new engine mechanism (association/membership natural-key + create path, distinct from the existing owners-via-memberships.yaml pattern) and copying two resources (app_role, user_group) that are deliberately excluded today.

### `idms_group_association`

No usable natural key among the four supported kinds (name, name_in_parent, account_number, date_range). The create fields are assertion_name, assertion_regex, idms_id, update_on_login, user_group_id — there is no name field at all, so `name` and `name_in_parent` are both out (name_in_parent still needs record.get('name')). Its real identity is a compound of (idms_id, user_group_id, assertion_name/assertion_regex), which none of the four kinds express, and inventing a new kind is explicitly disallowed. Separately, its only structural parent-ish field, idms_id, references an IDMS (SAML/OIDC/LDAP identity-provider) record, which is itself an out-of-scope prerequisite resource (config with redacted secrets, install-specific SSO wiring) not present in the natural_keys/references set and not being onboarded here — so even a hook could not reliably remap idms_id across installs. Both problems are independently disqualifying.

### `label`

Label's create payload has no name-like field — only color, key, value (plus id). Its true identity is the (key, value) pair (e.g. Environment=Production vs Environment=Staging are distinct labels sharing a key), a compound two-scalar-field identity not expressible by any of the four supported natural_key kinds: 'name' hardcodes record.get('name') (no such field exists on label); 'name_in_parent' requires a hierarchical parent FK (label has none — GET /v3/label is a flat, unscoped list, not a compound_key_parent_read); 'account_number' and 'date_range' are unrelated shapes. Aliasing 'key' alone as a synthetic name (via a read_transform) would be unsafe: it would make the engine match/adopt on key only, silently colliding or misassigning distinct label values under the same key. Getting this right needs a new natural_key kind (something like a 2-field 'field_pair' over key+value), which the task explicitly says not to invent. Separately, and independently sufficient: this repo's CLAUDE.md already lists 'labels' under 'Out of scope (not copied)'.

### `ou_cloud_access_role_exemption`

No usable natural key can be expressed with the engine's 4 supported kinds (name, name_in_parent, account_number, date_range). The create fields are only {id, ou_cloud_access_role_id, ou_id, reason} — there is no name/label field, and the record's real identity is the compound pair (ou_id, ou_cloud_access_role_id), which name_in_parent cannot represent (it needs a literal `name` string within a parent, not a second FK). This matches crud_archetypes.yaml, which already tags this resource `kind: no_read` — there is no by-id or list-by-id GET; the only read (`GET /v3/ou/{id}/cloud-rule/exemption`) is a collection nested under the parent OU, i.e. compound-key-parent-read shaped, but that mechanism (see scope_criteria) is also not exposed to this Python engine (keys.py only knows the 4 kinds above). Separately, ou_cloud_access_role_id references `ou_cloud_access_role`, a resource that is not one of the 7 entities this engine currently copies (billing_source/ou/funding_source/project/budget/account/scope) and has no natural-key/reference entry of its own — so even with a compound-key mechanism, the reference target wouldn't resolve on the target install today.

### `ou_enforcement`

ou_enforcement has no name/business-identity field among its create_fields (cloud_rule_id, description, enabled, overburn, service_id, threshold, threshold_type, timeframe, trigger_planned_amount_type, ugroup_ids, user_ids) -- none of these is a stable, human-assigned label. Per crud_archetypes.yaml it is archetype 'parent_list': records live under an OU (path /v3/ou/{id}/enforcement) and are individually addressed only by an opaque EnforcementID assigned on creation. Its real identity is a compound of (ou parent, timeframe, threshold_type, and optionally cloud_rule_id/service_id) -- an OU can legitimately have multiple enforcement rows sharing a timeframe if they're scoped to different cloud rules or services. None of the 4 supported natural_key kinds in kion/engine/keys.py (name, name_in_parent, account_number, date_range) can express this: there is no 'name' field for name/name_in_parent, no account_number, and no start/end datecode pair for date_range. Fabricating a synthetic compound key is explicitly disallowed by the task instructions ('do NOT invent a new kind'). Additionally its one truly cross-resource field, cloud_rule_id, points at 'cloud_rule', which CLAUDE.md documents as explicitly out of scope for this tool ('Cloud accounts, cloud rules, labels, custom variables, compliance, app-role permission mappings' are not copied) -- so even a hook couldn't fully resolve every enforcement row.

### `ou_permission_mapping`

Not a candidate for this engine, on both policy and mechanical grounds. (1) CLAUDE.md's 'Out of scope' list explicitly names 'app-role permission mappings' as not copied. (2) The create/read op (PATCH/GET /v3/ou/{id}/permission-mapping) operates on the FULL mapping collection for an OU in a single call — it is a bulk overwrite of every app_role_id→{user_ids,user_groups_ids} pair for that OU, not a per-record CRUD endpoint, so the schema slice's create_fields came back empty ({}) and there is no single-record shape to model. (3) Even if each mapping row were treated as its own entity, its identity is the compound pair (ou_id, app_role_id) — app_role_id is a foreign key, not a name string — which cannot be expressed by any of the 4 supported natural_key kinds in kion/engine/keys.py (name, name_in_parent [requires a literal `name` field], account_number, date_range). (4) The referenced entities on the far side — app_role (via app_role_id) and user/user_group (via user_ids/user_groups_ids) — are not resources this engine copies or exposes natural keys for (not in kion/meta/natural_keys.yaml), so reference translation has no target even if identity were solved. This matches the vendor archetype file's own treatment: ou_permission_mapping is tagged kind: association there (SDK-generator concept), which has no counterpart among this engine's supported natural-key kinds or archetypes (name / name_in_parent / account_number / date_range) and no analogous compound-key mechanism has been built into kion/engine/.

### `project_cloud_access_role_exemption`

This is the exact project-scoped sibling of `ou_cloud_access_role_exemption` (already onboarded as 'skip' in this sweep), and fails for the same two independent reasons. (1) No usable natural key: the create fields are only {id, ou_cloud_access_role_id, project_id, reason} -- there is no name/label field, and the record's real identity is the compound pair (project_id, the exempted cloud-access-role id), which none of the engine's 4 supported kinds (name, name_in_parent, account_number, date_range) can express (name_in_parent needs a literal `name` string within a parent, not a second FK). This matches crud_archetypes.yaml, which tags this exact resource `kind: no_read` -- there is no by-id or list-by-id GET; the only read (GET /v3/project/{id}/cloud-rule/exemption per the schema slice) is a collection nested under the parent project, i.e. compound-key-parent-read shaped, a mechanism keys.py does not support (only the 4 kinds above). (2) The FK it exempts against is `project_cloud_access_role` (onboarded this sweep as 'hook', not yet reconcilable), so even with a compound-key mechanism the reference target's ids would not be stable/resolvable until that hook lands. Schema note: the create_fields field is literally named `ou_cloud_access_role_id` even on this project-scoped endpoint (not `project_cloud_access_role_id`) -- copied verbatim from the schema slice, either genuine API field reuse or a swagger/codegen inconsistency; doesn't change the skip verdict.

### `project_enforcement`

`project_enforcement` is the project-scoped sibling of `ou_enforcement`/`funding_source_enforcement` (both already onboarded 'skip' in this sweep) and fails for the same structural reason: no name/business-identity field exists among its create_fields (amount_type, cloud_rule_id, description, notification_emails, notification_frequency, overburn, service_id, spend_option, threshold[required], threshold_type, timeframe[required], ugroup_ids, user_ids) -- none is a stable, human-assigned label. Per crud_archetypes.yaml it is archetype `parent_list` (parent_id_field: project_id, child_param: EnforcementID): records live under a project (GET/POST /v3/project/{id}/enforcement per the schema slice) and are individually addressed only by an opaque EnforcementID assigned on creation -- there is no by-id GET. Its real identity is a compound of (project parent, timeframe, threshold_type, and optionally cloud_rule_id/service_id/spend_option), since a project can legitimately have multiple enforcement rows sharing a timeframe if scoped to different cloud rules or services. None of keys.py's 4 supported kinds can express this compound identity, and a new kind must not be invented for this pass. Its one truly cross-resource field, cloud_rule_id, points at `cloud_rule`, which CLAUDE.md documents as explicitly out of scope for this tool -- so even a hook couldn't fully resolve every enforcement row. Note: user_ids/ugroup_ids are enforcement *notification recipients*, not object owners -- they must NOT feed the create-time owner-fallback-to-running-user logic used for OU/project/funding_source.

### `project_line_item`

No usable identity. The create fields are amount, category_id, datecode, description, funding_source_id, payer_id, project_id — there is no name-like field at all, so {kind:name} and {kind:name_in_parent} are both out. {kind:account_number} obviously doesn't apply. {kind:date_range} requires start_datecode/end_datecode (as budget uses), but project_line_item only has a single `datecode` int, not a range, so that kind doesn't fit either. The record's real identity is a compound key — something like (project_id, funding_source_id, category_id, datecode) — which is not expressible with any of the 4 kinds keys.py supports (name, name_in_parent, account_number, date_range). Per the task instructions, a resource whose identity is a compound not expressible with those 4 kinds must be classified 'skip' rather than inventing a new natural_key kind.

### `project_permission_mapping`

This resource has no usable identity for any of the engine's four supported natural_key kinds (name, name_in_parent, account_number, date_range). Its real identity is the compound (project_id, app_role_id) — there is no 'name' field on the record at all; the record is a per-project-per-role membership entry ({app_role_id, user_ids, user_groups_ids}). The vendored crud_archetypes.yaml (from the Terraform-provider generator, a different codebase) classifies it as kind: association with key_field: app_role_id and parent_field: project_id — i.e. exactly a compound-key/parent-scoped association, which keys.py has no kind for. Read and create are also the SAME bulk endpoint (PATCH/GET /v3/project/{id}/permission-mapping) that replaces the entire list of role mappings for a project in one call, not a per-record CRUD entity — so there is no single-record create to drive from a natural-key-keyed inventory row; the whole array must be built and PATCHed together. Additionally, and decisively, this repo's own CLAUDE.md explicitly lists 'app-role permission mappings' under '## Out of scope (not copied)' alongside cloud accounts/cloud rules/labels/custom variables/compliance — this tool intentionally copies org/financial structure only, not RBAC/permission assignments. Onboarding this resource would require inventing a new natural_key kind (compound parent+role) plus a bespoke bulk-PATCH hook that translates app_role_id -> permission_scheme identity and user_ids/user_groups_ids -> target user/user_group ids, which is out of scope for the metadata-driven engine and contradicts the project's stated scope.

### `saml_group_association`

No usable identity: create fields are assertion_name, assertion_regex, idms_id, update_on_login, user_group_id -- there is no 'name' field, and the engine's only four natural_key kinds (name, name_in_parent, account_number, date_range) can't express this resource's real identity, which is compound: (idms_id, user_group_id, assertion_name/assertion_regex). name_in_parent doesn't fit either since there's no single 'name' string being scoped under a parent -- assertion_name and assertion_regex are alternative matcher fields, not a stable label. Separately, this resource has no plain list-read: the vendor generator config shows its only enumeration path is /v3/idms/{id}/group-association (nested under each idms record, i.e. a parent-scoped/compound-key read, same shape as the scope_criteria compound_key_parent_read archetype) -- this engine's snapshot walk only knows how to GET a flat list per entity, not fan out over a parent resource's children. On top of that, its two references are both to resources outside this engine's copied set: idms_id points at an Identity Management System (SAML/OIDC provider config -- a prerequisite/provider-registration resource, analogous to the explicitly-called-out gcp_service_account skip case) and user_group_id points at user_group, which is not one of the 7 entities this tool copies (out of scope per CLAUDE.md: org/financial structure only, not users/groups/permissions). Onboarding this resource would require (a) a new engine mechanism for parent-scoped enumeration, (b) a new natural-key kind for compound identities, and (c) bringing idms and user_group into scope as prerequisite entities first -- none of which exist today.


## Live read smoke (active resources)

Ran a throwaway, read-only script (not committed) against **demo1** (`.env.source`) that calls `engine_meta()` to get the ACTIVE resource set, then `build_inventory(client, meta, refs, nkeys, resources)` -- the exact same generic read path `kion_copy.py export` and `scripts/equivalence_check.py` use -- for all 16 active resources (the original 7 plus the 9 "generic" resources). No writes were made; `.env.source` was only ever read from.

Result: **15 of 16 read cleanly with real data**; 2 of those 15 (`compliance_family`, `compliance_level`) returned 0 records each via a *caught*, non-fatal error rather than genuine emptiness -- flagged below, not silently dropped.

| resource | result |
|---|---|
| account | ok — 102 records |
| app_api_key | ok — 9 records |
| app_role | ok — 75 records |
| billing_rule | ok — 8 records |
| billing_source | ok — 17 records |
| budget | ok — 110 records |
| category | ok — 1 record |
| compliance_family | **flagged** — 0 records, list read returned HTTP 405 (see below) |
| compliance_level | **flagged** — 0 records, list read returned HTTP 405 (see below) |
| compliance_program | ok — 28 records |
| funding_source | ok — 65 records |
| idms | ok — 11 records |
| ou | ok — 22 records |
| project | ok — 36 records |
| scope | ok — 9 records |
| webhook | ok — 10 records |

**Flag: `compliance_family` / `compliance_level` are not cleanly "generic" on this install.** Both are classified `generic` with a `{kind: name_in_parent, parent_field: compliance_program_id}` natural key -- that part of the classification is still structurally correct (mirrors `compliance_family`'s and `compliance_level`'s parent-scoping under `compliance_program`, same shape as `ou`/`project`). The problem is the *generic list-read mechanism* `build_inventory` uses for every non-bespoke resource: it derives the LIST endpoint by stripping `{id}` off `read_path` (`/v4/compliance/family/{id}` -> `GET /v4/compliance/family`, `/v4/compliance/level/{id}` -> `GET /v4/compliance/level`). Against demo1 both calls returned **HTTP 405** (method not allowed), printed as `! /v4/compliance/family list failed: 405` / `! /v4/compliance/level list failed: 405` on stderr. `kion/engine/inventory.py`'s `list_records(..., on_error=_on_read_error)` **catches** this and returns an empty list rather than raising -- so a live `--engine` run would not crash, but would silently believe demo1 has zero compliance families and zero compliance levels (a data-completeness gap, not a hard failure) and therefore skip copying any that actually exist. `compliance_program` itself, at the structurally identical `/v4/compliance/program` -> `/v4/compliance/program/{id}`, read cleanly (28 records) -- so this is specific to the two child resources, consistent with them actually being enumerated only in a parent-scoped shape (e.g. `GET /v4/compliance/program/{id}/family`) that the flat-list derivation doesn't reach, not a demo1 outage. Per this pass's constraints (no `import_.py`/`export.py`/`client.py` changes, no new engine mechanism), no code or metadata change is made here -- `compliance_family`/`compliance_level` are left in the active `natural_keys.yaml`/`references.yaml` exactly as inherited from the original sweep, since reclassifying them is a judgment call beyond this corrective pass's scope. This is recorded as an explicit, visible **follow-up**: before relying on `compliance_family`/`compliance_level` copy correctness in a real `--apply` run, someone must either (a) confirm/implement a parent-scoped list read (`GET /v4/compliance/program/{id}/family` and `.../level`, fanning out over every read `compliance_program`) analogous to `scope_criteria`'s `compound_key_parent_read` archetype, or (b) reclassify them to `hook`/staged until that lands.

All other 14 active resources -- including all of the original 7 -- read cleanly with real, non-zero data (except `category`, which has exactly 1 record on demo1; that is genuine data, not an error) and required no code changes to confirm.


## Equivalence regression check

Ran `python scripts/equivalence_check.py --source-env .env.source --target-env .env.target` (source demo1, target **qa4**, plan only -- `--apply` never passed, no writes to either install). This is the pre-existing regression harness for the original 7 entities (billing_sources, ous, funding_sources, projects, budgets, accounts, scopes); it is unaffected by this pass's changes to `permission_scheme`/`project_cloud_access_role_exemption`/`project_enforcement` or the generic-resource split, since none of those are in the harness's oracle/engine comparison.

**Verdict: EQUIVALENT** -- every entity/action count matched between the independent "oracle" walk and the metadata-driven engine after normalization (plural/singular key aliasing; OU-root `+1`), for all 7 entities and all 6 action buckets (create/recreate/adopt/ok/skipped/failed). No regressions from the metadata safety split introduced in this corrective pass.
