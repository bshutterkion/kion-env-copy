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
