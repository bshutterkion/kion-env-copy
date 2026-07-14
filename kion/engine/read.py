"""Shared list-endpoint reader for the metadata-driven engine.

Both ``kion.engine.inventory`` (reading the *source* install) and
``kion.engine.reconcile._index_target`` (indexing the *target* install) need to
GET a resource's list endpoint and unwrap the result. Two response shapes exist
(verified live — see CLAUDE.md):

  * a bare list — most ``/v3/*`` endpoints; a single request is the full list.
  * an ``{items|data, total}`` envelope — the paginated endpoints (``/beta/scope``,
    ``/v4/billing-source``). Only these carry a ``total``; we page until we've
    collected ``total`` records or hit an empty page (mirrors
    ``export._export_scopes`` / ``import_._list_scopes``).

Factored here so the two call sites can't drift on either the unwrap or the
paging. The first request is deliberately param-free: the bare-list call sites
pass no params today, and the paginated endpoints still return their first page
(and ``total``) without one.
"""
from __future__ import annotations

from kion.client import KionAPIError


def _unwrap(resp):
    """Records out of a bare-list or ``{items|data}`` envelope response."""
    if isinstance(resp, dict):
        return (resp.get("items") if "items" in resp else resp.get("data")) or []
    return resp or []


def list_records(client, path: str) -> list[dict]:
    """GET ``path``'s list, unwrapping and paginating as needed. Returns [] on
    an API error (the caller's list simply comes back empty, as the hand-written
    export/import readers also degrade)."""
    try:
        resp = client.get(path)
    except KionAPIError:
        return []
    # A bare list, or an envelope without a 'total', is the whole result: no paging.
    if not isinstance(resp, dict) or "total" not in resp:
        return _unwrap(resp)

    items = list(_unwrap(resp))
    total = resp.get("total", len(items))
    count = len(items) or 100  # keep page size consistent with page 1
    page = 1
    while items and len(items) < total:
        page += 1
        try:
            resp = client.get(path, params={"page": page, "count": count})
        except KionAPIError:
            break
        batch = _unwrap(resp)
        if not batch:
            break
        items.extend(batch)
    return items
