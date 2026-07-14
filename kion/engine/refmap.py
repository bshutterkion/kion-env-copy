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
