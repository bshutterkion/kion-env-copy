import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.meta.load import load_references, load_natural_keys

def test_references_for_account():
    refs = load_references()
    by_field = {r.field: r for r in refs["account"]}
    assert by_field["payer_id"].target == "billing_source"
    assert by_field["project_id"].optional is True

def test_natural_key_kinds():
    nk = load_natural_keys()
    assert nk["budget"]["kind"] == "date_range"
    assert nk["project"]["parent_field"] == "ou_id"
