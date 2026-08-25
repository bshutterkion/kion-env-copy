from __future__ import annotations
import os
from dataclasses import dataclass, field
import yaml

_VENDOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor")

@dataclass
class Reference:
    field: str
    target: str
    key: str
    many: bool = False
    optional: bool = False

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

# Read/create overlays for resources the VENDORED generator_config.yaml
# describes wrongly or not at all — supplied here so we never edit the vendored
# files. ``account`` gained a vendored entry at provider 89a9195, but codegen
# records its by-id read (``/v3/account/{id}``) and an aws-only cache create;
# the engine's records are a union of /v3/account + /v3/account-cache assembled
# in kion.engine.inventory._read_accounts, so the list read_path is pinned here.
# The vendored create_path is loaded but never reached: account creates go
# through the ``build_create_payload`` hook
# (kion.overrides.registry._account_payload), which routes to the project or
# account-cache endpoint per record.
READ_OVERRIDES: dict[str, dict] = {
    "account": {
        "read_path": "/v3/account", "read_method": "GET",
    },
}

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

    # Merge Python-side read/create overrides (see READ_OVERRIDES). A resource
    # absent from the vendored config is created here; one already present (e.g.
    # account) has only the supplied fields overlaid.
    for name, ov in READ_OVERRIDES.items():
        m = out.get(name) or ResourceMeta(name=name)
        for attr in ("read_path", "read_method", "create_path", "create_method"):
            if ov.get(attr) is not None:
                setattr(m, attr, ov[attr])
        out[name] = m
    return out

def load_references(path: str | None = None) -> dict[str, list[Reference]]:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "references.yaml")
    raw = _yaml(os.path.dirname(path), os.path.basename(path))
    return {res: [Reference(**r) for r in lst] for res, lst in raw.items()}

def load_natural_keys(path: str | None = None) -> dict[str, dict]:
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "natural_keys.yaml")
    return _yaml(os.path.dirname(path), os.path.basename(path))
