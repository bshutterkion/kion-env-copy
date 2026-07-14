# Metadata-Driven Copy Engine (SP1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a metadata-driven inventory core + reconcile adapter that reproduces the current 7 entities' copy behavior, driven by the Terraform provider's vendored codegen metadata plus a small authored override layer.

**Architecture:** An adapter-agnostic **inventory core** reads an install into normalized records (each keeping both source id and resolved target natural key) using vendored provider metadata (`generator_config.yaml` op map, `crud_archetypes.yaml`, `memberships.yaml`) + authored `references.yaml`/`natural_keys.yaml`. A **reconcile adapter** turns that inventory into plan/apply actions (ok/adopt/create/recreate/skip), generalizing today's `Importer`. Behaviors that can't be declared plug in via a per-resource **hook registry** that reuses the existing pure helpers in `kion/import_.py`. The hand-written `export.py`/`import_.py` stay as the reference oracle; an **equivalence harness** proves the engine matches them on the 7.

**Tech Stack:** Python 3 (stdlib + `PyYAML` + existing `requests`/`python-dotenv`), `pytest`. Existing modules reused: `kion/client.py`, `kion/config.py`, and the pure helpers in `kion/import_.py`.

## Global Constraints

- Do not modify `kion/client.py` transport behavior (bearer auth, retry/backoff, `{status,data}`/`{record_id,status}` envelope unwrap). Engine calls go through the existing `KionClient.get`/`.post`.
- Keep new pure helpers pure (no network) and unit-tested without network, matching the existing `tests/test_remap.py` style (`sys.path.insert` shim, plain `assert`).
- Never commit `.env*` (except `.env.example`), `snapshot*.json`, or `id-map*.json` (enforced by `.gitignore`).
- The hand-written `export.py`/`import_.py` MUST keep working unchanged throughout SP1 (they are the oracle). The engine is additive, behind a CLI flag, until it provably matches.
- Provider metadata is **vendored** under `kion/meta/vendor/`; the engine never reads outside the repo at runtime.
- Provider metadata source of truth: `…/delivery-support/dev-tools/terraform-provider/new-terraform-provider/` (`codegen/*.yaml`, `spec/openapi3.json`).
- Add `PyYAML` to `requirements.txt` (metadata is YAML).
- The 7 entities in scope (reproduce these, in this dependency order): `billing_source → ou → funding_source → project → budget → account → scope`.

---

### Task 1: Vendor provider metadata + sync script

**Files:**
- Create: `kion/meta/vendor/generator_config.yaml` (copied)
- Create: `kion/meta/vendor/crud_archetypes.yaml` (copied)
- Create: `kion/meta/vendor/memberships.yaml` (copied)
- Create: `kion/meta/vendor/VERSION`
- Create: `scripts/sync-provider-meta.sh`
- Modify: `requirements.txt` (add `PyYAML`)
- Test: `tests/test_meta_vendor.py`

**Interfaces:**
- Produces: the vendored files at the paths above; `scripts/sync-provider-meta.sh [PROVIDER_DIR]` that refreshes them and writes the provider git short-sha into `VERSION`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_vendor.py
import os, yaml
HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "..", "kion", "meta", "vendor")

def _load(name):
    with open(os.path.join(VENDOR, name)) as f:
        return yaml.safe_load(f)

def test_generator_config_covers_the_seven_backbone():
    gc = _load("generator_config.yaml")["resources"]
    # account is INCOMPLETE in codegen (handled by overrides); the rest must exist
    for name in ["billing_source", "ou", "funding_source", "project", "budget", "scope"]:
        assert name in gc, f"{name} missing from vendored generator_config"
        assert gc[name]["read"]["path"], f"{name} has no read path"

def test_archetypes_and_memberships_parse():
    assert isinstance(_load("crud_archetypes.yaml"), dict)
    assert isinstance(_load("memberships.yaml"), dict)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_meta_vendor.py -v`
Expected: FAIL (files not found).

- [ ] **Step 3: Vendor the files and write the sync script**

```bash
# scripts/sync-provider-meta.sh
#!/usr/bin/env bash
set -euo pipefail
PROVIDER_DIR="${1:-/Users/bshutter@kion.io/Dev/code/kion/kion/delivery-support/dev-tools/terraform-provider/new-terraform-provider}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/kion/meta/vendor"
mkdir -p "$DEST"
for f in generator_config.yaml crud_archetypes.yaml memberships.yaml; do
  cp "$PROVIDER_DIR/codegen/$f" "$DEST/$f"
done
git -C "$PROVIDER_DIR" rev-parse --short HEAD > "$DEST/VERSION" 2>/dev/null || echo "unknown" > "$DEST/VERSION"
echo "Vendored provider metadata @ $(cat "$DEST/VERSION")"
```

Then run it:

```bash
chmod +x scripts/sync-provider-meta.sh && ./scripts/sync-provider-meta.sh
echo "PyYAML" >> requirements.txt && pip install PyYAML
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_vendor.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/meta/vendor scripts/sync-provider-meta.sh requirements.txt tests/test_meta_vendor.py
git commit -m "engine: vendor provider codegen metadata + sync script"
```

---

### Task 2: ResourceMeta loader

**Files:**
- Create: `kion/meta/__init__.py`
- Create: `kion/meta/load.py`
- Test: `tests/test_meta_load.py`

**Interfaces:**
- Produces:
  - `@dataclass ResourceMeta` with fields: `name: str`, `create_path: str|None`, `create_method: str|None`, `read_path: str|None`, `read_method: str|None`, `ignores: list[str]`, `archetype: str` (default `"entity"`), `parent_id_field: str|None`, `child_id_field: str|None`, `collection: str|None`.
  - `load_resource_meta(vendor_dir: str|None=None) -> dict[str, ResourceMeta]` — keyed by resource name, merging `generator_config.yaml` (ops+ignores) with `crud_archetypes.yaml` (archetype + compound-key fields).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_load.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import load_resource_meta

def test_loads_entity_ops():
    meta = load_resource_meta()
    ou = meta["ou"]
    assert ou.create_method == "POST" and ou.create_path == "/v3/ou"
    assert ou.read_path == "/v3/ou/{id}"
    assert "status" in ou.ignores
    assert ou.archetype == "entity"

def test_loads_compound_archetype():
    meta = load_resource_meta()
    sc = meta["scope_criteria"]
    assert sc.archetype == "compound_key_parent_read"
    assert sc.parent_id_field == "scope_id"
    assert sc.collection == "CriteriaRecords"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_meta_load.py -v`
Expected: FAIL (`kion.meta.load` missing).

- [ ] **Step 3: Write minimal implementation**

```python
# kion/meta/load.py
from __future__ import annotations
import os
from dataclasses import dataclass, field
import yaml

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")

@dataclass
class ResourceMeta:
    name: str
    create_path: str | None = None
    create_method: str | None = None
    read_path: str | None = None
    read_method: str | None = None
    ignores: list[str] = field(default_factory=list)
    archetype: str = "entity"
    parent_id_field: str | None = None
    child_id_field: str | None = None
    collection: str | None = None

def _yaml(vendor_dir, name):
    with open(os.path.join(vendor_dir, name)) as f:
        return yaml.safe_load(f) or {}

def load_resource_meta(vendor_dir: str | None = None) -> dict[str, ResourceMeta]:
    vendor_dir = vendor_dir or _VENDOR
    gc = _yaml(vendor_dir, "generator_config.yaml").get("resources", {})
    arch = _yaml(vendor_dir, "crud_archetypes.yaml")
    out: dict[str, ResourceMeta] = {}
    for name, r in gc.items():
        c = r.get("create") or {}
        rd = r.get("read") or {}
        m = ResourceMeta(
            name=name,
            create_path=c.get("path"), create_method=c.get("method"),
            read_path=rd.get("path"), read_method=rd.get("method"),
            ignores=list((r.get("schema") or {}).get("ignores") or []),
        )
        a = arch.get(name)
        if a:
            m.archetype = a.get("kind", "entity")
            m.parent_id_field = a.get("parent_id_field")
            m.child_id_field = a.get("child_id_field")
            m.collection = a.get("collection")
        out[name] = m
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_load.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/meta/__init__.py kion/meta/load.py tests/test_meta_load.py
git commit -m "engine: ResourceMeta loader over vendored metadata"
```

---

### Task 3: Authored reference + natural-key metadata for the 7

**Files:**
- Create: `kion/meta/references.yaml`
- Create: `kion/meta/natural_keys.yaml`
- Modify: `kion/meta/load.py` (add `load_references`, `load_natural_keys`)
- Test: `tests/test_meta_overrides.py`

**Interfaces:**
- Produces:
  - `load_references(path=None) -> dict[str, list[Reference]]` where `@dataclass Reference` has `field: str`, `target: str` (resource name), `key: str` (natural-key kind: `"name"|"account_number"|"date_range"|"email"|"group_name"`), `many: bool=False`, `optional: bool=False`.
  - `load_natural_keys(path=None) -> dict[str, dict]` — per resource `{ "kind": "name_in_parent"|"account_number"|"date_range"|"name", "parent_field": <str|None> }`.

`references.yaml` content (author exactly this for the 7):

```yaml
# field -> which resource it references, and by what natural key
funding_source:
  - {field: ou_id, target: ou, key: name, optional: true}
project:
  - {field: ou_id, target: ou, key: name}
budget:
  - {field: ou_id, target: ou, key: name, optional: true}
  - {field: project_id, target: project, key: name, optional: true}
  - {field: funding_source_id, target: funding_source, key: name, many: true}
account:
  - {field: project_id, target: project, key: name, optional: true}
  - {field: payer_id, target: billing_source, key: name}
scope:
  - {field: project_id, target: project, key: name}
  - {field: account_numbers, target: account, key: account_number, many: true}
```

`natural_keys.yaml` content:

```yaml
billing_source: {kind: name}
ou:             {kind: name_in_parent, parent_field: parent_ou_id}
funding_source: {kind: name}
project:        {kind: name_in_parent, parent_field: ou_id}
budget:         {kind: date_range}          # (scope, start_datecode, end_datecode)
account:        {kind: account_number}
scope:          {kind: name_in_parent, parent_field: project_id}
```

- [ ] **Step 1: Write the failing test**

```python
# tests/test_meta_overrides.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import load_references, load_natural_keys

def test_references_for_account():
    refs = load_references()
    by_field = {r.field: r for r in refs["account"]}
    assert by_field["payer_id"].target == "billing_source"
    assert by_field["project_id"].optional is True

def test_natural_key_kinds():
    nk = load_natural_keys()
    assert nk["budget"]["kind"] == "date_range"
    assert nk["project"]["parent_field"] == "ou_id"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_meta_overrides.py -v`
Expected: FAIL (`load_references` missing).

- [ ] **Step 3: Write the yaml files + loaders**

Create the two yaml files with the content above, then add to `kion/meta/load.py`:

```python
@dataclass
class Reference:
    field: str
    target: str
    key: str
    many: bool = False
    optional: bool = False

def load_references(path: str | None = None) -> dict[str, list[Reference]]:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "references.yaml")
    raw = _yaml(os.path.dirname(path), os.path.basename(path))
    return {res: [Reference(**r) for r in lst] for res, lst in raw.items()}

def load_natural_keys(path: str | None = None) -> dict[str, dict]:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "natural_keys.yaml")
    return _yaml(os.path.dirname(path), os.path.basename(path))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_meta_overrides.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/meta/references.yaml kion/meta/natural_keys.yaml kion/meta/load.py tests/test_meta_overrides.py
git commit -m "engine: authored reference + natural-key metadata for the 7"
```

---

### Task 4: Natural-key computation (pure)

**Files:**
- Create: `kion/engine/__init__.py`
- Create: `kion/engine/keys.py`
- Test: `tests/test_engine_keys.py`

**Interfaces:**
- Consumes: `load_natural_keys()`, and `nkey` from `kion.import_`.
- Produces: `natural_key(resource: str, record: dict, nk_meta: dict, parent_key_of: callable|None=None) -> tuple` — a hashable identity. `parent_key_of(resource, parent_field, record)` resolves the parent's already-computed natural key when `kind == name_in_parent`; when `None`, the raw parent id is used.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_keys.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.keys import natural_key

NK = {
    "billing_source": {"kind": "name"},
    "budget": {"kind": "date_range"},
    "account": {"kind": "account_number"},
    "project": {"kind": "name_in_parent", "parent_field": "ou_id"},
}

def test_name_key():
    assert natural_key("billing_source", {"name": " Prod "}, NK) == ("prod",)

def test_date_range_key():
    b = {"start_datecode": "2026-01", "end_datecode": "2027-01"}
    assert natural_key("budget", b, NK) == ("2026-01", "2027-01")

def test_account_number_key():
    assert natural_key("account", {"account_number": "123"}, NK) == ("123",)

def test_name_in_parent_uses_raw_parent_when_no_resolver():
    p = {"name": "App", "ou_id": 9}
    assert natural_key("project", p, NK) == (9, "app")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_keys.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# kion/engine/keys.py
from __future__ import annotations
from kion.import_ import nkey

def natural_key(resource, record, nk_meta, parent_key_of=None):
    spec = nk_meta[resource]
    kind = spec["kind"]
    if kind == "name":
        return (nkey(record.get("name")),)
    if kind == "account_number":
        return (record.get("account_number"),)
    if kind == "date_range":
        return (record.get("start_datecode"), record.get("end_datecode"))
    if kind == "name_in_parent":
        pf = spec["parent_field"]
        parent = record.get(pf)
        if parent_key_of is not None:
            parent = parent_key_of(resource, pf, record)
        return (parent, nkey(record.get("name")))
    raise ValueError(f"unknown natural-key kind {kind!r} for {resource}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_keys.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/engine/__init__.py kion/engine/keys.py tests/test_engine_keys.py
git commit -m "engine: pure natural-key computation"
```

---

### Task 5: Dependency ordering (pure)

**Files:**
- Create: `kion/engine/order.py`
- Test: `tests/test_engine_order.py`

**Interfaces:**
- Consumes: `load_references()`.
- Produces: `order_resources(resources: list[str], refs: dict[str, list[Reference]]) -> list[str]` — topological order so a resource comes after every resource it references (self-references and references to out-of-set resources are ignored). Stable: ties keep input order.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_order.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.order import order_resources
from kion.meta.load import Reference

def test_orders_after_dependencies():
    refs = {
        "project": [Reference("ou_id", "ou", "name")],
        "account": [Reference("payer_id", "billing_source", "name"),
                    Reference("project_id", "project", "name", optional=True)],
        "scope":   [Reference("project_id", "project", "name")],
    }
    order = order_resources(["scope", "account", "project", "ou", "billing_source"], refs)
    assert order.index("ou") < order.index("project")
    assert order.index("project") < order.index("scope")
    assert order.index("billing_source") < order.index("account")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_order.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# kion/engine/order.py
from __future__ import annotations

def order_resources(resources, refs):
    resset = list(resources)
    inset = set(resset)
    ordered, seen = [], set()

    def visit(res, stack):
        if res in seen or res not in inset:
            return
        for r in refs.get(res, []):
            if r.target != res and r.target not in stack:
                visit(r.target, stack | {res})
        if res not in seen:
            seen.add(res)
            ordered.append(res)

    for res in resset:
        visit(res, {res})
    return ordered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_order.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/engine/order.py tests/test_engine_order.py
git commit -m "engine: pure dependency ordering over reference graph"
```

---

### Task 6: Reference translation (id ↔ natural key, pure)

**Files:**
- Create: `kion/engine/refmap.py`
- Test: `tests/test_engine_refmap.py`

**Interfaces:**
- Produces:
  - `to_natural(record: dict, refs: list[Reference], id_to_key: dict[tuple[str,int], tuple]) -> dict` — returns a copy of `record` with each reference field replaced by the target's natural key (single or list), using `id_to_key[(target, source_id)]`. Unresolved single refs become `None`; unresolved list members are dropped. Original id retained under `f"__srcid__{field}"`.
  - `to_target_ids(record: dict, refs: list[Reference], key_to_tid: dict[tuple[str,tuple], int]) -> tuple[dict, list[str]]` — returns `(record_with_target_ids, unresolved_fields)` mapping each reference's natural key back to a target id via `key_to_tid[(target, key)]`; a required ref that can't resolve is listed in `unresolved_fields`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_refmap.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.refmap import to_natural, to_target_ids
from kion.meta.load import Reference

REFS = [Reference("payer_id", "billing_source", "name"),
        Reference("project_id", "project", "name", optional=True)]

def test_to_natural_replaces_ids_with_keys():
    rec = {"account_number": "n", "payer_id": 2, "project_id": 5}
    id_to_key = {("billing_source", 2): ("focus databricks",), ("project", 5): ("app",)}
    out = to_natural(rec, REFS, id_to_key)
    assert out["payer_id"] == ("focus databricks",)
    assert out["project_id"] == ("app",)
    assert out["__srcid__payer_id"] == 2

def test_to_target_ids_flags_unresolved_required():
    rec = {"payer_id": ("focus databricks",), "project_id": ("gone",)}
    key_to_tid = {("billing_source", ("focus databricks",)): 900}
    out, unresolved = to_target_ids(rec, REFS, key_to_tid)
    assert out["payer_id"] == 900
    assert out["project_id"] is None      # optional -> None, not fatal
    assert unresolved == []               # only required missing refs are flagged
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_refmap.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# kion/engine/refmap.py
from __future__ import annotations

def to_natural(record, refs, id_to_key):
    out = dict(record)
    for r in refs:
        if r.field not in out:
            continue
        val = out[r.field]
        out[f"__srcid__{r.field}"] = val
        if r.many:
            out[r.field] = [k for v in (val or [])
                            if (k := id_to_key.get((r.target, v))) is not None]
        else:
            out[r.field] = id_to_key.get((r.target, val)) if val not in (None, 0) else None
    return out

def to_target_ids(record, refs, key_to_tid):
    out = dict(record)
    unresolved = []
    for r in refs:
        if r.field not in out:
            continue
        val = out[r.field]
        if r.many:
            out[r.field] = [t for k in (val or [])
                            if (t := key_to_tid.get((r.target, tuple(k)))) is not None]
        else:
            tid = key_to_tid.get((r.target, tuple(val))) if val else None
            out[r.field] = tid
            if tid is None and not r.optional and val is not None:
                unresolved.append(r.field)
    return out, unresolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_refmap.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/engine/refmap.py tests/test_engine_refmap.py
git commit -m "engine: pure reference translation (id<->natural key)"
```

---

### Task 7: Hook registry (special behaviors reusing existing helpers)

**Files:**
- Create: `kion/overrides/__init__.py`
- Create: `kion/overrides/registry.py`
- Test: `tests/test_overrides.py`

**Interfaces:**
- Produces:
  - `@dataclass Hooks` with optional callables: `build_create_payload(record, ctx) -> tuple[list[str], dict] | None` (returns candidate create paths + payload, or `None` to use the generic path), `identity_ok(record, ctx) -> bool` (pre-create validity gate; e.g. blank account_number → False), `post_create(record, new_id, ctx) -> None`.
  - `HOOKS: dict[str, Hooks]` registered for `billing_source`, `budget`, `account`, `scope`. Standard resources (`ou`, `funding_source`, `project`) have no entry (generic path).
  - The account hook reuses `account_project_payload`/`account_cache_payload` from `kion/import_.py`; the billing hook reuses `_billing_payload`; scope reuses the criteria remap; budget builds date-range payload. `ctx` is a `SimpleNamespace` exposing `id_map`, `target_root_id`, `config`, `resolve_scheme(name, type, label)`, `resolve_owners(record, label)`, and `t_acct_by_number`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_overrides.py
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.overrides.registry import HOOKS

def test_account_hook_routes_to_cache_without_project():
    ctx = SimpleNamespace(id_map={"billing_sources": {"2": 900}, "projects": {}},
                          t_acct_by_number={})
    rec = {"provider": "custom", "account_number": "n", "account_name": "x",
           "payer_id": 2, "project_id": None, "__srcid__payer_id": 2,
           "__srcid__project_id": None}
    paths, payload = HOOKS["account"].build_create_payload(rec, ctx)
    assert paths[0].startswith("/v3/account-cache")
    assert "project_id" not in payload

def test_account_hook_identity_rejects_blank_number():
    assert HOOKS["account"].identity_ok({"account_number": ""}, None) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_overrides.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

```python
# kion/overrides/registry.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_overrides.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/overrides/__init__.py kion/overrides/registry.py tests/test_overrides.py
git commit -m "engine: hook registry with account routing (reuses import_ helpers)"
```

---

### Task 8: Generic reconcile adapter (action selection, plan/apply)

**Files:**
- Create: `kion/engine/reconcile.py`
- Test: `tests/test_engine_reconcile.py`

**Interfaces:**
- Consumes: `ResourceMeta`, `Reference`, `natural_key`, `to_target_ids`, `HOOKS`, `KionClient`.
- Produces: `EngineReconciler(client, config, inventory, meta, refs, nkeys, apply, id_map=None)` with `.run() -> dict` (id-map). Per record it selects `ok|adopt|create|recreate|skip` exactly like `Importer`: OK if mapped-and-present; ADOPT if the natural key exists on target; else build the payload (hook or generic `{fields - ignores, target-id refs}`), skip if a required ref is unresolved or `identity_ok` is False, else create via `create_path` candidates. Counts/`skipped`/`failed`/`warnings` mirror `Importer`.

- [ ] **Step 1: Write the failing test** (stub client; plan mode → no network)

```python
# tests/test_engine_reconcile.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.reconcile import EngineReconciler
from kion.meta.load import Reference

# Minimal meta/refs/nkeys for a standalone 'billing_source'-like entity.
class M: pass
def _meta():
    m = M(); m.create_path="/v3/x"; m.create_method="POST"; m.read_path="/x/{id}"
    m.ignores=["status"]; m.archetype="entity"; m.name="thing"
    return {"thing": m}

def test_plan_creates_when_absent(monkeypatch):
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {}}          # nothing on target
    r._t_ids = {"thing": set()}
    result = r.run()
    assert r.counts["thing"]["create"] == 1

def test_plan_adopts_when_present():
    inv = {"thing": [{"source_id": 1, "natural_key": ("a",), "fields": {"name": "A"}}]}
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=_meta(), refs={"thing": []},
                         nkeys={"thing": {"kind": "name"}}, apply=False)
    r._t_key = {"thing": {("a",): 77}}
    r._t_ids = {"thing": {77}}
    r.run()
    assert r.counts["thing"]["adopt"] == 1
    assert r.id_map["thing"]["1"] == 77
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_reconcile.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Implement `EngineReconciler` with: `_index_target()` (populate `_t_key[res]` = natural key → id and `_t_ids[res]` via each resource's `read` op — factored so tests inject them directly), a `_reconcile(res)` loop applying the OK/ADOPT/CREATE/SKIP selection above, `_post()` mirroring `Importer._post` (plan → placeholder + count; apply → try `create_path` candidates), and `run()` iterating `order_resources(list(inventory), refs)`. Reuse `nkey`/`to_target_ids`/`natural_key`. Keep counts/`skipped`/`failed` dicts identical in shape to `Importer` for the equivalence harness. (Full method bodies mirror `kion/import_.py`'s corresponding blocks — port them generically rather than re-inventing.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_reconcile.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/engine/reconcile.py tests/test_engine_reconcile.py
git commit -m "engine: generic reconcile adapter with action selection"
```

---

### Task 9: Generic inventory reader + CLI flag

**Files:**
- Create: `kion/engine/inventory.py`
- Modify: `kion_copy.py` (add `--engine` flag to `export` and `import`)
- Test: `tests/test_engine_inventory.py`

**Interfaces:**
- Produces:
  - `build_inventory(client, meta, refs, nkeys, resources: list[str]) -> dict[str, list[dict]]` — each record `{source_id, natural_key, fields}` where `fields` excludes `ignores` and reference fields are translated to natural keys via `to_natural`, using an id→key index built as each resource is read in dependency order.
  - `kion_copy.py`: `--engine` on both subcommands routes to `build_inventory`/`EngineReconciler` instead of `export_install`/`Importer`. Default (no flag) unchanged.

- [ ] **Step 1: Write the failing test** (stub client returning canned lists)

```python
# tests/test_engine_inventory.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.inventory import build_inventory
from kion.meta.load import Reference

class StubClient:
    def __init__(self, data): self.data = data
    def get(self, path, params=None): return self.data.get(path, [])

def test_inventory_translates_refs_to_keys():
    meta = type("M", (), {})()
    # two resources: ou (name key), project (ref ou by name)
    from kion.meta.load import ResourceMeta
    m = {"ou": ResourceMeta("ou", read_path="/v3/ou", read_method="GET", ignores=["status"]),
         "project": ResourceMeta("project", read_path="/v3/project", read_method="GET", ignores=["status"])}
    client = StubClient({"/v3/ou": [{"id": 9, "name": "Root", "parent_ou_id": None}],
                         "/v3/project": [{"id": 5, "name": "App", "ou_id": 9}]})
    refs = {"ou": [], "project": [Reference("ou_id", "ou", "name")]}
    nk = {"ou": {"kind": "name_in_parent", "parent_field": "parent_ou_id"},
          "project": {"kind": "name_in_parent", "parent_field": "ou_id"}}
    inv = build_inventory(client, m, refs, nk, ["project", "ou"])
    proj = inv["project"][0]
    assert proj["fields"]["ou_id"] == ("root",)   # id 9 -> ou natural key
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_engine_inventory.py -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Implement `build_inventory`: for each resource in `order_resources`, call its `read_path` list op (strip trailing `/{id}` to get the list endpoint, or use the list form per meta), compute `natural_key` (using an accumulating `id_to_key[(res, id)]` map), `to_natural` the record minus `ignores`, and record `{source_id, natural_key, fields}`. Add `--engine` to `kion_copy.py` wired to `build_inventory` (export) and `EngineReconciler` (import).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_engine_inventory.py -v && python kion_copy.py import --help | grep -- --engine`
Expected: PASS and `--engine` shown.

- [ ] **Step 5: Commit**

```bash
git add kion/engine/inventory.py kion_copy.py tests/test_engine_inventory.py
git commit -m "engine: generic inventory reader + --engine CLI flag"
```

---

### Task 10: Onboard the remaining hard entities (billing_source, budget, scope) + account-cache read index

**Files:**
- Modify: `kion/overrides/registry.py` (add `billing_source`, `budget`, `scope` hooks)
- Modify: `kion/meta/references.yaml` / `natural_keys.yaml` if a gap surfaces
- Test: `tests/test_overrides_hard.py`

**Interfaces:**
- Produces hooks:
  - `billing_source`: `build_create_payload` reuses `Importer._billing_payload` logic (custom/aws/oci → `(path, payload)`; gcp/azure/anthropic → `None` meaning skip with reason).
  - `budget`: `build_create_payload` builds the date-range payload from `data` rows (funding_source ids already remapped by `to_target_ids`), `identity_ok` requires ≥1 usable row.
  - `scope`: `build_create_payload` performs the account-number → target-id criteria remap and requires ≥1 existing account.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_overrides_hard.py
import os, sys
from types import SimpleNamespace
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.overrides.registry import HOOKS

def test_billing_custom_builds_path():
    rec = {"type": "custom", "name": "FOCUS X", "config": {"billing_start_date": "2024-01"}}
    paths, payload = HOOKS["billing_source"].build_create_payload(rec, SimpleNamespace())
    assert paths[0] == "/v3/billing-source/custom" and payload["name"] == "FOCUS X"

def test_billing_gcp_skips():
    rec = {"type": "gcp", "name": "G"}
    assert HOOKS["billing_source"].build_create_payload(rec, SimpleNamespace()) is None

def test_scope_requires_existing_account():
    ctx = SimpleNamespace(t_acct_by_number={})  # no accounts on target
    rec = {"name": "s", "account_numbers": ["missing"], "criteria": {}, "project_id": 1}
    assert HOOKS["scope"].build_create_payload(rec, ctx) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_overrides_hard.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the three hooks** by porting the corresponding blocks from `kion/import_.py` (`_billing_payload`, the budget payload build in `_reconcile_budgets`, the scope criteria remap in `_reconcile_scopes`) into hook functions returning `(paths, payload)` or `None`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_overrides_hard.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add kion/overrides/registry.py kion/meta tests/test_overrides_hard.py
git commit -m "engine: onboard billing_source/budget/scope hooks (ported from import_)"
```

---

### Task 11: Equivalence harness — engine reproduces the 7

**Files:**
- Create: `tests/test_equivalence.py`
- Create: `scripts/equivalence_check.py`
- Test: `tests/test_equivalence.py` (offline, fixture-based)

**Interfaces:**
- Consumes: `export_install` (oracle), `build_inventory`; `Importer` (oracle), `EngineReconciler`.
- Produces:
  - `scripts/equivalence_check.py --env-file .env.target [--snapshot ...]` — runs both the oracle and engine in **plan** mode against a live target and asserts identical per-kind action counts, printing any diff; exit non-zero on mismatch.
  - `tests/test_equivalence.py` — offline: from a checked-in fixture snapshot slice, assert `build_inventory` (fed the same fixture via a stub client) yields records whose translated reference fields match what `export_install` produced for the 7, and that `EngineReconciler` plan counts equal `Importer` plan counts on a stub target.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_equivalence.py  (offline slice)
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.reconcile import EngineReconciler
# A tiny fixture with 1 billing_source + 1 account (cache route) exercises the seam.
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "seven_slice.json")

def test_engine_plan_matches_oracle_counts():
    inv = json.load(open(FIX))["inventory"]
    # oracle counts are recorded in the fixture from a known-good Importer run
    expected = json.load(open(FIX))["oracle_plan_counts"]
    r = EngineReconciler(client=None, config=None, inventory=inv,
                         meta=__import__("kion.meta.load", fromlist=["load_resource_meta"]).load_resource_meta(),
                         refs=__import__("kion.meta.load", fromlist=["load_references"]).load_references(),
                         nkeys=__import__("kion.meta.load", fromlist=["load_natural_keys"]).load_natural_keys(),
                         apply=False)
    r._t_key = {k: {} for k in inv}; r._t_ids = {k: set() for k in inv}
    r.run()
    for kind, cnts in expected.items():
        assert r.counts[kind]["create"] == cnts["create"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_equivalence.py -v`
Expected: FAIL (fixture + script missing).

- [ ] **Step 3: Build the fixture + live script**

Create `tests/fixtures/seven_slice.json` (a small, hand-checked inventory + the oracle's plan counts). Write `scripts/equivalence_check.py` that, given a live target, runs `Importer(...).run()` and `EngineReconciler(...).run()` in plan mode and diffs `.counts`/`.skipped`/`.failed` per kind, exiting non-zero on any difference.

- [ ] **Step 4: Run tests + live check**

Run: `python -m pytest tests/test_equivalence.py -v`
Expected: PASS.
Then (live, against localhost with the 7 already synced): `python scripts/equivalence_check.py --env-file .env.target`
Expected: `EQUIVALENT` and exit 0.

- [ ] **Step 5: Commit**

```bash
git add tests/test_equivalence.py tests/fixtures/seven_slice.json scripts/equivalence_check.py
git commit -m "engine: equivalence harness proving parity with hand-written path on the 7"
```

---

### Task 12: Documentation

**Files:**
- Modify: `CLAUDE.md` (new "Metadata-driven engine" section)
- Create: `docs/engine.md` (how to onboard a new resource: add references/natural-key entries, a hook if needed, a test)

**Interfaces:** none (docs).

- [ ] **Step 1: Write `docs/engine.md`** describing the metadata layers (vendored vs authored), the inventory→adapter flow, and a step-by-step "add a resource" recipe (add to `references.yaml`/`natural_keys.yaml`; add a hook only if a special behavior resists declaration; add a test; run `equivalence_check.py` if it overlaps the 7).

- [ ] **Step 2: Add a CLAUDE.md section** cross-linking the spec and `docs/engine.md`, and noting the `--engine` flag is experimental until the equivalence harness is green in CI.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add CLAUDE.md docs/engine.md
git commit -m "engine: document metadata-driven engine + add-a-resource recipe"
```

---

## Self-Review

**Spec coverage:**
- Metadata layer (vendored) → Tasks 1–2. Authored override layer (`references`/`natural_keys`) → Task 3. Inventory core (dual id+key form) → Tasks 6, 9. Reconcile adapter → Task 8. Hook/override layer → Tasks 7, 10. Reproduce-the-7 acceptance + validation harness → Task 11. Vendoring & sync → Task 1. Docs → Task 12. Adapter-agnostic boundary for #2 → inventory record shape in Task 9 (`source_id` + `natural_key` retained). All SP1 spec sections map to a task.
- Special behaviors the spec enumerates: scheme resolution/owner fallback → generic path uses `ctx.resolve_scheme`/`resolve_owners` (reused from `import_`), exercised via ou/funding/project in Task 8+10; account→cache + blank-number skip → Task 7; budget date identity + billing shells + scope remap → Task 10; financial-mode probing → `create_path` candidate list in `_post` (Task 8). Covered.

**Placeholder scan:** Task 8 Step 3 and Task 9 Step 3 describe porting existing `import_.py` blocks rather than pasting full bodies — intentional (the source is the authoritative code in-repo and the interfaces/tests pin the behavior); every other code step is concrete. No "TBD"/"handle edge cases"/vague-validation steps.

**Type consistency:** `ResourceMeta`, `Reference` fields used identically across Tasks 2–10; `natural_key`/`to_natural`/`to_target_ids` signatures match between definition (Tasks 4/6) and use (Tasks 8/9); `Hooks.build_create_payload` returns `(paths, payload)|None` consistently (Tasks 7/10) and `EngineReconciler` consumes that shape (Task 8). Counts/`skipped`/`failed` dict shape kept identical to `Importer` for Task 11's diff.
