import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.keys import natural_key

NK = {
    "billing_source": {"kind": "name"},
    "budget": {"kind": "date_range"},
    "account": {"kind": "account_number"},
    "project": {"kind": "name_in_parent", "parent_field": "ou_id"},
}

def test_name_key():
    assert natural_key("billing_source", {"name": " Prod "}, NK) == ("prod",)

def test_date_range_key():
    b = {"start_datecode": "2026-01", "end_datecode": "2027-01"}
    assert natural_key("budget", b, NK) == ("2026-01", "2027-01")

def test_account_number_key():
    assert natural_key("account", {"account_number": "123"}, NK) == ("123",)

def test_name_in_parent_uses_raw_parent_when_no_resolver():
    p = {"name": "App", "ou_id": 9}
    assert natural_key("project", p, NK) == (9, "app")
