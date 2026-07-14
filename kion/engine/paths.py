"""Shared list-endpoint derivation for the metadata-driven engine.

Both ``kion.engine.reconcile`` (indexing the *target* install before
reconciling) and ``kion.engine.inventory`` (reading the *source* install) need
to turn a resource's ``read_path`` (its per-item GET, e.g. ``/v3/ou/{id}``)
into the LIST endpoint for that resource (e.g. ``/v3/ou``). Factored here so
the two call sites can't drift apart.
"""
from __future__ import annotations


def list_path(read_path: str | None) -> str | None:
    """Strip a trailing ``/{...}`` id template off ``read_path`` to get the
    list endpoint. Resources whose ``read_path`` is already a list endpoint
    (e.g. ``billing_source``'s ``/v4/billing-source``) are returned as-is."""
    if not read_path:
        return read_path
    return read_path.split("/{")[0]
