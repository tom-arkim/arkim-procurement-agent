"""
scripts/manage_suppliers.py — CLI tool for supplier registry management.

Usage:
    python scripts/manage_suppliers.py list
    python scripts/manage_suppliers.py show <name>
    python scripts/manage_suppliers.py update <name> --status <status> [--email <email>]
    python scripts/manage_suppliers.py seed

Commands:
    list      List all supplier records
    show      Show detail for one supplier by name
    update    Update a supplier's onboarding_status and/or contact_email
    seed      Re-run seed (safe — skips if records already exist)

Onboarding statuses:
    discovery_only           Encountered during web search; not contacted yet
    invited                  Partner invitation email sent
    onboarded_arkim_supplier Active Arkim supplier; eligible for "Direct Buy via Arkim"
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.supplier_registry import (
    all_entries, lookup_supplier, update_supplier, _get_conn, _maybe_seed
)

_STATUS_VALUES = ("discovery_only", "invited", "onboarded_arkim_supplier")


def cmd_list(args) -> None:
    entries = all_entries()
    if not entries:
        print("Registry is empty. Run: python scripts/manage_suppliers.py seed")
        return

    sep = "-" * 100
    print(f"\n{'='*100}")
    print(f"  ARKIM SUPPLIER REGISTRY -- {len(entries)} record(s)")
    print(f"{'='*100}")
    print(f"  {'Name':<30} {'Domain':<28} {'Status':<26} {'Auth':<14} {'Email'}")
    print(sep)
    for e in entries:
        email = e.get("contact_email") or "-"
        print(
            f"  {e['name']:<30} {(e.get('domain') or '--'):<28} "
            f"{e['onboarding_status']:<26} {e.get('vendor_authorization_status','?'):<14} "
            f"{email}"
        )
    print()


def cmd_show(args) -> None:
    entry = lookup_supplier(args.name)
    if not entry:
        print(f"Not found: {args.name!r}")
        sys.exit(1)
    sep = "-" * 60
    print(f"\n{sep}")
    for k, v in entry.items():
        print(f"  {k:<32}: {v}")
    print(sep)


def cmd_update(args) -> None:
    updates = {}
    if args.status:
        if args.status not in _STATUS_VALUES:
            print(f"Invalid status: {args.status!r}")
            print(f"Valid values: {', '.join(_STATUS_VALUES)}")
            sys.exit(1)
        updates["onboarding_status"] = args.status
    if args.email:
        updates["contact_email"] = args.email
    if args.auth:
        if args.auth not in ("Authorized", "Unauthorized", "Unknown"):
            print("--auth must be one of: Authorized, Unauthorized, Unknown")
            sys.exit(1)
        updates["vendor_authorization_status"] = args.auth

    if not updates:
        print("No fields to update -- pass --status, --email, or --auth")
        sys.exit(1)

    ok = update_supplier(args.name, **updates)
    if ok:
        print(f"Updated: {args.name}")
        for k, v in updates.items():
            if k != "updated_at":
                print(f"  {k}: {v}")
    else:
        print(f"Not found or update failed: {args.name!r}")
        sys.exit(1)


def cmd_seed(args) -> None:
    conn = _get_conn()
    before = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    _maybe_seed(conn)
    after  = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
    print(f"Seed complete. Records: {before} -> {after}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Arkim supplier registry management")
    sub    = parser.add_subparsers(dest="command")

    sub.add_parser("list",  help="List all supplier records")

    p_show = sub.add_parser("show",   help="Show one supplier")
    p_show.add_argument("name")

    p_upd = sub.add_parser("update",  help="Update a supplier record")
    p_upd.add_argument("name")
    p_upd.add_argument("--status", help="New onboarding_status")
    p_upd.add_argument("--email",  help="Contact email address")
    p_upd.add_argument("--auth",   help="vendor_authorization_status")

    sub.add_parser("seed",   help="Re-run seed (idempotent)")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    dispatch = {"list": cmd_list, "show": cmd_show, "update": cmd_update, "seed": cmd_seed}
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
