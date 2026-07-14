"""Offline equivalence guard (Task 11).

The metadata-driven engine (``EngineReconciler``) must make the SAME plan
decisions the hand-written oracle (``Importer``) would for the same environment.
The live ``scripts/equivalence_check.py`` proves this against real installs; this
test proves the engine's decision logic in CI, with no network, against a small
hand-checked inventory fixture whose expected counts are the oracle's plan for
the equivalent snapshot.

Fixture: ``tests/fixtures/equivalence_billing_account.json`` exercises
billing_source + account (routed to the target account cache). The oracle plan it
records, verified by hand against ``Importer._reconcile_billing_sources`` /
``_reconcile_accounts``:

  * ``billing_source`` "Custom BS" -> CREATE (type ``custom`` has an API recreate
    path via ``_billing_payload``); "GCP BS" -> SKIP (gcp has no recreate path).
  * ``account`` "Acct A" -> CREATE into the cache (its custom payer resolves to
    the just-created billing source, and it has no project); "Acct C" -> ADOPT
    (account number 333 already on the target); "Acct B" -> SKIP (its payer is
    the GCP billing source, which was skipped, so the payer id never mapped).

Because the two billing sources reconcile before the accounts (dependency order),
the account decisions depend on the billing-source plan actually running first --
so this asserts the cross-resource wiring, not just isolated per-record logic.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kion.engine.reconcile import EngineReconciler
from kion.meta.load import load_natural_keys, load_references, load_resource_meta

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "equivalence_billing_account.json")


def _load_fixture():
    with open(FIXTURE) as f:
        return json.load(f)


def _run_engine_plan(fx):
    """Run EngineReconciler in PLAN mode over the fixture inventory, with the
    target index injected (no network), mirroring the live harness' engine leg."""
    meta = load_resource_meta()
    refs = load_references()
    nkeys = load_natural_keys()
    r = EngineReconciler(client=None, config=None, inventory=fx["inventory"],
                         meta=meta, refs=refs, nkeys=nkeys, apply=False)
    # Inject the target index directly (bypass _index_target). The engine keys
    # _t_key on natural-key TUPLES; the fixture's target keys are single-component
    # (a billing-source name / an account number), so wrap each as a 1-tuple.
    r._t_key = {res: {(k,): v for k, v in kmap.items()}
                for res, kmap in fx["inject"]["t_key"].items()}
    r._t_ids = {res: set(ids) for res, ids in fx["inject"]["t_ids"].items()}
    r.run()
    return r


def test_offline_equivalence_billing_and_account_plan():
    fx = _load_fixture()
    r = _run_engine_plan(fx)

    for res, exp in fx["expected_engine_counts"].items():
        if res == "_doc":
            continue
        for action in ("create", "adopt", "recreate", "ok"):
            assert r.counts[res][action] == exp[action], (
                f"{res}.{action}: engine {r.counts[res][action]} != expected {exp[action]}")
        assert r.skipped[res] == exp["skipped"], (
            f"{res}.skipped: engine {r.skipped[res]} != expected {exp['skipped']}")
        assert r.failed[res] == exp["failed"], (
            f"{res}.failed: engine {r.failed[res]} != expected {exp['failed']}")


def test_offline_equivalence_records_nontrivial_plan():
    """Guard against a tautological fixture: the expected plan must actually
    contain a create, an adopt, and a skip (not all-zero)."""
    fx = _load_fixture()
    exp = fx["expected_engine_counts"]
    assert exp["billing_source"]["create"] >= 1
    assert exp["billing_source"]["skipped"] >= 1
    assert exp["account"]["create"] >= 1
    assert exp["account"]["adopt"] >= 1
    assert exp["account"]["skipped"] >= 1
