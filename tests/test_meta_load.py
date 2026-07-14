import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
