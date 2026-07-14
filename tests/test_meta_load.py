import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.setup import engine_meta
from kion.meta.load import load_resource_meta

def test_loads_entity_ops():
    meta = load_resource_meta()
    ou = meta["ou"]
    assert ou.create_method == "POST" and ou.create_path == "/v3/ou"
    assert ou.read_path == "/v3/ou/{id}"
    assert "status" in ou.ignores
    assert ou.archetype == "entity"

def test_loads_compound_archetype():
    meta = load_resource_meta()
    sc = meta["scope_criteria"]
    assert sc.archetype == "compound_key_parent_read"
    assert sc.parent_id_field == "scope_id"
    assert sc.collection == "CriteriaRecords"

ORIGINAL_SEVEN = [
    "account", "billing_source", "budget", "funding_source",
    "ou", "project", "scope",
]

# 30 resources added by the 49-resource metadata sweep (feature/engine-onboard-resources,
# .superpowers/onboard/proposals/*.json). See docs/ONBOARDING_REPORT.md for the
# per-resource classification. Having a natural-key + ResourceMeta entry only means
# the engine CAN enumerate/adopt/create the resource generically -- the 20 "hook"
# and 1 "read_transform" resources below still need their build_create_payload /
# reader hook registered in kion/overrides/registry.py (or kion/engine/inventory.py)
# before a real --engine run would produce correct payloads for them. That's the
# next phase's work, tracked in the report; this test only guards that the metadata
# itself loads and the resource set grows as expected.
ONBOARDED_GENERIC = [
    "app_api_key", "app_role", "billing_rule", "category", "compliance_family",
    "compliance_level", "compliance_program", "idms", "webhook",
]
ONBOARDED_HOOK = [
    "ami", "azure_arm_template", "azure_policy", "azure_role", "cft", "cloud_rule",
    "compliance_check", "compliance_control", "compliance_standard", "custom_variable",
    "gcp_iam_role", "iam_policy", "idms_open_id", "ou_cloud_access_role", "ou_note",
    "project_cloud_access_role", "project_note", "service_catalog",
    "service_control_policy", "user_group",
]
ONBOARDED_READ_TRANSFORM = ["user"]

def test_engine_meta_returns_the_onboarded_resources():
    """The shared bootstrap (item B: one impl for kion_copy + equivalence_check)
    returns meta/refs/nkeys plus the usable resource set -- the intersection of
    ResourceMeta and natural-key specs. The 49-resource metadata sweep expanded
    this from the original 7 to the original 7 plus the 30 non-skip resources
    from that sweep (generic + hook + read_transform all get a natural key;
    only "hook"/"read_transform" resources additionally need a registry.py hook
    before they're safe to reconcile live -- see docs/ONBOARDING_REPORT.md)."""
    meta, refs, nkeys, resources = engine_meta()
    expected = sorted(
        ORIGINAL_SEVEN + ONBOARDED_GENERIC + ONBOARDED_HOOK + ONBOARDED_READ_TRANSFORM)
    assert resources == expected
    # every returned resource has both a ResourceMeta and a natural-key spec
    assert all(r in meta and r in nkeys for r in resources)
