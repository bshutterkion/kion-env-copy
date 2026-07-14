#!/usr/bin/env python3
"""Copy a Kion environment (OUs, funding sources, projects, budgets) between installs.

  export:  read a source install            -> snapshot.json
  import:  recreate it on a target install  (dry-run by default; --apply to write)

See .env.example for configuration and the two-file copy workflow.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from kion.client import KionAPIError, KionClient
from kion.config import Config
from kion.engine.inventory import build_inventory
from kion.engine.reconcile import EngineReconciler
from kion.engine.setup import engine_meta
from kion.export import export_install
from kion.import_ import Importer


def _client(cfg: Config) -> KionClient:
    return KionClient(cfg.url, cfg.api_key, verify_ssl=cfg.verify_ssl, api_prefix=cfg.api_prefix)


def cmd_export(args) -> int:
    cfg = Config.load(args.env_file)
    client = _client(cfg)
    print(f"Exporting from {cfg.url} ...")
    try:
        if args.engine:
            meta, refs, nkeys, resources = engine_meta()
            snapshot = build_inventory(client, meta, refs, nkeys, resources)
        else:
            snapshot = export_install(client)
    except KionAPIError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    with open(args.out, "w") as f:
        json.dump(snapshot, f, indent=2)
    print(f"\nWrote snapshot -> {args.out}")
    return 0


def cmd_import(args) -> int:
    cfg = Config.load(args.env_file)
    client = _client(cfg)
    with open(args.snapshot) as f:
        snapshot = json.load(f)

    # id-map.json is the state file: load it so reconcile knows what is already managed.
    id_map = None
    if os.path.exists(args.id_map):
        with open(args.id_map) as f:
            id_map = json.load(f)
        print(f"Loaded state from {args.id_map}")

    if args.engine:
        meta, refs, nkeys, _resources = engine_meta()
        reconciler = EngineReconciler(client, cfg, snapshot, meta, refs, nkeys,
                                       apply=args.apply, id_map=id_map)
        try:
            result = reconciler.run()
        except KionAPIError as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            return 1
    else:
        try:
            importer = Importer(client, cfg, snapshot, apply=args.apply, id_map=id_map,
                                only=args.only)
        except ValueError as e:
            print(f"\nERROR: invalid --only: {e}", file=sys.stderr)
            return 2
        try:
            result = importer.run()
        except KionAPIError as e:
            print(f"\nERROR: {e}", file=sys.stderr)
            return 1

    if args.apply:
        with open(args.id_map, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\nWrote state -> {args.id_map}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # --env-file lives on each subcommand (used after the command, as in the README).
    env_parent = argparse.ArgumentParser(add_help=False)
    env_parent.add_argument("--env-file", default=".env", help="path to .env (default: .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    pe = sub.add_parser("export", parents=[env_parent], help="export a source install to snapshot.json")
    pe.add_argument("--out", default="snapshot.json", help="output file (default: snapshot.json)")
    pe.add_argument("--engine", action="store_true",
                    help="use the generic metadata-driven engine (build_inventory) "
                         "instead of the hand-written export_install")
    pe.set_defaults(func=cmd_export)

    pi = sub.add_parser("import", parents=[env_parent],
                        help="reconcile a snapshot into a target install (plan, then --apply)")
    pi.add_argument("--snapshot", default="snapshot.json", help="snapshot file (default: snapshot.json)")
    pi.add_argument("--id-map", default="id-map.json", help="state file: old->new id map (default: id-map.json)")
    pi.add_argument("--apply", action="store_true", help="make changes (default: plan only, no writes)")
    pi.add_argument("--only", default=None,
                    help="comma-separated entity kinds to sync instead of all "
                         "(billing_sources,ous,funding_sources,projects,budgets,accounts,scopes). "
                         "e.g. --only billing_sources,accounts. Ignored with --engine.")
    pi.add_argument("--engine", action="store_true",
                    help="use the generic metadata-driven engine (EngineReconciler) "
                         "instead of the hand-written Importer")
    pi.set_defaults(func=cmd_import)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
