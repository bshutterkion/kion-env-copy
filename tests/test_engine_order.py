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


def test_self_reference_appears_once_no_infinite_recursion():
    """A self-referential resource (OU's parent_ou_id -> ou) must not recurse
    forever; the ``r.target != res`` guard skips the self-edge, so it appears
    exactly once (item H)."""
    refs = {"ou": [Reference("parent_ou_id", "ou", "name")]}
    assert order_resources(["ou"], refs) == ["ou"]


def test_out_of_set_dependency_ignored():
    """A reference to a target NOT in the resource set is ignored: the target is
    never pulled into the order, and the depending resource still orders (item H)."""
    refs = {"project": [Reference("ou_id", "ou", "name")]}
    order = order_resources(["project"], refs)   # 'ou' deliberately not in the set
    assert order == ["project"]
    assert "ou" not in order


def test_stable_input_order_without_dependencies():
    """With no cross-references, input order is preserved (item H)."""
    assert order_resources(["c", "a", "b"], {}) == ["c", "a", "b"]


def test_optional_reference_still_orders_target_first():
    """An optional reference still constrains order (account's optional
    project_id -> project keeps project before account) (item H)."""
    refs = {"account": [Reference("project_id", "project", "name", optional=True)]}
    order = order_resources(["account", "project"], refs)
    assert order.index("project") < order.index("account")
