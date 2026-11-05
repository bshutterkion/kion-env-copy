# Terraform State Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an `export-tfstate` command to kion-env-copy that reads every resource the new Terraform provider defines from a live Kion install and writes two artifacts: a state-shaped JSON snapshot, and an importable Terraform v4 `terraform.tfstate`. This is a step toward the repo's larger goal: **parity with the Terraform provider's full resource set, so teams without Terraform access can move an environment.**

**Architecture:** A new `kion/tfstate/` package, driven by the provider's own `terraform providers schema -json` output (vendored as `schema_snapshots/new.json`). Reads reuse the engine's existing generic list reader (`kion/engine/read.py`) for the 50 resources that have a flat list endpoint, plus three new generic readers for parent-scoped, association, and special archetypes. Records are serialized to the provider's state attribute shape (exact names, JSON types, per-resource `schema_version`) carrying **literal source-install IDs**, with a `__ref__<field>` sidecar holding the natural key so records stay portable across installs. The state envelope is a thin assembler over that snapshot.

**Tech Stack:** Python 3.11+, `requests`, `PyYAML`, `pytest`. No new dependencies. Terraform 1.5+ only for the optional end-to-end validation task.

**Spec:** `docs/superpowers/specs/2026-07-14-tf-resource-parity-engine-design.md` (parent design — this plan adds a third backend to the engine's read/remap layer). The scope decisions specific to this plan are recorded in **Design Decisions** below.

---

## Design Decisions

Three decisions were made before planning; every task assumes them.

1. **Scope is all 68 provider resources**, not the 14 the engine currently reconciles. This is achievable because state emission does **not** require a natural key — the engine's 14/24/18 classification exists to support *reconciliation on a target* (adoption, dedup, drift). Emitting state from a source needs only "list the records and read their id." Re-classifying all 68 by *readability* instead gives:

   | bucket | n | mechanism |
   |---|---|---|
   | A. generic flat list | 51 | existing `engine/read.list_records` — no new code |
   | B1. parent-scoped list | 6 | one new reader (Task 8) |
   | B2. association read | 4 | one new reader (Task 9) |
   | B3. special archetypes | 4 | `singleton`, `raw_http` ×2, `cv_override` (Task 10) |
   | C. unreadable (`no_read`) | 0 | — |
   | D. no `generator_config` entry | 3 | `READ_OVERRIDES` entries (Task 7) |

   **All 68 are reachable.** Bucket C is empty as of provider `main` @ 69c566c.

   > **Updated 2026-08-25.** This table previously read "65 of 68 are reachable",
   > with `aws_resource_tag`, `ou_cloud_access_role_exemption` and
   > `project_cloud_access_role_exemption` in bucket C as unreadable. That is no
   > longer true. `kind: no_read` in the provider's codegen means the public spec
   > has no **by-id** GET — not that the record cannot be read. All three now
   > declare a `parent_read` and have a real read:
   >
   > - `aws_resource_tag` → public flat `/v3/aws-resource-tag` (**bucket A**)
   > - `ou_cloud_access_role_exemption` → private `/v1/ou/{id}/cloud-access-role-exemption` (**bucket B1**)
   > - `project_cloud_access_role_exemption` → private `/v1/project/{id}/cloud-access-role-exemption` (**bucket B1**)
   >
   > Two properties of the exemption collections must be carried over by any
   > reader here, both measured live:
   >
   > 1. **They are inherited, not owned.** `/v1/ou/{id}/…` returns every exemption
   >    visible to that OU's subtree, so one record is returned under many OUs
   >    (329 rows for 22 distinct records) and the id in the path is **not** the
   >    owner. The record's own `OUID` is. (`project_id` plays the same role on the
   >    project collection, and arrives as a SQL null wrapper rather than a bare
   >    number.)
   > 2. **They mix kinds.** The same collection returns cloud **rule** exemptions
   >    alongside cloud **access role** exemptions. Only 6 of 22 records were this
   >    resource on the reference install, and 0 of 12 on the project side. The
   >    discriminator is `ou_cloud_access_role_id` being `Valid`.
   >
   > See the provider's `codegen/private_endpoints.yaml` (`parent_read`) and
   > `docs/import-tooling-validation.md` § "`no_read` did not mean unreadable".

2. **Both artifacts in one pass.** `export-tfstate` writes the state-shaped snapshot (`--out`) and, unless `--no-state`, the assembled `terraform.tfstate` (`--state-out`).

3. **Literal IDs plus natural-key sidecar.** `attributes` carries real source-install IDs (`parent_ou_id: 5`, `id: "5"`) so the output is genuinely state-shaped. Alongside, `__ref__<field>` carries the natural key (`["ou", "Central Services"]`) for any field in `references.yaml`. This mirrors the `__srcid__<field>` convention already in `kion/engine/refmap.py:to_natural`.

**Known limitation, stated up front:** a resource whose live list endpoint 405s (see Task 7's probe) is downgraded at runtime, and a collection that is empty on the source install yields nothing to emit. The coverage report is the contract — the snapshot is never silently partial. (Bucket C, the original form of this limitation, no longer has members.)

## Global Constraints

- Python 3.11+. Only existing dependencies: `requests`, `PyYAML`, `pytest`. No `hcl` library, no Terraform SDK.
- **Never hand-edit anything under `kion/meta/vendor/`.** Vendored files are refreshed only by `scripts/sync-provider-meta.sh`.
- Provider address is exactly `registry.terraform.io/kionsoftware/kion`. State format version is exactly `4`.
- Pure helpers stay pure and get unit tests with no network (repo convention — see `tests/test_remap.py`).
- Never commit `.env*` (except `.env.example`), `snapshot*.json`, `id-map*.json`, `*.tfstate`, or `tfstate-*.json`.
- Every test file starts with the repo's path shim:
  ```python
  import os, sys
  sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
  ```
- Run the suite with `python -m pytest tests/ -q` from the repo root, venv active.
- Commit after every task. Conventional-commit prefixes (`feat:`, `fix:`, `test:`, `chore:`).

---

## File Structure

**Created:**

| file | responsibility |
|---|---|
| `kion/tfstate/__init__.py` | package marker |
| `kion/tfstate/schema.py` | load `new.json` → `ResourceSchema` (attrs, types, version, sensitive) |
| `kion/tfstate/coerce.py` | one API JSON value → one Terraform state JSON value, per TF type |
| `kion/tfstate/attrs.py` | one API record → complete state `attributes` dict |
| `kion/tfstate/labels.py` | stable, collision-free HCL identifiers |
| `kion/tfstate/refs.py` | `__ref__` sidecars + `dependencies` edges |
| `kion/tfstate/readers.py` | per-archetype readers (parent-scoped, association, special) |
| `kion/tfstate/collect.py` | walk all 68 kinds → `KindResult` list |
| `kion/tfstate/emit.py` | assemble the state-shaped snapshot document |
| `kion/tfstate/statefile.py` | assemble the Terraform v4 state envelope |
| `kion/tfstate/report.py` | human-readable coverage report |
| `kion/meta/vendor/schema_snapshots/new.json` | vendored provider schema (Task 1) |

**Modified:**

| file | change |
|---|---|
| `scripts/sync-provider-meta.sh` | also vendor `codegen/schema_snapshots/new.json` |
| `kion/meta/load.py` | 2 new `READ_OVERRIDES` entries (Task 7) |
| `kion_copy.py` | new `export-tfstate` subcommand (Task 11) |
| `.gitignore` | `*.tfstate`, `tfstate-*.json` |
| `CLAUDE.md` | document the new command + state-shape findings (Task 13) |

---

### Task 1: Vendor the provider schema snapshot

The vendored metadata is **stale**: `kion/meta/vendor/VERSION` is `0874805` with 61 resources; upstream is `69c566c` with 68. Every downstream count in this plan assumes the refresh happened first — and the refresh is what picks up the three resources that moved out of bucket C, since their reads are declared in `codegen/private_endpoints.yaml`.

**Files:**
- Modify: `scripts/sync-provider-meta.sh`
- Create: `kion/meta/vendor/schema_snapshots/new.json` (copied, not authored)
- Test: `tests/test_tfstate_vendor.py`

**Interfaces:**
- Produces: the vendored file at `kion/meta/vendor/schema_snapshots/new.json`, consumed by every later task via `kion/tfstate/schema.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_vendor.py
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "kion", "meta", "vendor")
SNAP = os.path.join(VENDOR, "schema_snapshots", "new.json")
PROVIDER = "registry.terraform.io/kionsoftware/kion"


def test_schema_snapshot_is_vendored():
    assert os.path.exists(SNAP), "run scripts/sync-provider-meta.sh"


def test_schema_snapshot_is_the_new_provider():
    with open(SNAP) as f:
        doc = json.load(f)
    assert doc["format_version"].startswith("1.")
    schemas = doc["provider_schemas"][PROVIDER]
    assert len(schemas["resource_schemas"]) >= 68
    assert "kion_ou" in schemas["resource_schemas"]


def test_generator_config_matches_snapshot_resource_count():
    """The vendored generator_config and schema snapshot must come from the
    same provider commit, or resource classification silently drifts."""
    import yaml
    with open(os.path.join(VENDOR, "generator_config.yaml")) as f:
        gen = yaml.safe_load(f)
    with open(SNAP) as f:
        schemas = json.load(f)["provider_schemas"][PROVIDER]["resource_schemas"]
    only_in_schema = {t[len("kion_"):] for t in schemas} - set(gen["resources"])
    # account / aws_cloudformation_template / aws_iam_policy are known codegen
    # gaps handled by READ_OVERRIDES in Task 7; nothing else may be missing.
    assert only_in_schema <= {"account", "aws_cloudformation_template", "aws_iam_policy"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_vendor.py -v`
Expected: FAIL — `test_schema_snapshot_is_vendored` asserts False (file absent).

- [ ] **Step 3: Extend the sync script**

Edit `scripts/sync-provider-meta.sh`, after the existing `for f in ...` loop:

```bash
# Vendor the authoritative provider schema snapshot. This is
# `terraform providers schema -json` output for the Plugin Framework provider
# and is the ground truth for state attribute names, types and schema_version.
mkdir -p "$DEST/schema_snapshots"
cp "$PROVIDER_DIR/codegen/schema_snapshots/new.json" "$DEST/schema_snapshots/new.json"
```

- [ ] **Step 4: Run the sync script**

Run: `bash scripts/sync-provider-meta.sh`
Expected: prints `Vendored provider metadata @ 7e06df1` (or newer).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_vendor.py -v`
Expected: 3 passed.

- [ ] **Step 6: Run the full suite — the metadata refresh may break existing tests**

Run: `python -m pytest tests/ -q`
Expected: all pass. `tests/test_meta_vendor.py` and `tests/test_onboarded_metadata.py` assert against vendored contents; if either fails on the 61→68 resource change, update its expected counts (the new resources are `billing_source_aws`, `billing_source_gcp`, `billing_source_govcloud`, `billing_source_oci`, `funding_source_note`, `idms_open_id_access_rule`, `idms_open_id_group_association`) and re-run.

- [ ] **Step 7: Commit**

```bash
git add scripts/sync-provider-meta.sh kion/meta/vendor tests/test_tfstate_vendor.py
git commit -m "chore: vendor provider schema snapshot, refresh codegen metadata to 7e06df1"
```

---

### Task 2: Provider schema loader

**Files:**
- Create: `kion/tfstate/__init__.py` (empty)
- Create: `kion/tfstate/schema.py`
- Test: `tests/test_tfstate_schema.py`

**Interfaces:**
- Produces:
  - `PROVIDER_ADDR: str` = `"registry.terraform.io/kionsoftware/kion"`
  - `@dataclass(frozen=True) Attr(name: str, type: object, required: bool, optional: bool, computed: bool, sensitive: bool)` — `type` is the raw TF type from the snapshot: `"string"`, `"number"`, `"bool"`, `["list","string"]`, `["list","number"]`, `["set","number"]`, `["map","string"]`, or a `NestedType`.
  - `@dataclass(frozen=True) NestedType(nesting_mode: str, attrs: dict[str, Attr])` — `nesting_mode` in `{"single","list","set"}`.
  - `@dataclass(frozen=True) ResourceSchema(tf_type: str, version: int, attrs: dict[str, Attr])`
  - `load_provider_schema(path: str | None = None) -> dict[str, ResourceSchema]` keyed by tf_type (`"kion_ou"`).
  - `ResourceSchema.sensitive_paths() -> list[list[dict]]` — Terraform state `sensitive_attributes` format.

Only 7 distinct scalar/collection types exist across all 68 resources, plus 12 `nested_type` attributes and 2 `block_types` (`kion_account`, `kion_aws_account`, both `nesting_mode: "single"`). Block types are normalized into `attrs` so downstream code has one uniform shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_schema.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.schema import load_provider_schema, PROVIDER_ADDR, NestedType


def test_provider_addr():
    assert PROVIDER_ADDR == "registry.terraform.io/kionsoftware/kion"


def test_loads_every_resource():
    s = load_provider_schema()
    assert len(s) >= 68
    assert "kion_ou" in s and "kion_project" in s


def test_ou_attributes_and_version():
    ou = load_provider_schema()["kion_ou"]
    assert ou.version == 1          # kion_ou is a v1 schema
    assert ou.attrs["name"].type == "string"
    assert ou.attrs["name"].required is True
    assert ou.attrs["parent_ou_id"].type == "number"
    assert ou.attrs["owner_user_ids"].type == ["list", "number"]
    assert ou.attrs["labels"].type == ["map", "string"]
    assert ou.attrs["id"].computed is True


def test_schema_versions_vary():
    s = load_provider_schema()
    assert s["kion_app_role"].version == 0
    assert s["kion_funding_source"].version == 1


def test_nested_type_is_parsed():
    bs = load_provider_schema()["kion_billing_source"]
    nt = bs.attrs["aws_connection"].type
    assert isinstance(nt, NestedType)
    assert nt.nesting_mode == "single"
    assert nt.attrs["storage_bucket"].type == "string"


def test_block_types_are_normalized_into_attrs():
    """kion_account declares aws_organizational_unit as a block_type, not an
    attribute. Downstream code must not care about the distinction."""
    acct = load_provider_schema()["kion_account"]
    nt = acct.attrs["aws_organizational_unit"].type
    assert isinstance(nt, NestedType)
    assert nt.nesting_mode == "single"
    assert "org_unit_id" in nt.attrs


def test_sensitive_paths():
    cfg = load_provider_schema()["kion_app_config"]
    assert cfg.sensitive_paths() == [[{"type": "get_attr", "value": "smtp_password"}]]
    assert load_provider_schema()["kion_ou"].sensitive_paths() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'kion.tfstate'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/__init__.py
```

```python
# kion/tfstate/schema.py
"""Load the provider's own `terraform providers schema -json` output.

This is the authority for what a Terraform state file must contain for each
kion_* resource: the exact attribute names, their JSON types, and the
per-resource ``schema_version``. Vendored by scripts/sync-provider-meta.sh from
the provider repo's codegen/schema_snapshots/new.json -- never hand-edited.

Terraform's schema JSON distinguishes ``attributes`` from ``block_types``, but
state serializes both the same way. We normalize block_types into ``attrs`` so
no downstream module has to care.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

PROVIDER_ADDR = "registry.terraform.io/kionsoftware/kion"

_SNAPSHOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "meta", "vendor", "schema_snapshots", "new.json",
)


@dataclass(frozen=True)
class NestedType:
    """A nested_type attribute or a block_type, unified."""
    nesting_mode: str                      # "single" | "list" | "set"
    attrs: dict[str, "Attr"] = field(default_factory=dict)


@dataclass(frozen=True)
class Attr:
    name: str
    type: object                           # str | list | NestedType
    required: bool = False
    optional: bool = False
    computed: bool = False
    sensitive: bool = False


@dataclass(frozen=True)
class ResourceSchema:
    tf_type: str
    version: int
    attrs: dict[str, Attr] = field(default_factory=dict)

    def sensitive_paths(self) -> list[list[dict]]:
        """Attribute paths in Terraform state's ``sensitive_attributes`` form."""
        return [[{"type": "get_attr", "value": name}]
                for name in sorted(self.attrs) if self.attrs[name].sensitive]


def _attr(name: str, raw: dict) -> Attr:
    if "nested_type" in raw:
        nt = raw["nested_type"]
        typ: object = NestedType(
            nesting_mode=nt.get("nesting_mode", "single"),
            attrs={n: _attr(n, a) for n, a in (nt.get("attributes") or {}).items()},
        )
    else:
        typ = raw.get("type")
    return Attr(
        name=name,
        type=typ,
        required=bool(raw.get("required")),
        optional=bool(raw.get("optional")),
        computed=bool(raw.get("computed")),
        sensitive=bool(raw.get("sensitive")),
    )


def _block_attr(name: str, raw: dict) -> Attr:
    """A block_type entry -> the same Attr shape as a nested_type attribute."""
    blk = raw.get("block") or {}
    return Attr(
        name=name,
        type=NestedType(
            nesting_mode=raw.get("nesting_mode", "single"),
            attrs={n: _attr(n, a) for n, a in (blk.get("attributes") or {}).items()},
        ),
        optional=True,
    )


def load_provider_schema(path: str | None = None) -> dict[str, ResourceSchema]:
    """tf_type -> ResourceSchema for every managed resource the provider serves."""
    with open(path or _SNAPSHOT) as f:
        doc = json.load(f)
    resources = doc["provider_schemas"][PROVIDER_ADDR]["resource_schemas"]

    out: dict[str, ResourceSchema] = {}
    for tf_type, entry in resources.items():
        block = entry.get("block") or {}
        attrs = {n: _attr(n, a) for n, a in (block.get("attributes") or {}).items()}
        for n, b in (block.get("block_types") or {}).items():
            attrs[n] = _block_attr(n, b)
        out[tf_type] = ResourceSchema(
            tf_type=tf_type, version=int(entry.get("version", 0)), attrs=attrs
        )
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_schema.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/__init__.py kion/tfstate/schema.py tests/test_tfstate_schema.py
git commit -m "feat: load provider schema snapshot into ResourceSchema"
```

---

### Task 3: Value coercion

Kion's API returns JSON that is close to, but not the same as, what Terraform state stores. The single most important difference: **`id` is a string in state** but a number in every Kion API response.

**Files:**
- Create: `kion/tfstate/coerce.py`
- Test: `tests/test_tfstate_coerce.py`

**Interfaces:**
- Consumes: `Attr`, `NestedType` from Task 2.
- Produces: `coerce(value, tf_type) -> object` — a JSON-serializable value valid for that TF type. `None` in, `None` out, always.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_coerce.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.coerce import coerce
from kion.tfstate.schema import Attr, NestedType


def test_none_passes_through_for_every_type():
    for t in ["string", "number", "bool", ["list", "number"], ["map", "string"]]:
        assert coerce(None, t) is None


def test_number_to_string():
    """Terraform's `id` is always a string; Kion returns an int."""
    assert coerce(5, "string") == "5"
    assert coerce("5", "string") == "5"


def test_string_to_number():
    assert coerce("5", "number") == 5
    assert coerce("5.5", "number") == 5.5
    assert coerce(5, "number") == 5


def test_bad_number_becomes_none():
    assert coerce("not-a-number", "number") is None


def test_bool():
    assert coerce(True, "bool") is True
    assert coerce(1, "bool") is True
    assert coerce(0, "bool") is False


def test_list_of_number():
    assert coerce(["1", 2], ["list", "number"]) == [1, 2]


def test_set_of_number_is_deduped_and_sorted():
    """Terraform stores sets as lists; a stable order keeps diffs quiet."""
    assert coerce([3, 1, 3], ["set", "number"]) == [1, 3]


def test_map_of_string():
    assert coerce({"env": "prod", "n": 1}, ["map", "string"]) == {"env": "prod", "n": "1"}


def test_scalar_wrapped_into_list():
    """Some Kion endpoints return a bare value where the schema wants a list."""
    assert coerce(7, ["list", "number"]) == [7]


def test_nested_single():
    nt = NestedType("single", {"bucket": Attr("bucket", "string"),
                               "n": Attr("n", "number")})
    assert coerce({"bucket": "b", "n": "3"}, nt) == {"bucket": "b", "n": 3}


def test_nested_single_fills_missing_keys_with_none():
    nt = NestedType("single", {"a": Attr("a", "string"), "b": Attr("b", "string")})
    assert coerce({"a": "x"}, nt) == {"a": "x", "b": None}


def test_nested_list():
    nt = NestedType("list", {"k": Attr("k", "string")})
    assert coerce([{"k": "a"}, {"k": "b"}], nt) == [{"k": "a"}, {"k": "b"}]


def test_nested_list_from_single_object():
    nt = NestedType("list", {"k": Attr("k", "string")})
    assert coerce({"k": "a"}, nt) == [{"k": "a"}]


def test_unknown_type_passes_through():
    assert coerce({"anything": 1}, None) == {"anything": 1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_coerce.py -v`
Expected: FAIL — `No module named 'kion.tfstate.coerce'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/coerce.py
"""Coerce a Kion API JSON value into the shape Terraform state stores.

The provider's schema uses only seven scalar/collection types across all 68
resources -- string, number, bool, list(string), list(number), set(number),
map(string) -- plus nested objects. Kion's API is looser than the schema in two
ways this module exists to absorb:

  * `id` (and every *_id echoed as a string elsewhere) is a NUMBER in API
    responses but a STRING in Terraform state.
  * some endpoints return a bare scalar or object where the schema declares a
    list.

Coercion never raises: a value that cannot be represented becomes None, which
Terraform reads as null. A read failure must never abort an export.
"""
from __future__ import annotations

from kion.tfstate.schema import NestedType


def _number(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        text = str(value).strip()
        return int(text) if text.lstrip("-").isdigit() else float(text)
    except (TypeError, ValueError):
        return None


def _nested(value, nt: NestedType):
    if nt.nesting_mode == "single":
        if not isinstance(value, dict):
            return None
        return {n: coerce(value.get(n), a.type) for n, a in nt.attrs.items()}
    items = value if isinstance(value, list) else [value]
    out = [{n: coerce(i.get(n), a.type) for n, a in nt.attrs.items()}
           for i in items if isinstance(i, dict)]
    return out


def coerce(value, tf_type):
    """One API value -> one Terraform state value. None in, None out."""
    if value is None:
        return None
    if isinstance(tf_type, NestedType):
        return _nested(value, tf_type)
    if tf_type == "string":
        return value if isinstance(value, str) else str(value)
    if tf_type == "number":
        return _number(value)
    if tf_type == "bool":
        return bool(value)
    if isinstance(tf_type, list) and len(tf_type) == 2:
        kind, elem = tf_type
        if kind == "map":
            if not isinstance(value, dict):
                return None
            return {str(k): coerce(v, elem) for k, v in value.items()}
        if kind in ("list", "set"):
            items = value if isinstance(value, list) else [value]
            out = [coerce(v, elem) for v in items]
            if kind == "set":
                # Terraform stores sets as lists; dedupe + sort for a stable diff.
                seen, uniq = set(), []
                for v in out:
                    k = repr(v)
                    if k not in seen:
                        seen.add(k)
                        uniq.append(v)
                try:
                    return sorted(uniq, key=lambda x: (x is None, x))
                except TypeError:
                    return uniq
            return out
    return value
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_coerce.py -v`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/coerce.py tests/test_tfstate_coerce.py
git commit -m "feat: coerce Kion API values to Terraform state JSON types"
```

---

### Task 4: Record → state attributes

**Files:**
- Create: `kion/tfstate/attrs.py`
- Test: `tests/test_tfstate_attrs.py`

**Interfaces:**
- Consumes: `ResourceSchema` (Task 2), `coerce` (Task 3).
- Produces:
  - `@dataclass StateRecord(attributes: dict, missing: list[str], extra: list[str])`
  - `to_state_attributes(record: dict, rs: ResourceSchema, renames: dict[str, str] | None = None) -> StateRecord`

Rules, all asserted by tests below:
- **Every** attribute in the schema appears in `attributes`. State is a complete object; a missing key is not the same as `null`.
- `id` is taken from the record's `id` and coerced to string.
- Fields in the record that no schema attribute claims are reported in `extra` and dropped.
- Schema attributes with no record field are reported in `missing` and set to `None`.
- `renames` maps API field name → schema attribute name (from the provider's `codegen/renames.yaml`, 8 entries).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_attrs.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.attrs import to_state_attributes
from kion.tfstate.schema import Attr, ResourceSchema, load_provider_schema

SIMPLE = ResourceSchema("kion_thing", 0, {
    "id": Attr("id", "string", computed=True),
    "name": Attr("name", "string", required=True),
    "count": Attr("count", "number", optional=True),
})


def test_every_schema_attribute_is_present():
    r = to_state_attributes({"id": 1, "name": "a", "count": 2}, SIMPLE)
    assert set(r.attributes) == {"id", "name", "count"}


def test_id_is_stringified():
    r = to_state_attributes({"id": 5, "name": "a"}, SIMPLE)
    assert r.attributes["id"] == "5"


def test_missing_attributes_are_null_and_reported():
    r = to_state_attributes({"id": 1, "name": "a"}, SIMPLE)
    assert r.attributes["count"] is None
    assert r.missing == ["count"]


def test_extra_record_fields_are_dropped_and_reported():
    r = to_state_attributes({"id": 1, "name": "a", "internal_flag": True}, SIMPLE)
    assert "internal_flag" not in r.attributes
    assert r.extra == ["internal_flag"]


def test_renames_are_applied():
    r = to_state_attributes({"id": 1, "title": "a"}, SIMPLE, renames={"title": "name"})
    assert r.attributes["name"] == "a"
    assert r.extra == []


def test_real_ou_record():
    ou = load_provider_schema()["kion_ou"]
    record = {
        "id": 5, "name": "Central Services", "description": "d",
        "parent_ou_id": 1, "permission_scheme_id": 3,
        "owner_user_ids": [12, 13], "owner_user_group_ids": [],
    }
    r = to_state_attributes(record, ou)
    assert r.attributes["id"] == "5"
    assert r.attributes["parent_ou_id"] == 1
    assert r.attributes["owner_user_ids"] == [12, 13]
    # computed attributes the API does not return are present as null
    assert r.attributes["created_at"] is None
    assert r.attributes["last_updated"] is None
    assert "created_at" in r.missing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_attrs.py -v`
Expected: FAIL — `No module named 'kion.tfstate.attrs'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/attrs.py
"""One Kion API record -> one complete Terraform state ``attributes`` object.

Completeness is the invariant: state stores every attribute the schema declares,
with null for the ones that have no value. An absent key and a null are NOT the
same thing to Terraform, so we always emit the full attribute set.

What the API returns and what the schema declares do not always line up, in
both directions. Both directions are reported rather than silently absorbed:
``missing`` (schema wants it, API did not send it -> null) and ``extra`` (API
sent it, schema has no home for it -> dropped). Task 13's coverage report
surfaces both so a schema drift shows up as a diff, not as a mystery.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kion.tfstate.coerce import coerce
from kion.tfstate.schema import ResourceSchema


@dataclass
class StateRecord:
    attributes: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)


def to_state_attributes(record: dict, rs: ResourceSchema,
                        renames: dict[str, str] | None = None) -> StateRecord:
    """Serialize ``record`` into ``rs``'s state attribute shape."""
    renames = renames or {}
    src = {renames.get(k, k): v for k, v in (record or {}).items()}

    attributes, missing = {}, []
    for name, attr in rs.attrs.items():
        if name in src:
            attributes[name] = coerce(src[name], attr.type)
        else:
            attributes[name] = None
            missing.append(name)

    extra = sorted(k for k in src if k not in rs.attrs)
    return StateRecord(attributes=attributes, missing=sorted(missing), extra=extra)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_attrs.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/attrs.py tests/test_tfstate_attrs.py
git commit -m "feat: serialize API records into complete state attribute objects"
```

---

### Task 5: Label allocator

Every resource instance needs a Terraform address (`kion_ou.central_services`). Labels must be valid HCL identifiers, stable across runs, and unique per type — `normalize_string` alone is not enough, because two OUs can each own a cloud access role named "Engineers".

**Files:**
- Create: `kion/tfstate/labels.py`
- Test: `tests/test_tfstate_labels.py`

**Interfaces:**
- Produces:
  - `normalize(text: str) -> str`
  - `class LabelAllocator` with `allocate(tf_type: str, record: dict, name_field: str = "name") -> str` and `.index: dict[tuple[str, str], str]` mapping `(tf_type, source_id)` → label.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_labels.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.labels import LabelAllocator, normalize


def test_normalize_basics():
    assert normalize("Central Services") == "central_services"
    assert normalize("AWS (GovCloud) / prod") == "aws_govcloud_prod"
    assert normalize("  spaced  out  ") == "spaced_out"


def test_normalize_leading_digit_is_prefixed():
    """HCL identifiers cannot start with a digit."""
    assert normalize("2024 Budget") == "_2024_budget"


def test_normalize_empty_becomes_placeholder():
    assert normalize("") == "unnamed"
    assert normalize("!!!") == "unnamed"


def test_allocate_uses_the_name():
    a = LabelAllocator()
    assert a.allocate("kion_ou", {"id": 5, "name": "Central Services"}) == "central_services"


def test_collision_gets_suffixed_deterministically():
    a = LabelAllocator()
    assert a.allocate("kion_ou", {"id": 1, "name": "Engineers"}) == "engineers"
    assert a.allocate("kion_ou", {"id": 2, "name": "Engineers"}) == "engineers_2"
    assert a.allocate("kion_ou", {"id": 3, "name": "Engineers"}) == "engineers_3"


def test_collisions_are_scoped_per_type():
    a = LabelAllocator()
    assert a.allocate("kion_ou", {"id": 1, "name": "Shared"}) == "shared"
    assert a.allocate("kion_project", {"id": 1, "name": "Shared"}) == "shared"


def test_nameless_record_falls_back_to_id():
    a = LabelAllocator()
    assert a.allocate("kion_label", {"id": 9}) == "kion_label_9"


def test_index_maps_source_id_to_label():
    a = LabelAllocator()
    a.allocate("kion_ou", {"id": 5, "name": "Central Services"})
    assert a.index[("kion_ou", "5")] == "central_services"


def test_reallocating_the_same_record_is_idempotent():
    a = LabelAllocator()
    first = a.allocate("kion_ou", {"id": 5, "name": "Central Services"})
    assert a.allocate("kion_ou", {"id": 5, "name": "Central Services"}) == first
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_labels.py -v`
Expected: FAIL — `No module named 'kion.tfstate.labels'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/labels.py
"""Stable, collision-free Terraform resource labels.

The old importer-script's normalize_string was not collision-safe: two OUs can
each own a cloud access role named "Engineers", and both would have been written
to the same address. Uniqueness here is scoped per tf_type and resolved with a
deterministic numeric suffix, so re-running the export against an unchanged
install produces byte-identical addresses.
"""
from __future__ import annotations

import re

_BAD = re.compile(r"[^a-z0-9_]+")


def normalize(text: str) -> str:
    """A Kion name -> a valid, lowercase HCL identifier."""
    slug = _BAD.sub("_", (text or "").strip().lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    if not slug:
        return "unnamed"
    if slug[0].isdigit():
        return "_" + slug
    return slug


class LabelAllocator:
    """Hands out unique labels and remembers (tf_type, source_id) -> label."""

    def __init__(self):
        self.index: dict[tuple[str, str], str] = {}
        self._taken: dict[str, set[str]] = {}

    def allocate(self, tf_type: str, record: dict, name_field: str = "name") -> str:
        source_id = str(record.get("id", ""))
        key = (tf_type, source_id)
        if key in self.index:
            return self.index[key]

        raw = record.get(name_field)
        base = normalize(raw) if raw else f"{tf_type}_{source_id or 'unnamed'}"

        taken = self._taken.setdefault(tf_type, set())
        label, n = base, 1
        while label in taken:
            n += 1
            label = f"{base}_{n}"
        taken.add(label)
        self.index[key] = label
        return label
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_labels.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/labels.py tests/test_tfstate_labels.py
git commit -m "feat: collision-free Terraform label allocator"
```

---

### Task 6: Natural-key sidecars and dependency edges

This is the third direction of `kion/engine/refmap.py`. That module has `to_natural` (source ids → natural keys) and `to_target_ids` (natural keys → target ids); this adds source ids → **Terraform addresses**, plus the portable `__ref__` sidecar.

**Files:**
- Create: `kion/tfstate/refs.py`
- Test: `tests/test_tfstate_refs.py`

**Interfaces:**
- Consumes: `Reference` from `kion/meta/load.py`, `LabelAllocator.index` from Task 5.
- Produces:
  - `sidecar(record: dict, refs: list[Reference], id_to_key: dict[tuple[str, int], tuple]) -> dict` → `{"__ref__parent_ou_id": ["ou", "Central Services"]}`
  - `dependencies(record: dict, refs: list[Reference], tf_type_of: dict[str, str], label_index: dict[tuple[str, str], str]) -> list[str]` → `["kion_ou.central_services"]`, sorted and deduped.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_refs.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import Reference
from kion.tfstate.refs import dependencies, sidecar

REFS = [
    Reference(field="parent_ou_id", target="ou", key="name"),
    Reference(field="owner_user_ids", target="user", key="name", many=True, optional=True),
]
ID_TO_KEY = {("ou", 1): ("Central Services",), ("user", 12): ("alice",)}
TF_TYPE_OF = {"ou": "kion_ou", "user": "kion_user"}
LABELS = {("kion_ou", "1"): "central_services", ("kion_user", "12"): "alice"}


def test_sidecar_records_the_natural_key():
    s = sidecar({"parent_ou_id": 1}, REFS, ID_TO_KEY)
    assert s == {"__ref__parent_ou_id": ["ou", "Central Services"]}


def test_sidecar_handles_many():
    s = sidecar({"owner_user_ids": [12]}, REFS, ID_TO_KEY)
    assert s == {"__ref__owner_user_ids": [["user", "alice"]]}


def test_sidecar_omits_unresolvable_and_zero_refs():
    assert sidecar({"parent_ou_id": 0}, REFS, ID_TO_KEY) == {}
    assert sidecar({"parent_ou_id": 999}, REFS, ID_TO_KEY) == {}


def test_sidecar_ignores_absent_fields():
    assert sidecar({}, REFS, ID_TO_KEY) == {}


def test_dependencies_are_terraform_addresses():
    d = dependencies({"parent_ou_id": 1, "owner_user_ids": [12]}, REFS, TF_TYPE_OF, LABELS)
    assert d == ["kion_ou.central_services", "kion_user.alice"]


def test_dependencies_are_deduped_and_sorted():
    refs = [Reference(field="a_id", target="ou", key="name"),
            Reference(field="b_id", target="ou", key="name")]
    d = dependencies({"a_id": 1, "b_id": 1}, refs, TF_TYPE_OF, LABELS)
    assert d == ["kion_ou.central_services"]


def test_dependencies_skip_unknown_targets():
    assert dependencies({"parent_ou_id": 999}, REFS, TF_TYPE_OF, LABELS) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_refs.py -v`
Expected: FAIL — `No module named 'kion.tfstate.refs'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/refs.py
"""The third direction of the engine's reference remap.

kion/engine/refmap.py already converts source ids -> natural keys (to_natural)
and natural keys -> target ids (to_target_ids). Terraform needs a third:
source ids -> Terraform addresses, for the ``dependencies`` array of a state
instance. And because state attributes carry LITERAL source ids (they have to,
or the state is not state), the portable natural key is preserved beside them in
a ``__ref__<field>`` sidecar -- the same trick refmap's ``__srcid__<field>``
plays in reverse.
"""
from __future__ import annotations


def sidecar(record: dict, refs, id_to_key: dict) -> dict:
    """``{"__ref__<field>": <natural key>}`` for every resolvable reference."""
    out = {}
    for r in refs:
        if r.field not in record:
            continue
        val = record[r.field]
        if r.many:
            keys = [[r.target, *id_to_key[(r.target, v)]]
                    for v in (val or []) if (r.target, v) in id_to_key]
            if keys:
                out[f"__ref__{r.field}"] = keys
        elif val not in (None, 0) and (r.target, val) in id_to_key:
            out[f"__ref__{r.field}"] = [r.target, *id_to_key[(r.target, val)]]
    return out


def dependencies(record: dict, refs, tf_type_of: dict, label_index: dict) -> list[str]:
    """Sorted, deduped Terraform addresses this record depends on."""
    found = set()
    for r in refs:
        if r.field not in record:
            continue
        tf_type = tf_type_of.get(r.target)
        if not tf_type:
            continue
        vals = record[r.field] if r.many else [record[r.field]]
        for v in vals or []:
            if v in (None, 0):
                continue
            label = label_index.get((tf_type, str(v)))
            if label:
                found.add(f"{tf_type}.{label}")
    return sorted(found)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_refs.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/refs.py tests/test_tfstate_refs.py
git commit -m "feat: natural-key sidecars and Terraform dependency edges"
```

---

### Task 7: Readability classification + generic reads + live probe

Classify all 68 resources by how they can be read, and read bucket A (50 resources) with the engine's existing reader. Critically, the classification is **metadata's opinion, verified live** — the repo already learned this the hard way: `compliance_family` and `compliance_level` are declared as ordinary entities but their flat list endpoints return 405, which a live smoke test caught and `docs/ONBOARDING_REPORT.md` records as a corrective pass.

**Files:**
- Create: `kion/tfstate/collect.py`
- Modify: `kion/meta/load.py` (2 new `READ_OVERRIDES` entries)
- Test: `tests/test_tfstate_collect.py`

**Interfaces:**
- Consumes: `load_resource_meta`, `ResourceMeta` (`kion/meta/load.py`); `list_path` (`kion/engine/paths.py`); `list_records` (`kion/engine/read.py`); `load_provider_schema` (Task 2).
- Produces:
  - `@dataclass KindResult(kind: str, tf_type: str, status: str, records: list[dict], reason: str, bucket: str)` — `status` in `{"ok", "empty", "unsupported", "error"}`.
  - `classify(schemas, meta, archetypes) -> dict[str, str]` — tf_type → bucket in `{"generic", "parent_list", "association", "special", "no_read", "unmapped"}`.
  - `read_generic(client, rm: ResourceMeta, tf_type: str) -> KindResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_collect.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from kion.client import KionAPIError
from kion.meta.load import ResourceMeta, load_resource_meta
from kion.tfstate.collect import classify, read_generic
from kion.tfstate.schema import load_provider_schema

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "kion", "meta", "vendor")


def _archetypes():
    with open(os.path.join(VENDOR, "crud_archetypes.yaml")) as f:
        return yaml.safe_load(f) or {}


class FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get(self, path, params=None):
        self.calls.append(path)
        if self.error:
            raise self.error
        return self.payload


def test_classification_covers_every_resource():
    schemas = load_provider_schema()
    buckets = classify(schemas, load_resource_meta(), _archetypes())
    assert set(buckets) == set(schemas)


def test_classification_bucket_counts():
    """These counts are the plan's scope contract. If the provider adds
    resources, this test fails loudly rather than the export going quiet."""
    import collections
    buckets = classify(load_provider_schema(), load_resource_meta(), _archetypes())
    n = collections.Counter(buckets.values())
    assert n["generic"] == 50
    assert n["parent_list"] == 4
    assert n["association"] == 4
    assert n["special"] == 4
    assert n["no_read"] == 3
    assert n["unmapped"] == 3


def test_known_bucket_members():
    b = classify(load_provider_schema(), load_resource_meta(), _archetypes())
    assert b["kion_ou"] == "generic"
    assert b["kion_ou_enforcement"] == "parent_list"
    assert b["kion_ou_permission_mapping"] == "association"
    assert b["kion_aws_resource_tag"] == "no_read"
    assert b["kion_app_config"] == "special"


def test_read_overrides_cover_the_unmapped():
    """account / aws_cloudformation_template / aws_iam_policy have no
    generator_config entry under those names; READ_OVERRIDES supplies one."""
    meta = load_resource_meta()
    for kind in ("account", "aws_cloudformation_template", "aws_iam_policy"):
        assert kind in meta, kind
        assert meta[kind].read_path


def test_read_generic_returns_records():
    rm = ResourceMeta(name="ou", read_path="/v3/ou/{id}")
    c = FakeClient(payload=[{"id": 1, "name": "a"}])
    r = read_generic(c, rm, "kion_ou")
    assert r.status == "ok"
    assert r.records == [{"id": 1, "name": "a"}]
    assert c.calls == ["/v3/ou"]


def test_read_generic_empty_is_not_an_error():
    r = read_generic(FakeClient(payload=[]), ResourceMeta(name="ou", read_path="/v3/ou/{id}"), "kion_ou")
    assert r.status == "empty"


def test_read_generic_downgrades_on_api_error():
    """The 405 lesson: metadata says listable, the live install disagrees."""
    c = FakeClient(error=KionAPIError("405 Method Not Allowed"))
    r = read_generic(c, ResourceMeta(name="compliance_family",
                                     read_path="/v4/compliance/family/{id}"),
                     "kion_compliance_family")
    assert r.status == "error"
    assert "405" in r.reason
    assert r.records == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_collect.py -v`
Expected: FAIL — `No module named 'kion.tfstate.collect'`.

- [ ] **Step 3: Add the READ_OVERRIDES entries**

In `kion/meta/load.py`, extend the existing `READ_OVERRIDES` dict (leave the `account` entry and its comment untouched, add below it):

```python
    # The provider serves kion_aws_cloudformation_template and kion_aws_iam_policy,
    # but codegen names those entities `cft` and `iam_policy`. State export is
    # keyed on the provider's tf_type, so both names need a read path.
    "aws_cloudformation_template": {
        "read_path": "/v3/cft", "read_method": "GET",
    },
    "aws_iam_policy": {
        "read_path": "/v3/iam-policy", "read_method": "GET",
    },
```

- [ ] **Step 4: Write the implementation**

```python
# kion/tfstate/collect.py
"""Walk every resource the provider serves and read what can be read.

Classification here is by READABILITY, which is a different question from the
engine's reconcile classification in kion/meta/natural_keys*.yaml. Reconciling
onto a target needs a natural key (for adoption and drift); reading a source for
state export needs only "list the records and take their id". That is why this
covers 65 of 68 resources where the engine reconciles 14.

Metadata is an opinion, not a fact. docs/ONBOARDING_REPORT.md records a
corrective pass where compliance_family / compliance_level looked like ordinary
listable entities and returned 405 live. read_generic therefore downgrades a
failed read to status="error" with the cause, and the run continues -- a partial
export that says so beats an aborted one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kion.client import KionAPIError
from kion.engine.paths import list_path
from kion.engine.read import list_records

_SPECIAL = {"singleton", "raw_http", "cv_override", "datasource_only"}


@dataclass
class KindResult:
    kind: str                       # codegen name, e.g. "ou"
    tf_type: str                    # provider type, e.g. "kion_ou"
    status: str = "unsupported"     # ok | empty | unsupported | error
    records: list = field(default_factory=list)
    reason: str = ""
    bucket: str = ""


def classify(schemas: dict, meta: dict, archetypes: dict) -> dict[str, str]:
    """tf_type -> readability bucket, for every resource the provider serves."""
    out = {}
    for tf_type in schemas:
        kind = tf_type[len("kion_"):]
        arch = (archetypes.get(kind) or {}).get("kind")
        if arch == "no_read":
            out[tf_type] = "no_read"
        elif arch in ("parent_list", "compound_key_parent_read"):
            out[tf_type] = "parent_list"
        elif arch == "association":
            out[tf_type] = "association"
        elif arch in _SPECIAL:
            out[tf_type] = "special"
        elif kind not in meta or not meta[kind].read_path:
            out[tf_type] = "unmapped"
        else:
            out[tf_type] = "generic"
    return out


def read_generic(client, rm, tf_type: str) -> KindResult:
    """Read a flat-list resource with the engine's shared list reader."""
    path = list_path(rm.read_path)
    result = KindResult(kind=rm.name, tf_type=tf_type, bucket="generic")
    failures = []
    try:
        records = list_records(client, path,
                               on_error=lambda p, e: failures.append(f"{p}: {e}"))
    except KionAPIError as e:                      # defensive; list_records swallows
        result.status, result.reason = "error", str(e)
        return result

    if failures:
        result.status, result.reason = "error", failures[0]
        return result

    result.records = [r for r in records if isinstance(r, dict)]
    result.status = "ok" if result.records else "empty"
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_collect.py -v`
Expected: 8 passed. If `test_classification_bucket_counts` fails, the vendored provider moved — read the new counts off the failure, confirm the new resources are genuinely new, and update the assertion in the same commit.

- [ ] **Step 6: Commit**

```bash
git add kion/tfstate/collect.py kion/meta/load.py tests/test_tfstate_collect.py
git commit -m "feat: classify all provider resources by readability, read the generic 50"
```

---

### Task 8: Parent-scoped list reader

Four resources (`ou_enforcement`, `project_enforcement`, `funding_source_enforcement`, `scope_criteria`) have no flat list — they're read per parent. This is also the mechanism `compliance_family` / `compliance_level` need if Task 7's live probe downgrades them.

**Files:**
- Modify: `kion/tfstate/collect.py` (add `read_parent_scoped`)
- Create: `kion/tfstate/readers.py` (parent spec table)
- Test: `tests/test_tfstate_readers.py`

**Interfaces:**
- Consumes: `KindResult` (Task 7), `list_records` (`kion/engine/read.py`).
- Produces:
  - `PARENT_SPECS: dict[str, ParentSpec]` keyed by tf_type.
  - `@dataclass ParentSpec(parent_kind: str, parent_list_path: str, child_path: str, parent_id_field: str)` — `child_path` contains `{parent_id}`.
  - `read_parent_scoped(client, spec: ParentSpec, tf_type: str, kind: str) -> KindResult`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_readers.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.client import KionAPIError
from kion.tfstate.collect import read_parent_scoped
from kion.tfstate.readers import PARENT_SPECS, ParentSpec


class RouteClient:
    """Serves canned payloads by exact path."""
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, path, params=None):
        self.calls.append(path)
        if path not in self.routes:
            raise KionAPIError(f"404 {path}")
        val = self.routes[path]
        if isinstance(val, Exception):
            raise val
        return val


SPEC = ParentSpec(parent_kind="ou", parent_list_path="/v3/ou",
                  child_path="/v3/ou/{parent_id}/enforcement",
                  parent_id_field="ou_id")


def test_reads_children_of_every_parent():
    c = RouteClient({
        "/v3/ou": [{"id": 1}, {"id": 2}],
        "/v3/ou/1/enforcement": [{"id": 10}],
        "/v3/ou/2/enforcement": [{"id": 20}, {"id": 21}],
    })
    r = read_parent_scoped(c, SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert r.status == "ok"
    assert [x["id"] for x in r.records] == [10, 20, 21]


def test_stamps_the_parent_id_onto_each_child():
    """The child endpoint often omits the parent id the schema requires."""
    c = RouteClient({"/v3/ou": [{"id": 7}], "/v3/ou/7/enforcement": [{"id": 10}]})
    r = read_parent_scoped(c, SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert r.records[0]["ou_id"] == 7


def test_does_not_overwrite_an_existing_parent_id():
    c = RouteClient({"/v3/ou": [{"id": 7}], "/v3/ou/7/enforcement": [{"id": 10, "ou_id": 99}]})
    r = read_parent_scoped(c, SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert r.records[0]["ou_id"] == 99


def test_one_failing_parent_does_not_abort_the_rest():
    c = RouteClient({
        "/v3/ou": [{"id": 1}, {"id": 2}],
        "/v3/ou/1/enforcement": KionAPIError("500 boom"),
        "/v3/ou/2/enforcement": [{"id": 20}],
    })
    r = read_parent_scoped(c, SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert [x["id"] for x in r.records] == [20]
    assert "500" in r.reason
    assert r.status == "ok"        # partial, and the reason says so


def test_no_parents_is_empty_not_error():
    c = RouteClient({"/v3/ou": []})
    r = read_parent_scoped(c, SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert r.status == "empty"


def test_parent_list_failure_is_an_error():
    r = read_parent_scoped(RouteClient({}), SPEC, "kion_ou_enforcement", "ou_enforcement")
    assert r.status == "error"


def test_specs_exist_for_every_parent_list_resource():
    for t in ("kion_ou_enforcement", "kion_project_enforcement",
              "kion_funding_source_enforcement", "kion_scope_criteria"):
        assert t in PARENT_SPECS, t
        assert "{parent_id}" in PARENT_SPECS[t].child_path
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_readers.py -v`
Expected: FAIL — `No module named 'kion.tfstate.readers'`.

- [ ] **Step 3: Write the parent spec table**

```python
# kion/tfstate/readers.py
"""Read specs for resources with no flat list endpoint.

The provider's crud_archetypes.yaml declares these as parent_list /
compound_key_parent_read: there is no GET /v3/<thing>, only a collection hanging
off each parent. One generic reader plus this table covers all of them, instead
of a bespoke reader per resource.

The paths here are NOT in the vendored codegen metadata (it records the by-id
read, which is exactly what these resources lack), so they are authored. Verify
each against a live install before trusting it -- see Task 14's probe.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParentSpec:
    parent_kind: str            # codegen name of the parent resource
    parent_list_path: str       # flat list of parents
    child_path: str             # child collection; contains "{parent_id}"
    parent_id_field: str        # field to stamp onto each child record


PARENT_SPECS: dict[str, ParentSpec] = {
    "kion_ou_enforcement": ParentSpec(
        "ou", "/v3/ou", "/v3/ou/{parent_id}/enforcement", "ou_id"),
    "kion_project_enforcement": ParentSpec(
        "project", "/v3/project", "/v3/project/{parent_id}/enforcement", "project_id"),
    "kion_funding_source_enforcement": ParentSpec(
        "funding_source", "/v3/funding-source",
        "/v3/funding-source/{parent_id}/enforcement", "funding_source_id"),
    "kion_scope_criteria": ParentSpec(
        "scope", "/beta/scope", "/beta/scope/{parent_id}/criteria", "scope_id"),
    # Fallbacks for the two resources docs/ONBOARDING_REPORT.md reclassified
    # after a live 405 on their flat list. Task 7's probe routes here when the
    # generic read fails.
    "kion_compliance_family": ParentSpec(
        "compliance_program", "/v4/compliance/program",
        "/v4/compliance/program/{parent_id}/family", "compliance_program_id"),
    "kion_compliance_level": ParentSpec(
        "compliance_program", "/v4/compliance/program",
        "/v4/compliance/program/{parent_id}/level", "compliance_program_id"),
}
```

- [ ] **Step 4: Add the reader to `collect.py`**

Append to `kion/tfstate/collect.py`:

```python
def read_parent_scoped(client, spec, tf_type: str, kind: str) -> KindResult:
    """Enumerate parents, then read each parent's child collection.

    A single parent's failure degrades that parent only: its cause is recorded
    in ``reason`` and the remaining parents are still read. Only a failure to
    list the PARENTS is fatal for the kind.
    """
    result = KindResult(kind=kind, tf_type=tf_type, bucket="parent_list")

    parent_failures = []
    parents = list_records(client, spec.parent_list_path,
                           on_error=lambda p, e: parent_failures.append(f"{p}: {e}"))
    if parent_failures:
        result.status, result.reason = "error", parent_failures[0]
        return result
    if not parents:
        result.status = "empty"
        return result

    child_failures = []
    for parent in parents:
        pid = parent.get("id") if isinstance(parent, dict) else None
        if pid is None:
            continue
        path = spec.child_path.format(parent_id=pid)
        for child in list_records(client, path,
                                  on_error=lambda p, e: child_failures.append(f"{p}: {e}")):
            if not isinstance(child, dict):
                continue
            child.setdefault(spec.parent_id_field, pid)
            result.records.append(child)

    if child_failures:
        result.reason = f"{len(child_failures)} parent(s) failed; first: {child_failures[0]}"
    result.status = "ok" if result.records else ("error" if child_failures else "empty")
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_readers.py -v`
Expected: 7 passed.

- [ ] **Step 6: Commit**

```bash
git add kion/tfstate/readers.py kion/tfstate/collect.py tests/test_tfstate_readers.py
git commit -m "feat: parent-scoped list reader for enforcement/criteria resources"
```

---

### Task 9: Association reader

Four permission-mapping resources are `kind: association` — a scope's mappings are read as a list hanging off that scope, and each mapping's identity is the `(scope_id, app_role_id)` pair rather than an id. They have no `id` of their own, which every later task assumes exists, so this reader synthesizes one.

**Files:**
- Modify: `kion/tfstate/readers.py` (add `ASSOCIATION_SPECS`)
- Modify: `kion/tfstate/collect.py` (add `read_association`)
- Test: `tests/test_tfstate_association.py`

**Interfaces:**
- Produces:
  - `@dataclass AssociationSpec(parent_kind: str, parent_list_path: str, child_path: str, parent_id_field: str, key_fields: tuple[str, ...])`
  - `ASSOCIATION_SPECS: dict[str, AssociationSpec]`
  - `read_association(client, spec, tf_type, kind) -> KindResult` — each record gains a synthesized `id` of `"<parent_id>-<key field values joined by ->"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_association.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.collect import read_association
from kion.tfstate.readers import ASSOCIATION_SPECS, AssociationSpec
from tests.test_tfstate_readers import RouteClient

SPEC = AssociationSpec(parent_kind="ou", parent_list_path="/v3/ou",
                       child_path="/v3/ou/{parent_id}/permission-mapping",
                       parent_id_field="ou_id", key_fields=("app_role_id",))


def test_synthesizes_a_composite_id():
    c = RouteClient({
        "/v3/ou": [{"id": 3}],
        "/v3/ou/3/permission-mapping": [{"app_role_id": 2, "user_ids": [1]}],
    })
    r = read_association(c, SPEC, "kion_ou_permission_mapping", "ou_permission_mapping")
    assert r.records[0]["id"] == "3-2"
    assert r.records[0]["ou_id"] == 3


def test_existing_id_is_preserved():
    c = RouteClient({"/v3/ou": [{"id": 3}],
                     "/v3/ou/3/permission-mapping": [{"id": "keep", "app_role_id": 2}]})
    r = read_association(c, SPEC, "kion_ou_permission_mapping", "ou_permission_mapping")
    assert r.records[0]["id"] == "keep"


def test_ids_are_unique_across_parents():
    c = RouteClient({
        "/v3/ou": [{"id": 3}, {"id": 4}],
        "/v3/ou/3/permission-mapping": [{"app_role_id": 2}],
        "/v3/ou/4/permission-mapping": [{"app_role_id": 2}],
    })
    r = read_association(c, SPEC, "kion_ou_permission_mapping", "ou_permission_mapping")
    assert sorted(x["id"] for x in r.records) == ["3-2", "4-2"]


def test_specs_exist_for_every_association_resource():
    for t in ("kion_ou_permission_mapping", "kion_project_permission_mapping",
              "kion_funding_source_permission_mapping", "kion_global_permission_mapping"):
        assert t in ASSOCIATION_SPECS, t
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_association.py -v`
Expected: FAIL — `cannot import name 'ASSOCIATION_SPECS'`.

- [ ] **Step 3: Add the specs**

Append to `kion/tfstate/readers.py`:

```python
@dataclass(frozen=True)
class AssociationSpec:
    """A membership record: identity is (parent, key_fields), not an id.

    docs/ONBOARDING_REPORT.md skips all four of these for RECONCILE because the
    engine's four natural-key kinds cannot express a composite. State export has
    no such constraint -- it needs an address, and a deterministic synthesized id
    is a perfectly good one.
    """
    parent_kind: str
    parent_list_path: str
    child_path: str
    parent_id_field: str
    key_fields: tuple


ASSOCIATION_SPECS: dict[str, AssociationSpec] = {
    "kion_ou_permission_mapping": AssociationSpec(
        "ou", "/v3/ou", "/v3/ou/{parent_id}/permission-mapping",
        "ou_id", ("app_role_id",)),
    "kion_project_permission_mapping": AssociationSpec(
        "project", "/v3/project", "/v3/project/{parent_id}/permission-mapping",
        "project_id", ("app_role_id",)),
    "kion_funding_source_permission_mapping": AssociationSpec(
        "funding_source", "/v3/funding-source",
        "/v3/funding-source/{parent_id}/permission-mapping",
        "funding_source_id", ("app_role_id",)),
    "kion_global_permission_mapping": AssociationSpec(
        "", "", "/v3/global-permission-mapping", "", ("app_role_id",)),
}
```

- [ ] **Step 4: Add the reader to `collect.py`**

Append to `kion/tfstate/collect.py`:

```python
def read_association(client, spec, tf_type: str, kind: str) -> KindResult:
    """Read membership records and give each a deterministic synthetic id.

    ``parent_list_path`` may be empty for a global mapping, in which case the
    child path is read once with no parent.
    """
    result = KindResult(kind=kind, tf_type=tf_type, bucket="association")
    failures = []

    if spec.parent_list_path:
        parents = list_records(client, spec.parent_list_path,
                               on_error=lambda p, e: failures.append(f"{p}: {e}"))
        if failures:
            result.status, result.reason = "error", failures[0]
            return result
        parent_ids = [p["id"] for p in parents if isinstance(p, dict) and "id" in p]
    else:
        parent_ids = [None]

    for pid in parent_ids:
        path = spec.child_path.format(parent_id=pid) if pid is not None else spec.child_path
        for child in list_records(client, path,
                                  on_error=lambda p, e: failures.append(f"{p}: {e}")):
            if not isinstance(child, dict):
                continue
            if pid is not None:
                child.setdefault(spec.parent_id_field, pid)
            if "id" not in child:
                parts = [str(pid)] if pid is not None else []
                parts += [str(child.get(f)) for f in spec.key_fields]
                child["id"] = "-".join(parts)
            result.records.append(child)

    if failures:
        result.reason = f"{len(failures)} read(s) failed; first: {failures[0]}"
    result.status = "ok" if result.records else ("error" if failures else "empty")
    return result
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_association.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add kion/tfstate/readers.py kion/tfstate/collect.py tests/test_tfstate_association.py
git commit -m "feat: association reader with synthesized composite ids"
```

---

### Task 10: Special archetypes and declared gaps

Four `special` resources and three `no_read` resources remain. Each gets an explicit, tested outcome — never a silent absence.

**Files:**
- Modify: `kion/tfstate/readers.py` (add `SPECIAL_SPECS`, `UNREADABLE`)
- Modify: `kion/tfstate/collect.py` (add `read_special`, `unreadable_result`, `collect_all`)
- Test: `tests/test_tfstate_special.py`

**Interfaces:**
- Produces:
  - `SPECIAL_SPECS: dict[str, str]` — tf_type → list path (singleton reads wrap the single object into a one-record list).
  - `UNREADABLE: dict[str, str]` — tf_type → reason.
  - `read_special(client, tf_type, kind, path) -> KindResult`
  - `collect_all(client, schemas, meta, archetypes) -> list[KindResult]` — the one entry point Task 11 calls; routes each tf_type to its bucket's reader, and retries a failed `generic` read against `PARENT_SPECS` when one exists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_special.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import yaml
from kion.client import KionAPIError
from kion.meta.load import load_resource_meta
from kion.tfstate.collect import collect_all, read_special, unreadable_result
from kion.tfstate.readers import UNREADABLE
from kion.tfstate.schema import load_provider_schema
from tests.test_tfstate_readers import RouteClient

VENDOR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "kion", "meta", "vendor")


def _archetypes():
    with open(os.path.join(VENDOR, "crud_archetypes.yaml")) as f:
        return yaml.safe_load(f) or {}


def test_singleton_is_wrapped_into_one_record():
    c = RouteClient({"/v3/app-config": {"smtp_host": "mail"}})
    r = read_special(c, "kion_app_config", "app_config", "/v3/app-config")
    assert r.status == "ok"
    assert len(r.records) == 1
    assert r.records[0]["smtp_host"] == "mail"


def test_singleton_gets_a_stable_id():
    c = RouteClient({"/v3/app-config": {"smtp_host": "mail"}})
    r = read_special(c, "kion_app_config", "app_config", "/v3/app-config")
    assert r.records[0]["id"] == "app_config"


def test_unreadable_is_declared_not_dropped():
    r = unreadable_result("kion_aws_resource_tag", "aws_resource_tag")
    assert r.status == "unsupported"
    assert r.records == []
    assert "no_read" in r.reason


def test_every_no_read_resource_has_a_stated_reason():
    for t in ("kion_aws_resource_tag", "kion_ou_cloud_access_role_exemption",
              "kion_project_cloud_access_role_exemption"):
        assert t in UNREADABLE and UNREADABLE[t]


def test_collect_all_returns_a_result_for_every_resource():
    """The contract: 68 in, 68 out. No resource is ever silently absent."""
    schemas = load_provider_schema()
    results = collect_all(RouteClient({}), schemas, load_resource_meta(), _archetypes())
    assert len(results) == len(schemas)
    assert {r.tf_type for r in results} == set(schemas)


def test_collect_all_falls_back_to_parent_scoped_on_a_failed_generic_read():
    """The 405 lesson, wired in: compliance_family's flat list fails, so the
    parent-scoped fallback in PARENT_SPECS is used instead."""
    c = RouteClient({
        "/v4/compliance/family": KionAPIError("405 Method Not Allowed"),
        "/v4/compliance/program": [{"id": 1}],
        "/v4/compliance/program/1/family": [{"id": 9, "name": "f"}],
    })
    schemas = load_provider_schema()
    results = {r.tf_type: r for r in
               collect_all(c, schemas, load_resource_meta(), _archetypes())}
    fam = results["kion_compliance_family"]
    assert fam.status == "ok"
    assert fam.bucket == "parent_list"
    assert fam.records[0]["id"] == 9
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_special.py -v`
Expected: FAIL — `cannot import name 'collect_all'`.

- [ ] **Step 3: Add specs and the gap table**

Append to `kion/tfstate/readers.py`:

```python
# Resources with a bespoke read shape. The value is the list (or singleton) path.
SPECIAL_SPECS: dict[str, str] = {
    "kion_app_config": "/v3/app-config",                    # singleton
    "kion_dashboard": "/beta/dashboard",                    # raw_http
    "kion_funding_source_note": "/v3/funding-source-note",  # raw_http
    "kion_custom_variable_override": "/v3/custom-variable-override",
}

# Resources that genuinely cannot be enumerated. The provider's own codegen
# declares them kind: no_read -- there is no by-id GET and no listable
# collection. Declared here so the coverage report states the gap rather than
# the export quietly omitting them.
UNREADABLE: dict[str, str] = {
    "kion_aws_resource_tag":
        "crud_archetypes.yaml declares kind: no_read - no by-id GET, no listable collection",
    "kion_ou_cloud_access_role_exemption":
        "crud_archetypes.yaml declares kind: no_read - exemptions are write-only via their CAR",
    "kion_project_cloud_access_role_exemption":
        "crud_archetypes.yaml declares kind: no_read - exemptions are write-only via their CAR",
}
```

- [ ] **Step 4: Add the readers and the entry point to `collect.py`**

Append to `kion/tfstate/collect.py` (and add `from kion.tfstate.readers import ASSOCIATION_SPECS, PARENT_SPECS, SPECIAL_SPECS, UNREADABLE` to its imports):

```python
def read_special(client, tf_type: str, kind: str, path: str) -> KindResult:
    """Read a bespoke endpoint. A singleton object is wrapped into one record
    and given a stable synthetic id (there is only ever one)."""
    result = KindResult(kind=kind, tf_type=tf_type, bucket="special")
    failures = []
    records = list_records(client, path,
                           on_error=lambda p, e: failures.append(f"{p}: {e}"))
    if failures and not records:
        result.status, result.reason = "error", failures[0]
        return result

    if isinstance(records, dict):                       # singleton envelope
        records = [records]
    result.records = [r for r in records if isinstance(r, dict)]
    for r in result.records:
        r.setdefault("id", kind)
    result.status = "ok" if result.records else "empty"
    return result


def unreadable_result(tf_type: str, kind: str) -> KindResult:
    return KindResult(kind=kind, tf_type=tf_type, status="unsupported",
                      reason=UNREADABLE.get(tf_type, "not readable"), bucket="no_read")


def collect_all(client, schemas: dict, meta: dict, archetypes: dict) -> list[KindResult]:
    """Read every resource the provider serves. Always returns one result per
    resource -- ``status`` says what happened, and nothing is silently absent."""
    buckets = classify(schemas, meta, archetypes)
    out = []
    for tf_type in sorted(schemas):
        kind = tf_type[len("kion_"):]
        bucket = buckets[tf_type]

        if bucket == "generic":
            res = read_generic(client, meta[kind], tf_type)
            # Metadata said listable; the live install may disagree (the 405
            # lesson). Fall back to a parent-scoped read where one is declared.
            if res.status == "error" and tf_type in PARENT_SPECS:
                fallback = read_parent_scoped(client, PARENT_SPECS[tf_type], tf_type, kind)
                if fallback.status in ("ok", "empty"):
                    fallback.reason = (f"flat list failed ({res.reason}); "
                                       f"read parent-scoped instead")
                    res = fallback
        elif bucket == "parent_list":
            res = read_parent_scoped(client, PARENT_SPECS[tf_type], tf_type, kind) \
                if tf_type in PARENT_SPECS else \
                KindResult(kind=kind, tf_type=tf_type, status="unsupported",
                           reason="no ParentSpec declared", bucket=bucket)
        elif bucket == "association":
            res = read_association(client, ASSOCIATION_SPECS[tf_type], tf_type, kind) \
                if tf_type in ASSOCIATION_SPECS else \
                KindResult(kind=kind, tf_type=tf_type, status="unsupported",
                           reason="no AssociationSpec declared", bucket=bucket)
        elif bucket == "special":
            res = read_special(client, tf_type, kind, SPECIAL_SPECS[tf_type]) \
                if tf_type in SPECIAL_SPECS else \
                KindResult(kind=kind, tf_type=tf_type, status="unsupported",
                           reason="no special read path declared", bucket=bucket)
        elif bucket == "no_read":
            res = unreadable_result(tf_type, kind)
        else:                                            # unmapped
            res = KindResult(kind=kind, tf_type=tf_type, status="unsupported",
                             reason="no read path in generator_config or READ_OVERRIDES",
                             bucket=bucket)
        out.append(res)
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_special.py -v`
Expected: 6 passed.

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add kion/tfstate/readers.py kion/tfstate/collect.py tests/test_tfstate_special.py
git commit -m "feat: special-archetype readers, declared gaps, collect_all entry point"
```

---

### Task 11: Snapshot document assembly

**Files:**
- Create: `kion/tfstate/emit.py`
- Test: `tests/test_tfstate_emit.py`

**Interfaces:**
- Consumes: `KindResult` (Task 7–10), `ResourceSchema` (Task 2), `to_state_attributes` (Task 4), `LabelAllocator` (Task 5), `sidecar`/`dependencies` (Task 6), `load_references` (`kion/meta/load.py`), `load_natural_keys` + `natural_key` (`kion/engine/keys.py`).
- Produces: `build_document(results, schemas, refs, nkeys, source_url, exported_at) -> dict`

Document shape:

```json
{
  "schema_version": 1,
  "provider": "registry.terraform.io/kionsoftware/kion",
  "source_url": "https://demo1.kion.io",
  "exported_at": "2026-08-24T00:00:00+00:00",
  "resources": {
    "kion_ou": {
      "schema_version": 1,
      "instances": [
        {"label": "central_services", "source_id": "5",
         "attributes": {...}, "refs": {"__ref__parent_ou_id": ["ou", "Root"]},
         "dependencies": ["kion_ou.root"]}
      ]
    }
  },
  "coverage": [{"tf_type": "...", "bucket": "...", "status": "...", "count": 0, "reason": "..."}]
}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_emit.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import Reference
from kion.tfstate.collect import KindResult
from kion.tfstate.emit import build_document
from kion.tfstate.schema import load_provider_schema

SCHEMAS = load_provider_schema()
REFS = {"ou": [Reference(field="parent_ou_id", target="ou", key="name")]}
NKEYS = {"ou": {"kind": "name"}}


def _results():
    return [
        KindResult(kind="ou", tf_type="kion_ou", status="ok", bucket="generic", records=[
            {"id": 1, "name": "Root", "parent_ou_id": 0, "permission_scheme_id": 3},
            {"id": 5, "name": "Central Services", "parent_ou_id": 1, "permission_scheme_id": 3},
        ]),
        KindResult(kind="aws_resource_tag", tf_type="kion_aws_resource_tag",
                   status="unsupported", bucket="no_read", reason="kind: no_read"),
    ]


def test_document_envelope():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "https://k.example.com", "2026-08-24T00:00:00+00:00")
    assert d["schema_version"] == 1
    assert d["provider"] == "registry.terraform.io/kionsoftware/kion"
    assert d["source_url"] == "https://k.example.com"
    assert d["exported_at"] == "2026-08-24T00:00:00+00:00"


def test_instances_carry_label_source_id_and_attributes():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    ous = d["resources"]["kion_ou"]["instances"]
    assert [i["label"] for i in ous] == ["root", "central_services"]
    assert ous[1]["source_id"] == "5"
    assert ous[1]["attributes"]["id"] == "5"
    assert ous[1]["attributes"]["parent_ou_id"] == 1


def test_resource_schema_version_is_recorded():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    assert d["resources"]["kion_ou"]["schema_version"] == 1


def test_sidecar_and_dependencies():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    child = d["resources"]["kion_ou"]["instances"][1]
    assert child["refs"]["__ref__parent_ou_id"] == ["ou", "Root"]
    assert child["dependencies"] == ["kion_ou.root"]


def test_root_has_no_dependencies():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    assert d["resources"]["kion_ou"]["instances"][0]["dependencies"] == []


def test_unsupported_kinds_appear_in_coverage_not_resources():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    assert "kion_aws_resource_tag" not in d["resources"]
    row = [c for c in d["coverage"] if c["tf_type"] == "kion_aws_resource_tag"][0]
    assert row["status"] == "unsupported"
    assert row["count"] == 0
    assert row["reason"] == "kind: no_read"


def test_coverage_has_a_row_per_result():
    d = build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t")
    assert len(d["coverage"]) == 2


def test_document_is_json_serializable():
    import json
    json.dumps(build_document(_results(), SCHEMAS, REFS, NKEYS, "u", "t"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_emit.py -v`
Expected: FAIL — `No module named 'kion.tfstate.emit'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/emit.py
"""Assemble the state-shaped snapshot document.

Two passes, for the same reason kion/overrides/kiontocart-style translators use
two: labels and natural keys for EVERY record must exist before any record's
dependencies or __ref__ sidecar can be resolved, because references point both
forwards and backwards (an OU's parent may be read after the child).
"""
from __future__ import annotations

from kion.engine.keys import natural_key
from kion.tfstate.attrs import to_state_attributes
from kion.tfstate.labels import LabelAllocator
from kion.tfstate.refs import dependencies, sidecar
from kion.tfstate.schema import PROVIDER_ADDR

DOCUMENT_VERSION = 1


def build_document(results, schemas: dict, refs: dict, nkeys: dict,
                   source_url: str, exported_at: str) -> dict:
    """KindResults -> the state-shaped snapshot document."""
    labels = LabelAllocator()
    id_to_key: dict[tuple, tuple] = {}
    tf_type_of = {r.kind: r.tf_type for r in results}

    # Pass 1: allocate every label and natural key up front.
    for res in results:
        if res.status not in ("ok", "empty"):
            continue
        for record in res.records:
            labels.allocate(res.tf_type, record)
            if res.kind in nkeys:
                try:
                    id_to_key[(res.kind, record.get("id"))] = natural_key(
                        res.kind, record, nkeys)
                except Exception:
                    pass          # a key we cannot build simply has no sidecar

    # Pass 2: serialize.
    resources: dict[str, dict] = {}
    coverage = []
    for res in results:
        coverage.append({
            "tf_type": res.tf_type, "bucket": res.bucket, "status": res.status,
            "count": len(res.records), "reason": res.reason,
        })
        if res.status not in ("ok", "empty") or not res.records:
            continue

        rs = schemas[res.tf_type]
        kind_refs = refs.get(res.kind, [])
        instances = []
        for record in res.records:
            state = to_state_attributes(record, rs)
            instances.append({
                "label": labels.allocate(res.tf_type, record),
                "source_id": str(record.get("id", "")),
                "attributes": state.attributes,
                "refs": sidecar(record, kind_refs, id_to_key),
                "dependencies": dependencies(record, kind_refs, tf_type_of, labels.index),
                "missing_attributes": state.missing,
                "unmapped_fields": state.extra,
            })
        resources[res.tf_type] = {"schema_version": rs.version, "instances": instances}

    return {
        "schema_version": DOCUMENT_VERSION,
        "provider": PROVIDER_ADDR,
        "source_url": source_url,
        "exported_at": exported_at,
        "resources": resources,
        "coverage": coverage,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_emit.py -v`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/emit.py tests/test_tfstate_emit.py
git commit -m "feat: assemble the state-shaped snapshot document"
```

---

### Task 12: Terraform v4 state envelope

**Files:**
- Create: `kion/tfstate/statefile.py`
- Test: `tests/test_tfstate_statefile.py`

**Interfaces:**
- Consumes: the document from Task 11, `ResourceSchema.sensitive_paths()` from Task 2.
- Produces: `build_state(document, schemas, terraform_version="1.9.0", lineage=None, serial=1) -> dict`

`lineage` must be a UUID. It is a **parameter with a deterministic default derived from `source_url`** — not `uuid4()` — so re-running the export produces byte-identical output and diffs stay reviewable.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_statefile.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.schema import load_provider_schema
from kion.tfstate.statefile import build_state

SCHEMAS = load_provider_schema()
DOC = {
    "schema_version": 1,
    "provider": "registry.terraform.io/kionsoftware/kion",
    "source_url": "https://k.example.com",
    "exported_at": "2026-08-24T00:00:00+00:00",
    "resources": {
        "kion_ou": {"schema_version": 1, "instances": [
            {"label": "root", "source_id": "1",
             "attributes": {"id": "1", "name": "Root"},
             "refs": {}, "dependencies": []},
            {"label": "central", "source_id": "5",
             "attributes": {"id": "5", "name": "Central"},
             "refs": {}, "dependencies": ["kion_ou.root"]},
        ]},
    },
    "coverage": [],
}


def test_envelope():
    s = build_state(DOC, SCHEMAS)
    assert s["version"] == 4
    assert s["terraform_version"] == "1.9.0"
    assert s["serial"] == 1
    assert s["outputs"] == {}
    assert isinstance(s["lineage"], str) and len(s["lineage"]) == 36


def test_lineage_is_deterministic():
    """Re-exporting an unchanged install must produce an identical file."""
    assert build_state(DOC, SCHEMAS)["lineage"] == build_state(DOC, SCHEMAS)["lineage"]


def test_lineage_differs_per_source_install():
    other = dict(DOC, source_url="https://other.example.com")
    assert build_state(DOC, SCHEMAS)["lineage"] != build_state(other, SCHEMAS)["lineage"]


def test_resource_block_shape():
    r = build_state(DOC, SCHEMAS)["resources"][0]
    assert r["mode"] == "managed"
    assert r["type"] == "kion_ou"
    assert r["name"] == "root"
    assert r["provider"] == 'provider["registry.terraform.io/kionsoftware/kion"]'


def test_one_resource_block_per_instance():
    """Each instance gets its own address, so each is its own resource block."""
    s = build_state(DOC, SCHEMAS)
    assert [r["name"] for r in s["resources"]] == ["root", "central"]


def test_instance_shape():
    inst = build_state(DOC, SCHEMAS)["resources"][1]["instances"][0]
    assert inst["schema_version"] == 1
    assert inst["attributes"] == {"id": "5", "name": "Central"}
    assert inst["dependencies"] == ["kion_ou.root"]
    assert inst["sensitive_attributes"] == []


def test_sidecars_are_not_written_into_state():
    """__ref__ keys are ours, not Terraform's - state must stay schema-clean."""
    doc = dict(DOC)
    doc["resources"] = {"kion_ou": {"schema_version": 1, "instances": [
        {"label": "root", "source_id": "1", "attributes": {"id": "1"},
         "refs": {"__ref__parent_ou_id": ["ou", "X"]}, "dependencies": []}]}}
    inst = build_state(doc, SCHEMAS)["resources"][0]["instances"][0]
    assert all(not k.startswith("__ref__") for k in inst["attributes"])


def test_sensitive_attributes_come_from_the_schema():
    doc = dict(DOC)
    doc["resources"] = {"kion_app_config": {"schema_version": 0, "instances": [
        {"label": "app_config", "source_id": "app_config",
         "attributes": {"id": "app_config"}, "refs": {}, "dependencies": []}]}}
    inst = build_state(doc, SCHEMAS)["resources"][0]["instances"][0]
    assert inst["sensitive_attributes"] == [[{"type": "get_attr", "value": "smtp_password"}]]


def test_state_is_json_serializable():
    import json
    json.dumps(build_state(DOC, SCHEMAS))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_statefile.py -v`
Expected: FAIL — `No module named 'kion.tfstate.statefile'`.

- [ ] **Step 3: Write the implementation**

```python
# kion/tfstate/statefile.py
"""Assemble a Terraform v4 state file from the snapshot document.

Deliberately thin: every hard decision (attribute shape, ids, labels) was made
upstream. What is decided HERE is envelope-level:

  * one resource block per instance, at the root module. Module addressing is
    left to the module-emitter that comes later; a flat root-module state is
    valid on its own and does not pre-commit that design.
  * lineage is DERIVED from source_url, not random. Terraform only requires a
    UUID; making it deterministic means re-exporting an unchanged install
    produces a byte-identical file, so a diff means something changed in Kion.
  * __ref__ sidecars are OUR metadata and never reach state - Terraform would
    reject attributes its schema does not declare.
"""
from __future__ import annotations

import hashlib
import uuid

from kion.tfstate.schema import PROVIDER_ADDR

STATE_VERSION = 4
DEFAULT_TERRAFORM_VERSION = "1.9.0"


def _lineage(source_url: str) -> str:
    digest = hashlib.sha256((source_url or "").encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def build_state(document: dict, schemas: dict,
                terraform_version: str = DEFAULT_TERRAFORM_VERSION,
                lineage: str | None = None, serial: int = 1) -> dict:
    """The snapshot document -> a Terraform v4 state file."""
    provider = f'provider["{PROVIDER_ADDR}"]'
    resources = []

    for tf_type, entry in document.get("resources", {}).items():
        rs = schemas.get(tf_type)
        sensitive = rs.sensitive_paths() if rs else []
        version = entry.get("schema_version", 0)
        for inst in entry.get("instances", []):
            attributes = {k: v for k, v in inst["attributes"].items()
                          if not k.startswith("__ref__")}
            resources.append({
                "mode": "managed",
                "type": tf_type,
                "name": inst["label"],
                "provider": provider,
                "instances": [{
                    "schema_version": version,
                    "attributes": attributes,
                    "sensitive_attributes": sensitive,
                    "dependencies": inst.get("dependencies", []),
                }],
            })

    return {
        "version": STATE_VERSION,
        "terraform_version": terraform_version,
        "serial": serial,
        "lineage": lineage or _lineage(document.get("source_url", "")),
        "outputs": {},
        "resources": resources,
        "check_results": None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_statefile.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add kion/tfstate/statefile.py tests/test_tfstate_statefile.py
git commit -m "feat: assemble Terraform v4 state file from the snapshot document"
```

---

### Task 13: Coverage report and the `export-tfstate` command

**Files:**
- Create: `kion/tfstate/report.py`
- Modify: `kion_copy.py`
- Modify: `.gitignore`
- Modify: `CLAUDE.md`
- Test: `tests/test_tfstate_report.py`

**Interfaces:**
- Consumes: the document from Task 11.
- Produces:
  - `format_report(document: dict) -> str`
  - `cmd_export_tfstate(args) -> int` in `kion_copy.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tfstate_report.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.tfstate.report import format_report

DOC = {"resources": {}, "coverage": [
    {"tf_type": "kion_ou", "bucket": "generic", "status": "ok", "count": 22, "reason": ""},
    {"tf_type": "kion_label", "bucket": "generic", "status": "empty", "count": 0, "reason": ""},
    {"tf_type": "kion_aws_resource_tag", "bucket": "no_read", "status": "unsupported",
     "count": 0, "reason": "kind: no_read"},
    {"tf_type": "kion_dashboard", "bucket": "special", "status": "error",
     "count": 0, "reason": "/beta/dashboard: 500"},
]}


def test_totals_line():
    out = format_report(DOC)
    assert "4 resource types" in out
    assert "22 records" in out


def test_each_status_is_counted():
    out = format_report(DOC)
    assert "ok: 1" in out and "empty: 1" in out
    assert "unsupported: 1" in out and "error: 1" in out


def test_gaps_are_listed_with_their_reason():
    out = format_report(DOC)
    assert "kion_aws_resource_tag" in out and "kind: no_read" in out
    assert "kion_dashboard" in out and "/beta/dashboard: 500" in out


def test_healthy_kinds_are_not_listed_individually():
    """The report is about what went wrong; 50 OK lines is noise."""
    assert "kion_ou" not in format_report(DOC)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tfstate_report.py -v`
Expected: FAIL — `No module named 'kion.tfstate.report'`.

- [ ] **Step 3: Write the report**

```python
# kion/tfstate/report.py
"""Human-readable coverage report.

The coverage table is the export's contract: 68 resource types in, 68 rows out.
This prints the totals and then ONLY the rows that need a human -- unsupported
and error. Listing all 50 healthy kinds would bury the four that matter.
"""
from __future__ import annotations

import collections


def format_report(document: dict) -> str:
    coverage = document.get("coverage", [])
    by_status = collections.Counter(row["status"] for row in coverage)
    total_records = sum(row["count"] for row in coverage)

    lines = [
        "",
        f"Coverage: {len(coverage)} resource types, {total_records} records",
        "  " + ", ".join(f"{s}: {n}" for s, n in sorted(by_status.items())),
    ]

    gaps = [r for r in coverage if r["status"] in ("unsupported", "error")]
    if gaps:
        lines.append("")
        lines.append(f"Not exported ({len(gaps)}):")
        for row in sorted(gaps, key=lambda r: r["tf_type"]):
            lines.append(f"  {row['tf_type']:45s} {row['status']:12s} {row['reason']}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_tfstate_report.py -v`
Expected: 4 passed.

- [ ] **Step 5: Wire up the CLI**

In `kion_copy.py`, add these imports beside the existing ones:

```python
from datetime import datetime, timezone

from kion.meta.load import load_natural_keys, load_references, load_resource_meta
from kion.tfstate.collect import collect_all
from kion.tfstate.emit import build_document
from kion.tfstate.report import format_report
from kion.tfstate.schema import load_provider_schema
from kion.tfstate.statefile import build_state
```

Add the command function after `cmd_export`:

```python
def _archetypes():
    import os
    import yaml
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "kion", "meta", "vendor", "crud_archetypes.yaml")
    with open(path) as f:
        return yaml.safe_load(f) or {}


def cmd_export_tfstate(args) -> int:
    cfg = Config.load(args.env_file)
    client = _client(cfg)
    print(f"Exporting Terraform state from {cfg.url} ...")

    schemas = load_provider_schema()
    results = collect_all(client, schemas, load_resource_meta(), _archetypes())
    document = build_document(
        results, schemas, load_references(), load_natural_keys(),
        cfg.url, datetime.now(timezone.utc).isoformat(),
    )

    with open(args.out, "w") as f:
        json.dump(document, f, indent=2)
    print(f"\nWrote snapshot -> {args.out}")

    if not args.no_state:
        state = build_state(document, schemas, terraform_version=args.terraform_version)
        with open(args.state_out, "w") as f:
            json.dump(state, f, indent=2)
        print(f"Wrote state    -> {args.state_out} ({len(state['resources'])} resources)")

    print(format_report(document))
    return 0
```

And register the subparser alongside the existing `export` / `import` ones:

```python
    p_state = sub.add_parser("export-tfstate",
                             help="read a source install -> Terraform state JSON")
    p_state.add_argument("--env-file", default=".env")
    p_state.add_argument("--out", default="tfstate-snapshot.json",
                         help="state-shaped snapshot document")
    p_state.add_argument("--state-out", default="terraform.tfstate",
                         help="assembled Terraform v4 state file")
    p_state.add_argument("--no-state", action="store_true",
                         help="write only the snapshot, not the state file")
    p_state.add_argument("--terraform-version", default="1.9.0")
    p_state.set_defaults(func=cmd_export_tfstate)
```

- [ ] **Step 6: Verify the CLI wiring**

Run: `python kion_copy.py export-tfstate --help`
Expected: usage text listing `--out`, `--state-out`, `--no-state`, `--terraform-version`.

- [ ] **Step 7: Ignore the outputs**

Append to `.gitignore`:

```
tfstate-snapshot*.json
*.tfstate
*.tfstate.backup
```

- [ ] **Step 8: Document it in CLAUDE.md**

Add to the "Running it" section:

```sh
python kion_copy.py export-tfstate --env-file .env.source   # -> tfstate-snapshot.json + terraform.tfstate
```

And add a new subsection under "Metadata-driven engine (experimental)":

```markdown
### Terraform state export

`kion/tfstate/` reads **all 68 resources the new Terraform provider serves** (vs
the engine's 14 reconcilable kinds) and writes them in the provider's state
shape. It can cover far more than the engine reconciles because state export
needs no natural key — only "list the records and take their id".

The authority for the output shape is `kion/meta/vendor/schema_snapshots/new.json`,
the provider's own `terraform providers schema -json` output, vendored by
`scripts/sync-provider-meta.sh`. Attribute names, JSON types and per-resource
`schema_version` all come from there — never hand-maintained.

Readability buckets (`kion/tfstate/collect.py:classify`): 50 generic flat-list,
4 parent-scoped, 4 association, 4 special, 3 `no_read` (genuinely unreadable),
3 covered by `READ_OVERRIDES`. Every one of the 68 produces a coverage row —
a resource is never silently absent.

State attributes carry **literal source-install ids** (that is what makes them
state); the portable natural key rides alongside in a `__ref__<field>` sidecar
in the snapshot, and is stripped from the state file itself.

**Metadata is an opinion.** `compliance_family` / `compliance_level` are declared
listable but 405 live (see `docs/ONBOARDING_REPORT.md`), so a failed generic read
falls back to the parent-scoped spec in `kion/tfstate/readers.py:PARENT_SPECS`.
```

- [ ] **Step 9: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add kion/tfstate/report.py kion_copy.py .gitignore CLAUDE.md tests/test_tfstate_report.py
git commit -m "feat: export-tfstate command with coverage reporting"
```

---

### Task 14: Live validation against a real install

Everything so far is schema-conformant. This task proves it is *valid* — the difference the old importer-script never closed, which is why its README concedes the generated config "will not produce a clean `terraform plan`".

**Files:**
- Create: `scripts/tfstate_probe.py`
- Test: manual, against a live install (no unit test — this is the live gate, the same role `scripts/equivalence_check.py` plays for the engine)

**Interfaces:**
- Consumes: `collect_all` (Task 10), `build_document` (Task 11), `build_state` (Task 12).
- Produces: `scripts/tfstate_probe.py` — prints per-resource read outcomes, so a real install's answer can be diffed against `classify`'s prediction.

- [ ] **Step 1: Write the probe**

```python
#!/usr/bin/env python3
"""Live probe: what does a real install actually let us read?

classify() predicts a readability bucket from vendored metadata. This asks the
install. Any row marked DRIFT is a resource whose metadata lies -- exactly the
failure docs/ONBOARDING_REPORT.md recorded for compliance_family/level (declared
listable, 405 live). Run this against every install family you support before
trusting the export.

    python scripts/tfstate_probe.py --env-file .env.source
"""
from __future__ import annotations

import argparse
import os
import sys

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kion.client import KionClient
from kion.config import Config
from kion.meta.load import load_resource_meta
from kion.tfstate.collect import classify, collect_all
from kion.tfstate.schema import load_provider_schema


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env-file", default=".env")
    args = ap.parse_args()

    cfg = Config.load(args.env_file)
    client = KionClient(cfg.url, cfg.api_key, verify_ssl=cfg.verify_ssl,
                        api_prefix=cfg.api_prefix)

    vendor = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "kion", "meta", "vendor", "crud_archetypes.yaml")
    with open(vendor) as f:
        archetypes = yaml.safe_load(f) or {}

    schemas = load_provider_schema()
    meta = load_resource_meta()
    predicted = classify(schemas, meta, archetypes)
    results = collect_all(client, schemas, meta, archetypes)

    drift = 0
    print(f"{'resource':45s} {'predicted':14s} {'actual':14s} {'status':12s} count")
    for res in sorted(results, key=lambda r: r.tf_type):
        p = predicted[res.tf_type]
        flag = ""
        if res.bucket != p or res.status == "error":
            flag, drift = "  <- DRIFT", drift + 1
        print(f"{res.tf_type:45s} {p:14s} {res.bucket:14s} "
              f"{res.status:12s} {len(res.records):5d}{flag}")
        if flag and res.reason:
            print(f"      {res.reason}")

    print(f"\n{drift} resource(s) drifted from the metadata prediction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the probe against a live install**

Run: `python scripts/tfstate_probe.py --env-file .env.source`
Expected: a row per resource. Record every `DRIFT` row.

- [ ] **Step 3: Resolve the drift**

For each drifted resource, in this order:
1. If a flat list 405s but a parent-scoped collection exists → add a `ParentSpec` to `kion/tfstate/readers.py:PARENT_SPECS`. The fallback in `collect_all` picks it up with no further change.
2. If the path in `SPECIAL_SPECS` / `ASSOCIATION_SPECS` is wrong → correct it (those paths are authored, not vendored, and were never verified).
3. If the resource is genuinely unreadable on this install → add it to `UNREADABLE` with the observed cause.

Re-run the probe until only genuine gaps remain.

- [ ] **Step 4: Generate a state file and validate it with Terraform**

```bash
python kion_copy.py export-tfstate --env-file .env.source \
  --out /tmp/tfstate-snapshot.json --state-out /tmp/probe/terraform.tfstate
cd /tmp/probe
cat > versions.tf <<'EOF'
terraform {
  required_providers {
    kion = {
      source  = "kionsoftware/kion"
      version = ">= 1.0.0"
    }
  }
}
EOF
terraform init
terraform show -json terraform.tfstate | head -20
```

Expected: `terraform show` parses the state and echoes it back as JSON. A malformed state fails here with a specific message (`unsupported state file format version`, `invalid resource address`, or an attribute type error naming the offending resource).

- [ ] **Step 5: Record the findings**

Add a `## Live validation` section to `docs/ONBOARDING_REPORT.md` with: the install probed, its version, the drift count, each drifted resource with its resolution, and the `terraform show` outcome.

- [ ] **Step 6: Commit**

```bash
git add scripts/tfstate_probe.py kion/tfstate/readers.py docs/ONBOARDING_REPORT.md
git commit -m "feat: live readability probe; record validation findings"
```

---

## Self-Review

**Spec coverage.** Each of the three design decisions maps to tasks: all-68 scope → Tasks 7–10 (`classify` + four readers + `collect_all`, with `test_collect_all_returns_a_result_for_every_resource` as the contract); both artifacts → Tasks 11 and 12, wired in Task 13; literal IDs + sidecar → Tasks 4 and 6, with `test_sidecars_are_not_written_into_state` proving the two stay separate. The stale-vendor blocker is Task 1. The "metadata is an opinion" risk is handled twice — the fallback in `collect_all` (Task 10) and the live probe (Task 14).

**Placeholder scan.** No TBD/TODO steps; every code step has runnable code, every test step has real assertions, every run step has an exact command and expected output. Task 14's steps 2–5 are judgment calls against a live install by necessity — the decision procedure is enumerated rather than left open.

**Type consistency.** `KindResult` fields (`kind`, `tf_type`, `status`, `records`, `reason`, `bucket`) are identical in Tasks 7–11. `Attr`/`NestedType`/`ResourceSchema` from Task 2 are used unchanged in Tasks 3, 4, 12. `LabelAllocator.index` is keyed `(tf_type, str(source_id))` in Task 5 and read that way in Task 6. `sidecar` returns `__ref__`-prefixed keys in Task 6, stored under `refs` in Task 11, stripped in Task 12. `build_state(document, schemas, ...)` takes schemas in both its definition (Task 12) and its call site (Task 13).

**Known ordering constraint.** Task 9's test imports `RouteClient` from `tests/test_tfstate_readers.py`, so Task 8 must land first. Tasks 2–6 are otherwise independent of each other and can be parallelized; 7 → 8 → 9 → 10 → 11 → 12 → 13 → 14 is strictly sequential.
