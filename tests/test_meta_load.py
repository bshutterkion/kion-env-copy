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

# 9 resources classified "generic" by the 49-resource metadata sweep
# (feature/engine-onboard-resources, .superpowers/onboard/proposals/*.json).
# See docs/ONBOARDING_REPORT.md for the per-resource classification. These are
# the ONLY sweep resources present in the active kion/meta/natural_keys.yaml /
# references.yaml -- they're copyable via the existing metadata-driven engine
# alone (natural key + references remap, no bespoke code), so they're safe for
# a live `--engine` run today.
ONBOARDED_GENERIC = [
    "app_api_key", "app_role", "billing_rule", "category", "compliance_family",
    "compliance_level", "compliance_program", "idms", "webhook",
]

# The 21 "hook" + 1 "read_transform" resources from the same sweep are
# INTENTIONALLY NOT in kion/meta/natural_keys.yaml / references.yaml -- they
# have no build_create_payload / reader hook registered in
# kion/overrides/registry.py (or kion/engine/inventory.py) yet, so the generic
# reconcile path would mis-copy them (unmapped owners, dropped type-specific
# nesting, wrong payload shape) on a live --engine run. Their metadata is
# staged in kion/meta/natural_keys.staged.yaml / references.staged.yaml
# instead, which load.py/setup.py never read. This list exists only so the
# test below can assert none of them leaked into the active resource set.
STAGED_HOOK = [
    "ami", "azure_arm_template", "azure_policy", "azure_role", "cft", "cloud_rule",
    "compliance_check", "compliance_control", "compliance_standard", "custom_variable",
    "gcp_iam_role", "iam_policy", "idms_open_id", "ou_cloud_access_role", "ou_note",
    "permission_scheme", "project_cloud_access_role", "project_note", "service_catalog",
    "service_control_policy", "user_group",
]
STAGED_READ_TRANSFORM = ["user"]
STAGED_RESOURCES = STAGED_HOOK + STAGED_READ_TRANSFORM

def test_engine_meta_returns_the_onboarded_resources():
    """The shared bootstrap (item B: one impl for kion_copy + equivalence_check)
    returns meta/refs/nkeys plus the usable resource set -- the intersection of
    ResourceMeta and natural-key specs. The 49-resource metadata sweep expanded
    this from the original 7 to the original 7 plus the 9 "generic" resources
    from that sweep -- the ACTIVE, engine-ready set. The 21 "hook" + 1
    "read_transform" resources are staged (not active) until their
    registry.py/inventory.py hooks land -- see docs/ONBOARDING_REPORT.md."""
    meta, refs, nkeys, resources = engine_meta()
    expected = sorted(ORIGINAL_SEVEN + ONBOARDED_GENERIC)
    assert resources == expected
    # every returned resource has both a ResourceMeta and a natural-key spec
    assert all(r in meta and r in nkeys for r in resources)

def test_no_staged_hook_or_read_transform_resource_is_active():
    """Safety guard: a resource classified 'hook' or 'read_transform' must
    never appear in engine_meta()'s active resource set -- it has no hook
    registered, so the generic reconcile path would mis-copy it. This is the
    invariant the natural_keys.yaml / natural_keys.staged.yaml split exists
    to enforce."""
    _, _, _, resources = engine_meta()
    leaked = sorted(set(STAGED_RESOURCES) & set(resources))
    assert not leaked, (
        f"staged hook/read_transform resource(s) leaked into the active "
        f"engine resource set: {leaked}"
    )
