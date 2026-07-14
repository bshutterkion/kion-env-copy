import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.refmap import to_natural, to_target_ids
from kion.meta.load import Reference

REFS = [Reference("payer_id", "billing_source", "name"),
        Reference("project_id", "project", "name", optional=True)]

def test_to_natural_replaces_ids_with_keys():
    rec = {"account_number": "n", "payer_id": 2, "project_id": 5}
    id_to_key = {("billing_source", 2): ("focus databricks",), ("project", 5): ("app",)}
    out = to_natural(rec, REFS, id_to_key)
    assert out["payer_id"] == ("focus databricks",)
    assert out["project_id"] == ("app",)
    assert out["__srcid__payer_id"] == 2

def test_to_target_ids_flags_unresolved_required():
    rec = {"payer_id": ("focus databricks",), "project_id": ("gone",)}
    key_to_tid = {("billing_source", ("focus databricks",)): 900}
    out, unresolved = to_target_ids(rec, REFS, key_to_tid)
    assert out["payer_id"] == 900
    assert out["project_id"] is None      # optional -> None, not fatal
    assert unresolved == []               # only required missing refs are flagged
