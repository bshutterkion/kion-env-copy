"""Structural guard for the 49-resource metadata sweep (feature/engine-onboard-resources).

Pure / no-network: only reads the vendored + hand-authored YAML metadata and
the onboarding proposal JSON files that drove this sweep -- never touches a
live Kion install. See docs/ONBOARDING_REPORT.md for the human-readable
summary this test's fixtures are drawn from.

For every non-skip resource named in .superpowers/onboard/proposals/*.json,
asserts:
  1. it has a kion/meta/natural_keys.yaml entry,
  2. load_resource_meta()[resource] exists and has a non-null read_path,
  3. if its natural-key kind is name_in_parent, parent_field is set, and
  4. every references.yaml target resource is itself a known resource
     (present in natural_keys.yaml) -- checked once, not per-resource, since
     it's a property of the references graph as a whole.
"""
import glob
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import load_natural_keys, load_references, load_resource_meta

HERE = os.path.dirname(os.path.abspath(__file__))
PROPOSALS_DIR = os.path.join(HERE, "..", ".superpowers", "onboard", "proposals")


def _load_proposals():
    proposals = {}
    for path in sorted(glob.glob(os.path.join(PROPOSALS_DIR, "*.json"))):
        with open(path) as f:
            d = json.load(f)
        proposals[d["resource"]] = d
    return proposals


_PROPOSALS = _load_proposals()
_NON_SKIP_RESOURCES = sorted(
    r for r, d in _PROPOSALS.items() if d["classification"] != "skip"
)


def test_proposals_directory_is_non_empty():
    # Guards against a silently-empty glob making every parametrized test below
    # vacuously pass.
    assert len(_PROPOSALS) > 0
    assert len(_NON_SKIP_RESOURCES) > 0


@pytest.mark.parametrize("resource", _NON_SKIP_RESOURCES)
def test_non_skip_resource_has_natural_key_entry(resource):
    nkeys = load_natural_keys()
    assert resource in nkeys, (
        f"{resource}: classified '{_PROPOSALS[resource]['classification']}' "
        f"(non-skip) but missing from kion/meta/natural_keys.yaml"
    )


@pytest.mark.parametrize("resource", _NON_SKIP_RESOURCES)
def test_non_skip_resource_has_resource_meta_with_read_path(resource):
    meta = load_resource_meta()
    assert resource in meta, f"{resource}: no ResourceMeta (missing from generator_config.yaml / READ_OVERRIDES)"
    assert meta[resource].read_path, f"{resource}: ResourceMeta has no read_path"


@pytest.mark.parametrize("resource", _NON_SKIP_RESOURCES)
def test_name_in_parent_resources_declare_parent_field(resource):
    nkeys = load_natural_keys()
    spec = nkeys.get(resource)
    assert spec is not None, f"{resource}: missing from natural_keys.yaml"
    if spec.get("kind") == "name_in_parent":
        assert spec.get("parent_field"), (
            f"{resource}: kind is name_in_parent but parent_field is not set"
        )


def test_every_reference_target_is_a_known_resource():
    """references.yaml's `target` values must themselves have a natural_keys.yaml
    entry (the generic id->natural-key remap looks the target up by natural key),
    and the *source* resource of every references.yaml entry must be a known,
    non-skip resource too."""
    nkeys = load_natural_keys()
    refs = load_references()
    unknown_targets = []
    for source_resource, ref_list in refs.items():
        assert source_resource in nkeys, (
            f"references.yaml has an entry for '{source_resource}' but it has "
            f"no natural_keys.yaml entry"
        )
        for ref in ref_list:
            if ref.target not in nkeys:
                unknown_targets.append((source_resource, ref.field, ref.target))
    assert not unknown_targets, (
        "references.yaml has reference(s) whose target isn't a known resource "
        f"(present in natural_keys.yaml): {unknown_targets}"
    )
