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
