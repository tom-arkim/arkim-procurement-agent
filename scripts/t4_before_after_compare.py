"""T4 before/after comparison: baseline 50-run vs the fresh vague-tier re-run.

Compares per-part wrongness flags (noun-class mismatch + above floor) for the
12 vague-* parts. The contamination signature: motor URLs (eMotors Direct,
Electric Motor Warehouse, MROSupply motor pages) and pump URLs (shoppumps.com)
appearing on unrelated vague queries (valve / solenoid / starter / hose /
level) at a hard-coded 50.0 suitability.

After T1+T2+T3:
  - T1: null-PN guard -> vague (PN-less) requests never read price_db, so the
    unknown|UNKNOWN-PN bucket can't be served.
  - T2: type-gate drops confirmed-wrong-class cached candidates at both seams.
  - T3: honest match_type (orthogonal to this table; tracked in the report).

Usage:
    python scripts/t4_before_after_compare.py \
        --before scripts/harness_results_20260704T183210Z.json \
        --after  <path-to-fresh-vague-run.json>
"""
import argparse
import json
from pathlib import Path
from typing import Any

# The contamination carriers — vendor names / URL hosts that signal a motor or
# pump listing. These are the rows that topped unrelated vague queries in the
# baseline 50-run at suitability=50.0.
MOTOR_HOSTS = ("emotorsdirect.ca", "electricmotorwarehouse.com", "mrosupply.com")
PUMP_HOSTS = ("shoppumps.com",)

# The vague parts where a motor/pump URL is WRONG (not the request's class).
# (vague-motor and vague-washdown-motor legitimately want motors; pump URLs on
#  those are still wrong-class but motor URLs are correct there.)
WRONG_CLASS_HOSTS_ON = {
    "vague-valve":       (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-solenoid":    (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-starter":     (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-hose":        (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-level":       (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-transmitter": (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-gasket":      (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-belting":     (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-chain":       (MOTOR_HOSTS, PUMP_HOSTS),
    "vague-gearbox-oil": (MOTOR_HOSTS, PUMP_HOSTS),
}


def _host(url: str) -> str:
    from urllib.parse import urlparse
    try:
        h = (urlparse((url or "").lower()).hostname or "").replace("www.", "")
    except Exception:
        return ""
    return h


def _wrongness_for_part(run_data: dict, part_id: str) -> list[dict]:
    """Return the wrongness flags for a part from a harness results file.

    The baseline file stores wrongness_flags as {part_id: [...]}; a fresh
    single-run file has `wrongness_flags` keyed the same way (the harness writes
    it per-run in the summary).
    """
    wf = run_data.get("wrongness_flags") or {}
    if isinstance(wf, dict):
        return wf.get(part_id, []) or []
    return []


def _candidates_for_part(run_data: dict, part_id: str) -> list[dict]:
    for r in run_data.get("records", []):
        if r.get("part_id") == part_id:
            return r.get("candidates", []) or []
    return []


def _contamination_flags(cands: list[dict], part_id: str) -> list[dict]:
    """Flag motor/pump-URL candidates on a part where they're wrong-class."""
    hosts = WRONG_CLASS_HOSTS_ON.get(part_id)
    if not hosts:
        return []
    motor_hosts, pump_hosts = hosts
    out = []
    for c in cands:
        if c.get("rejected"):
            continue
        url = c.get("url") or ""
        host = _host(url)
        vendor = (c.get("vendor") or "").lower()
        kind = None
        if any(h in host for h in motor_hosts) or "emotors direct" in vendor \
           or "electric motor warehouse" in vendor:
            kind = "MOTOR"
        elif any(h in host for h in pump_hosts) or "pump products" in vendor:
            kind = "PUMP"
        if kind:
            out.append({"vendor": c.get("vendor"), "url": url, "host": host,
                        "kind": kind, "suitability": c.get("suitability")})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    args = ap.parse_args()

    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))

    vague_ids = sorted(WRONG_CLASS_HOSTS_ON.keys())
    print("=" * 100)
    print("T4 BEFORE/AFTER — contamination on vague-tier (motor/pump URLs on wrong-class requests)")
    print("=" * 100)
    print(f"\nBEFORE = {args.before}")
    print(f"AFTER  = {args.after}\n")

    hdr = f"{'part':<22}{'before wrongness':<28}{'after wrongness':<28}{'before contam':<14}{'after contam':<14}"
    print(hdr)
    print("-" * 100)
    any_after_contam = False
    for pid in vague_ids:
        b_wf = _wrongness_for_part(before, pid)
        a_wf = _wrongness_for_part(after, pid)
        b_cands = _candidates_for_part(before, pid)
        a_cands = _candidates_for_part(after, pid)
        b_contam = _contamination_flags(b_cands, pid)
        a_contam = _contamination_flags(a_cands, pid)
        if a_contam:
            any_after_contam = True
        b_sum = f"{len(b_wf)}wf/{len(b_contam)}contam"
        a_sum = f"{len(a_wf)}wf/{len(a_contam)}contam"
        b_kinds = ",".join(sorted({c["kind"] for c in b_contam})) or "-"
        a_kinds = ",".join(sorted({c["kind"] for c in a_contam})) or "-"
        print(f"{pid:<22}{b_sum:<28}{a_sum:<28}{b_kinds:<14}{a_kinds:<14}")

    print("-" * 100)
    # Detail: any after-contamination rows
    if any_after_contam:
        print("\n!! AFTER contamination rows (regression — must be empty):")
        for pid in vague_ids:
            a_contam = _contamination_flags(_candidates_for_part(after, pid), pid)
            for c in a_contam:
                print(f"  [{pid}] {c['kind']} {c['vendor']} suit={c['suitability']} {c['url']}")
    else:
        print("\nAFTER contamination: NONE on valve/solenoid/starter/hose/level/transmitter/gasket/belting/chain/gearbox-oil.")

    # Detail: before contamination rows (what the baseline showed)
    print("\nBEFORE contamination rows (the baseline signature):")
    for pid in vague_ids:
        b_contam = _contamination_flags(_candidates_for_part(before, pid), pid)
        for c in b_contam:
            print(f"  [{pid}] {c['kind']} {c['vendor']} suit={c['suitability']} {c['url']}")

    # Also report wrongness_flags delta (any-class mismatch, the harness's own
    # post-hoc detector — broader than the motor/pump contamination check).
    print("\nWRONGNESS FLAGS delta (harness post-hoc; any noun-class mismatch + above floor):")
    for pid in vague_ids:
        b_wf = _wrongness_for_part(before, pid)
        a_wf = _wrongness_for_part(after, pid)
        if b_wf or a_wf:
            print(f"  {pid}: before={len(b_wf)} after={len(a_wf)}")
            for f in a_wf:
                print(f"    AFTER  {f.get('vendor')} query={f.get('query_class')} result={f.get('result_class')} suit={f.get('suitability')} {f.get('url')}")
    print("=" * 100)
    return 0 if not any_after_contam else 1


if __name__ == "__main__":
    raise SystemExit(main())
