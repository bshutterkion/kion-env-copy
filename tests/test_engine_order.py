import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.order import order_resources
from kion.meta.load import Reference

def test_orders_after_dependencies():
    refs = {
        "project": [Reference("ou_id", "ou", "name")],
        "account": [Reference("payer_id", "billing_source", "name"),
                    Reference("project_id", "project", "name", optional=True)],
        "scope":   [Reference("project_id", "project", "name")],
    }
    order = order_resources(["scope", "account", "project", "ou", "billing_source"], refs)
    assert order.index("ou") < order.index("project")
    assert order.index("project") < order.index("scope")
    assert order.index("billing_source") < order.index("account")
