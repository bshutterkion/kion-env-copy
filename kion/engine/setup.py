"""Shared engine bootstrap.

``kion_copy.py`` (the CLI) and ``scripts/equivalence_check.py`` (the live gate)
both need the exact same metadata bundle to drive the generic engine. This is
the single source of truth so the two can't drift.
"""
from __future__ import annotations

from kion.meta.load import load_natural_keys, load_references, load_resource_meta


def engine_meta():
    """Load the metadata that drives the generic engine: per-resource
    read/create paths + ignores (ResourceMeta), cross-resource id references,
    and natural-key specs, plus the resource set ``--engine`` can actually walk.

    ``generator_config.yaml`` (ResourceMeta) currently covers ~60 vendor
    entities, but ``natural_keys.yaml`` only has entries for the handful
    onboarded to the engine so far (see CLAUDE.md task sequencing). A resource
    without a natural-key spec would KeyError inside ``natural_key()``, so the
    usable resource set is the intersection, not all of ``meta``. ``account``
    has no ``generator_config.yaml`` entry under that exact name (a vendor gap)
    but is supplied a list read_path via ``load.READ_OVERRIDES``, so it now joins
    the set and is read as the union of /v3/account + /v3/account-cache.

    Returns ``(meta, refs, nkeys, resources)``.
    """
    meta = load_resource_meta()
    refs = load_references()
    nkeys = load_natural_keys()
    resources = sorted(r for r in meta if r in nkeys)
    return meta, refs, nkeys, resources
