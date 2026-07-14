"""Structural guard for the 49-resource metadata sweep (feature/engine-onboard-resources).

Pure / no-network: only reads the vendored + hand-authored YAML metadata and
the onboarding proposal JSON files that drove this sweep -- never touches a
live Kion install. See docs/ONBOARDING_REPORT.md for the human-readable
summary this test's fixtures are drawn from.

Safety split: only resources classified "generic" belong in the ACTIVE
kion/meta/natural_keys.yaml / references.yaml (what engine_meta() loads).
Resources classified "hook" or "read_transform" have no hook registered in
kion/overrides/registry.py yet, so their metadata must live in the STAGED
kion/meta/natural_keys.staged.yaml / references.staged.yaml instead -- those
files are never read by kion/meta/load.py or kion/engine/setup.py. This test
enforces that split, in addition to the original structural guards:
  1. every non-skip resource has an entry in the correct file (active for
     generic, staged for hook/read_transform) -- and NOT in the other one,
  2. load_resource_meta()[resource] exists and has a non-null read_path,
  3. if its natural-key kind is name_in_parent, parent_field is set, and
  4. every references.yaml / references.staged.yaml target resource is
     itself a known resource (present in the active+staged natural-key set) --
     checked once, not per-resource, since it's a property of the references
     graph as a whole.
  5. no staged (hook/read_transform) resource is in engine_meta()'s active
     resource set.
"""
import glob
import json
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.setup import engine_meta
from kion.meta.load import load_natural_keys, load_references, load_resource_meta

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS_DIR = os.path.join(HERE, "..", ".superpowers", "onboard", "proposals")
META_DIR = os.path.join(HERE, "..", "kion", "meta")


def _load_proposals():
    proposals = {}
    for path in sorted(glob.glob(os.path.join(PROPOSALS_DIR, "*.json"))):
        with open(path) as f:
            d = json.load(f)
        proposals[d["resource"]] = d
    return proposals


def _load_yaml(name):
    with open(os.path.join(META_DIR, name)) as f:
        return yaml.safe_load(f) or {}


_PROPOSALS = _load_proposals()
_NON_SKIP_RESOURCES = sorted(
    r for r, d in _PROPOSALS.items() if d["classification"] != "skip"
)
_GENERIC_RESOURCES = sorted(
    r for r, d in _PROPOSALS.items() if d["classification"] == "generic"
)
_STAGED_RESOURCES = sorted(
    r for r, d in _PROPOSALS.items() if d["classification"] in ("hook", "read_transform")
)

_STAGED_NKEYS = _load_yaml("natural_keys.staged.yaml")
_STAGED_REFS = _load_yaml("references.staged.yaml")


def test_proposals_directory_is_non_empty():
    # Guards against a silently-empty glob making every parametrized test below
    # vacuously pass.
    assert len(_PROPOSALS) > 0
    assert len(_NON_SKIP_RESOURCES) > 0


def test_proposal_count_and_classification_tally_matches_49():
    # Locks in the corrected count (49, not the earlier sweep's undercounted
    # 46) now that permission_scheme, project_cloud_access_role_exemption,
    # and project_enforcement have been analyzed. compliance_family and
    # compliance_level were later reclassified generic -> read_transform
    # after a live smoke test found their list-read endpoint 405s (no flat
    # list endpoint -- see docs/ONBOARDING_REPORT.md), so generic dropped
    # from 9 to 7 and read_transform grew from 1 to 3; the total stays 49.
    assert len(_PROPOSALS) == 49
    tally = {}
    for d in _PROPOSALS.values():
        tally[d["classification"]] = tally.get(d["classification"], 0) + 1
    assert tally == {"generic": 7, "hook": 21, "read_transform": 3, "skip": 18}


@pytest.mark.parametrize("resource", _GENERIC_RESOURCES)
def test_generic_resource_has_active_natural_key_entry(resource):
    nkeys = load_natural_keys()
    assert resource in nkeys, (
        f"{resource}: classified 'generic' but missing from the active "
        f"kion/meta/natural_keys.yaml"
    )
    assert resource not in _STAGED_NKEYS, (
        f"{resource}: classified 'generic' but also present in "
        f"kion/meta/natural_keys.staged.yaml -- should only be active"
    )


@pytest.mark.parametrize("resource", _STAGED_RESOURCES)
def test_hook_or_read_transform_resource_is_staged_not_active(resource):
    nkeys = load_natural_keys()
    assert resource not in nkeys, (
        f"{resource}: classified '{_PROPOSALS[resource]['classification']}' "
        f"(no hook registered yet) but present in the ACTIVE "
        f"kion/meta/natural_keys.yaml -- this would make it engine-ready "
        f"and unsafe for a live --engine run. It must live in "
        f"kion/meta/natural_keys.staged.yaml instead."
    )
    assert resource in _STAGED_NKEYS, (
        f"{resource}: classified '{_PROPOSALS[resource]['classification']}' "
        f"but missing from kion/meta/natural_keys.staged.yaml"
    )


@pytest.mark.parametrize("resource", _NON_SKIP_RESOURCES)
def test_non_skip_resource_has_resource_meta_with_read_path(resource):
    meta = load_resource_meta()
    assert resource in meta, f"{resource}: no ResourceMeta (missing from generator_config.yaml / READ_OVERRIDES)"
    assert meta[resource].read_path, f"{resource}: ResourceMeta has no read_path"


@pytest.mark.parametrize("resource", _NON_SKIP_RESOURCES)
def test_name_in_parent_resources_declare_parent_field(resource):
    nkeys = load_natural_keys()
    spec = nkeys.get(resource) or _STAGED_NKEYS.get(resource)
    assert spec is not None, (
        f"{resource}: missing from both natural_keys.yaml and "
        f"natural_keys.staged.yaml"
    )
    if spec.get("kind") == "name_in_parent":
        assert spec.get("parent_field"), (
            f"{resource}: kind is name_in_parent but parent_field is not set"
        )


def test_every_reference_target_is_a_known_resource():
    """Both the active references.yaml and the staged references.staged.yaml
    have `target` values that must themselves be a known resource -- present
    in the active natural_keys.yaml OR the staged natural_keys.staged.yaml
    (staged references are allowed to point at other staged/future resources,
    since they're a forward-looking worklist, not live metadata). The
    *source* resource of every references.yaml entry must be active/known
    too, and every references.staged.yaml source must be one of the staged
    hook/read_transform resources (not accidentally duplicated into both
    files)."""
    nkeys = load_natural_keys()
    refs = load_references()
    known = set(nkeys) | set(_STAGED_NKEYS)

    unknown_targets = []
    for source_resource, ref_list in refs.items():
        assert source_resource in nkeys, (
            f"references.yaml has an entry for '{source_resource}' but it has "
            f"no ACTIVE natural_keys.yaml entry"
        )
        for ref in ref_list:
            if ref.target not in known:
                unknown_targets.append((source_resource, ref.field, ref.target))
    assert not unknown_targets, (
        "references.yaml has reference(s) whose target isn't a known resource "
        f"(present in natural_keys.yaml or natural_keys.staged.yaml): {unknown_targets}"
    )

    staged_unknown_targets = []
    for source_resource, ref_list in _STAGED_REFS.items():
        assert source_resource in _STAGED_NKEYS, (
            f"references.staged.yaml has an entry for '{source_resource}' but "
            f"it has no natural_keys.staged.yaml entry"
        )
        for ref in ref_list:
            if ref["target"] not in known:
                staged_unknown_targets.append((source_resource, ref["field"], ref["target"]))
    assert not staged_unknown_targets, (
        "references.staged.yaml has reference(s) whose target isn't a known "
        f"resource (active or staged): {staged_unknown_targets}"
    )


def test_no_staged_resource_is_in_engine_meta_active_set():
    """Safety guard, restated at the engine_meta() boundary (not just the raw
    YAML): no resource classified 'hook' or 'read_transform' by the
    onboarding sweep may appear in engine_meta()'s active `resources` list,
    since that's exactly the set kion_copy.py --engine and
    scripts/equivalence_check.py walk live."""
    _, _, _, resources = engine_meta()
    leaked = sorted(set(_STAGED_RESOURCES) & set(resources))
    assert not leaked, f"staged resource(s) leaked into engine_meta() active set: {leaked}"
