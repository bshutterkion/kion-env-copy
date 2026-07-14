import os, yaml
HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "..", "kion", "meta", "vendor")

def _load(name):
    with open(os.path.join(VENDOR, name)) as f:
        return yaml.safe_load(f)

def test_generator_config_covers_the_seven_backbone():
    gc = _load("generator_config.yaml")["resources"]
    # account is INCOMPLETE in codegen (handled by overrides); the rest must exist
    for name in ["billing_source", "ou", "funding_source", "project", "budget", "scope"]:
        assert name in gc, f"{name} missing from vendored generator_config"
        assert gc[name]["read"]["path"], f"{name} has no read path"

def test_archetypes_and_memberships_parse():
    assert isinstance(_load("crud_archetypes.yaml"), dict)
    assert isinstance(_load("memberships.yaml"), dict)
