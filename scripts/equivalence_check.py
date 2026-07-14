#!/usr/bin/env python3
"""Live equivalence gate (Task 11): does the metadata-driven engine reproduce the
hand-written oracle's PLAN decisions for the 7 entities?

Reads a source install once, then plans it against a target TWICE — with the
oracle (``export_install`` + ``Importer``) and with the engine
(``build_inventory`` + ``EngineReconciler``) — both in PLAN mode (no writes).
The two count dictionaries are normalized (the two known surface differences,
below) and diffed per entity per action. Prints a per-entity table and a final
EQUIVALENT / DIVERGENT verdict; exits 0 when equivalent, 1 when divergent, 2 on a
run/network error.

Known, ACCEPTED surface differences (normalized here, not hidden):
  1. Count-dict key names: the oracle uses PLURAL kind keys
     (billing_sources, ous, ...); the engine uses SINGULAR (billing_source, ou,
     ...). Paired explicitly via PAIRS.
  2. OU root: the engine counts the source-root -> target-root mapping as
     ``ou.ok = 1``; the oracle maps it uncounted. So the engine's ou/ok is
     expected to be exactly the oracle's ous/ok + 1. Normalized on the ou row
     only; any other +N is a real divergence.

Any OTHER per-entity per-action difference is a REAL divergence and is reported.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kion.client import KionAPIError, KionClient
from kion.config import Config
from kion.engine.inventory import build_inventory
from kion.engine.reconcile import EngineReconciler
from kion.engine.setup import engine_meta
from kion.export import export_install
from kion.import_ import Importer

# (oracle plural kind, engine singular resource)
PAIRS = [
    ("billing_sources", "billing_source"),
    ("ous", "ou"),
    ("funding_sources", "funding_source"),
    ("projects", "project"),
    ("budgets", "budget"),
    ("accounts", "account"),
    ("scopes", "scope"),
]
ACTIONS = ("create", "recreate", "adopt", "ok")   # from .counts[kind][action]
# skipped/failed are separate per-kind dicts on both reconcilers.


def _client(cfg: Config) -> KionClient:
    return KionClient(cfg.url, cfg.api_key, verify_ssl=cfg.verify_ssl,
                      api_prefix=cfg.api_prefix)


def _oracle_counts(row: dict, action: str) -> int:
    return row["counts"][action]


def _cell(counts: dict, skipped: int, failed: int, action: str) -> int:
    return skipped if action == "skipped" else failed if action == "failed" else counts[action]


def gather(source_env: str, target_env: str, quiet: bool = True):
    """Run both planners against the live installs and return their raw
    counts/skipped/failed. Sub-call stdout is suppressed (both reconcilers are
    chatty) so this script's own table is the only thing on stdout."""
    src_cfg = Config.load(source_env)
    src_client = _client(src_cfg)
    tgt_cfg = Config.load(target_env)
    tgt_client = _client(tgt_cfg)

    meta, refs, nkeys, resources = engine_meta()

    sink = io.StringIO() if quiet else sys.stdout
    with contextlib.redirect_stdout(sink):
        oracle_snapshot = export_install(src_client)
        engine_inventory = build_inventory(src_client, meta, refs, nkeys, resources)

        importer = Importer(tgt_client, tgt_cfg, oracle_snapshot, apply=False)
        importer.run()

        reconciler = EngineReconciler(tgt_client, tgt_cfg, engine_inventory,
                                      meta, refs, nkeys, apply=False)
        reconciler.run()

    oracle = {
        "counts": importer.counts, "skipped": importer.skipped, "failed": importer.failed,
    }
    engine = {
        "counts": reconciler.counts, "skipped": reconciler.skipped, "failed": reconciler.failed,
    }
    return oracle, engine, src_cfg, tgt_cfg


def diff(oracle: dict, engine: dict):
    """Compare the two plans per entity per action after normalization. Returns
    (rows, divergences). Each row: (plural, action, oracle_val, engine_val,
    normalized_oracle_baseline, ok_bool, note)."""
    rows = []
    divergences = []
    columns = ACTIONS + ("skipped", "failed")
    for plural, singular in PAIRS:
        o_counts = oracle["counts"].get(plural, {})
        e_counts = engine["counts"].get(singular, {})
        o_skip = oracle["skipped"].get(plural, 0)
        o_fail = oracle["failed"].get(plural, 0)
        e_skip = engine["skipped"].get(singular, 0)
        e_fail = engine["failed"].get(singular, 0)
        for action in columns:
            o_val = _cell(o_counts, o_skip, o_fail, action)
            e_val = _cell(e_counts, e_skip, e_fail, action)
            note = ""
            baseline = o_val
            # OU-root normalization: engine ou.ok is expected to be oracle ous.ok + 1.
            if plural == "ous" and action == "ok":
                baseline = o_val + 1
                note = "root +1 (normalized)"
            ok = (e_val == baseline)
            rows.append((plural, action, o_val, e_val, baseline, ok, note))
            if not ok:
                divergences.append((plural, singular, action, o_val, e_val, baseline, note))
    return rows, divergences


def print_report(rows, divergences, oracle, engine, src_cfg, tgt_cfg) -> None:
    print("=" * 78)
    print("EQUIVALENCE CHECK — engine vs oracle (PLAN only, no writes)")
    print(f"  source: {src_cfg.url}")
    print(f"  target: {tgt_cfg.url}")
    print("=" * 78)
    header = f"{'entity':16} {'action':9} {'oracle':>7} {'engine':>7}  status"
    print(header)
    print("-" * len(header))
    last_entity = None
    for plural, action, o_val, e_val, baseline, ok, note in rows:
        ent = plural if plural != last_entity else ""
        last_entity = plural
        if ok and note:
            status = f"ok   ({note})"
        elif ok:
            status = "ok"
        else:
            status = f"DIVERGE (expected {baseline})" + (f" [{note}]" if note else "")
        print(f"{ent:16} {action:9} {o_val:7d} {e_val:7d}  {status}")
    print("-" * len(header))
    if divergences:
        print(f"\nDIVERGENT — {len(divergences)} divergence(s):")
        for plural, singular, action, o_val, e_val, baseline, note in divergences:
            extra = f" (normalized baseline {baseline})" if baseline != o_val else ""
            print(f"  - {plural}/{singular} {action}: "
                  f"oracle={o_val} engine={e_val}{extra}")
    else:
        print("\nEQUIVALENT — every entity/action matches after normalization "
              "(plural↔singular keys; OU-root +1).")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-env", default=".env.source",
                    help="source install .env (read-only export; default .env.source)")
    ap.add_argument("--target-env", default=".env.target",
                    help="target install .env (PLAN only, no writes; default .env.target)")
    ap.add_argument("--verbose", action="store_true",
                    help="do not suppress the reconcilers' own plan output")
    args = ap.parse_args()

    try:
        oracle, engine, src_cfg, tgt_cfg = gather(
            args.source_env, args.target_env, quiet=not args.verbose)
    except KionAPIError as e:
        print(f"\nERROR (API): {e}", file=sys.stderr)
        return 2
    except (OSError, SystemExit) as e:
        print(f"\nERROR (config/io): {e}", file=sys.stderr)
        return 2

    rows, divergences = diff(oracle, engine)
    print_report(rows, divergences, oracle, engine, src_cfg, tgt_cfg)
    return 1 if divergences else 0


if __name__ == "__main__":
    raise SystemExit(main())
