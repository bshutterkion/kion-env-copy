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
