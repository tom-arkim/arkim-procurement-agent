"""Batch sourcing harness — drive the REAL backend API in a loop over test parts.

COST WARNING — READ BEFORE RUNNING.
===================================

This script is NOT a unit test. It drives the live Arkim backend API
(`uvicorn api_server:app --port 8001`), which makes REAL Anthropic LLM calls
(intake extraction + clarification) and REAL Tavily web-search calls per part.
Every part costs money (LLM + ~6 Tavily searches + comparison LLMs). A 12-part
run is roughly a dozen intake-extraction calls + 70+ Tavily queries + comparison
calls — trivial in absolute terms but NONZERO. Never run this in the pytest
suite (it is excluded by testpaths and is not collected). Run it ONLY against a
local backend you started deliberately, with your keys in `.env`.

It is read-only against the backend API (POST /api/runs, /messages,
/confirm-intake, GET /api/runs/{id}, GET /api/health) — no direct DB writes.
The only direct store access is READING `data/run_capture.sqlite` (when
RUN_CAPTURE=1) for the flywheel-report section.

Usage:
    # Start the backend first (Terminal A), with the flag on to exercise capture:
    #   $env:RUN_CAPTURE = "1"; uvicorn api_server:app --port 8001
    # Then (Terminal B):
    uv run python scripts/batch_sourcing_harness.py
    uv run python scripts/batch_sourcing_harness.py --parts 3
    uv run python scripts/batch_sourcing_harness.py --dry-run
    uv run python scripts/batch_sourcing_harness.py --base-url http://localhost:8001
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

# Make the repo root importable so `utils.*` resolves when run via uv.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_FIXTURE = _REPO_ROOT / "scripts" / "fixtures" / "harness_parts.json"
_RESULTS_DIR = _REPO_ROOT / "scripts"

# Intake reply handling
_MAX_CLARIFY_TURNS = 2          # cap on generic follow-up answers before forcing confirm
_GENERIC_ANSWER = "standard configuration, quantity 1"
_INTAKE_REPLY_TIMEOUT = 60      # per intake LLM call (the API itself proxies Anthropic)
_SOURCING_POLL_TIMEOUT = 180    # total wait for sourcing to complete
_POLL_INTERVAL = 2.0
_BETWEEN_PARTS_SLEEP = 5.0      # be kind to Tavily

# Phases that mean sourcing is done (stop polling).
sourcing_done_phases = {"comparison", "error", "cancelled", "pending_first_approval"}


# ---------------------------------------------------------------------------
# Backend client
# ---------------------------------------------------------------------------

class Backend:
    """Thin client over the real api_server REST surface (the frontend's calls)."""

    def __init__(self, base_url: str, demo_session_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.s = requests.Session()
        self.demo_session_id = demo_session_id
        if demo_session_id:
            self.s.headers["X-Session-Id"] = demo_session_id

    def health(self) -> Dict[str, Any]:
        r = self.s.get(f"{self.base_url}/api/health", timeout=10)
        r.raise_for_status()
        return r.json()

    def create_run(self, facility_id: str = "00000000-0000-0000-0000-000000000000") -> Dict[str, Any]:
        r = self.s.post(f"{self.base_url}/api/runs", json={"facility_id": facility_id}, timeout=15)
        r.raise_for_status()
        return r.json()

    def send_message(self, run_id: str, content: str) -> Dict[str, Any]:
        r = self.s.post(f"{self.base_url}/api/runs/{run_id}/messages",
                        json={"content": content}, timeout=_INTAKE_REPLY_TIMEOUT)
        r.raise_for_status()
        return r.json()

    def confirm_intake(self, run_id: str) -> Dict[str, Any]:
        r = self.s.post(f"{self.base_url}/api/runs/{run_id}/confirm-intake", timeout=15)
        r.raise_for_status()
        return r.json()

    def get_run(self, run_id: str) -> Dict[str, Any]:
        r = self.s.get(f"{self.base_url}/api/runs/{run_id}", timeout=15)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Per-part execution
# ---------------------------------------------------------------------------

def _identified_as(detail: Dict[str, Any]) -> str:
    """Best-effort 'identified as' string from the run detail (specs + detected_type)."""
    specs = detail.get("asset_specs") or {}
    parts = [specs.get("manufacturer"), specs.get("model"), specs.get("part_number")]
    ident = " ".join(p for p in parts if p) or specs.get("detected_type") or "(unidentified)"
    return ident.strip()


def _collect_candidates(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten sourcing_results.tier1/2/3 into a per-candidate row list."""
    sr = detail.get("sourcing_results") or {}
    out = []
    for tier_key in ("tier1", "tier2", "tier3"):
        for c in sr.get(tier_key) or []:
            out.append({
                "tier":           c.get("tier") or int(tier_key[-1]),
                "vendor":         c.get("vendorName") or "Unknown",
                "price":          c.get("price"),
                "suitability":    c.get("suitability"),
                "confidence":     c.get("confidence"),
                "match_type":     c.get("pnMatchLevel") or c.get("matchType"),
                "url":            c.get("url") or "",
                "rejected":       False,  # the API response already filters rejects out
                "rejection_reason": None,
            })
    return out


def run_one_part(backend: Backend, part: Dict[str, Any]) -> Dict[str, Any]:
    """Drive one part through the real API (exactly the frontend's calls)."""
    t0 = time.time()
    record: Dict[str, Any] = {"part_id": part["id"], "desc": part["desc"], "expect": part.get("expect"),
                              "run_id": None, "identified_as": None, "duration_s": None,
                              "phase": None, "candidates": [], "intake_turns": [],
                              "error": None}
    try:
        cr = backend.create_run()
        run_id = cr["id"]
        record["run_id"] = run_id

        # 1. Initial description → intake
        reply = backend.send_message(run_id, part["desc"])
        record["intake_turns"].append({"user": part["desc"], "agent": reply["message"]["content"]})

        # 2. If intake asks a clarifying question, answer generically (cap 2 turns).
        turns = 0
        while reply.get("proceed_state") in (None, "", "needs_clarification") and turns < _MAX_CLARIFY_TURNS:
            # A follow-up question means the agent replied with a question (not "specs look complete").
            agent_text = (reply.get("message") or {}).get("content") or ""
            if any(phrase in agent_text.lower() for phrase in
                   ("specs look complete", "review in the panel", "sourcing by category", "confirm to start")):
                break  # intake is satisfied; stop feeding generic answers
            ans = backend.send_message(run_id, _GENERIC_ANSWER)
            record["intake_turns"].append({"user": _GENERIC_ANSWER, "agent": ans["message"]["content"]})
            reply = ans
            turns += 1

        # 3. Confirm intake → kicks off background sourcing
        backend.confirm_intake(run_id)

        # 4. Poll until sourcing completes (comparison/error) or timeout
        deadline = time.time() + _SOURCING_POLL_TIMEOUT
        detail = backend.get_run(run_id)
        while time.time() < deadline:
            phase = detail.get("phase")
            if phase in sourcing_done_phases:
                break
            time.sleep(_POLL_INTERVAL)
            detail = backend.get_run(run_id)

        record["phase"] = detail.get("phase")
        record["identified_as"] = _identified_as(detail)
        record["candidates"] = _collect_candidates(detail)
        record["duration_s"] = round(time.time() - t0, 1)
        if detail.get("phase") == "error":
            record["error"] = "sourcing errored (phase=error)"
        elif detail.get("phase") not in sourcing_done_phases:
            record["error"] = f"timeout after {_SOURCING_POLL_TIMEOUT}s (phase={detail.get('phase')})"
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["duration_s"] = round(time.time() - t0, 1)
    return record


# ---------------------------------------------------------------------------
# Post-hoc wrongness analysis (uses part_type_classes classifiers on recorded data
# only — NO scorer changes, NO live calls)
# ---------------------------------------------------------------------------

def _query_noun_class(desc: str) -> Optional[str]:
    from utils.sourcing_archieved.part_type_classes import classify_noun_class
    return classify_noun_class(desc)


def _result_noun_class(candidate: Dict[str, Any]) -> Optional[str]:
    from utils.sourcing_archieved.part_type_classes import classify_result_noun_class
    # The candidate carries the vendor name + url; title isn't in the API response
    # (the frontend card doesn't need it), so classify off vendor + url.
    return classify_result_noun_class(candidate.get("vendor") or "", candidate.get("url") or "")


def wrongness_flags(record: Dict[str, Any], floor: float = 30.0) -> List[Dict[str, Any]]:
    """Flag displayed candidates whose noun-class ≠ query noun-class AND scored above the floor."""
    q_cls = _query_noun_class(record["desc"])
    flags = []
    if q_cls is None:
        return flags  # query noun undetectable — no cross-class flag is meaningful
    for c in record["candidates"]:
        if (c.get("suitability") or 0) < floor:
            continue
        r_cls = _result_noun_class(c)
        if r_cls is not None and r_cls != q_cls:
            flags.append({
                "vendor": c.get("vendor"),
                "url": c.get("url"),
                "query_class": q_cls,
                "result_class": r_cls,
                "suitability": c.get("suitability"),
            })
    return flags


# ---------------------------------------------------------------------------
# RUN_CAPTURE flywheel report (only when the flag is on in the backend env)
# ---------------------------------------------------------------------------

def capture_report(run_ids: List[str]) -> Dict[str, Any]:
    """Read run_capture.sqlite directly (read-only) and summarize per-run events."""
    try:
        from utils import run_capture as rc
    except Exception as exc:
        return {"available": False, "error": f"import failed: {exc}"}
    db = rc._DB_PATH
    if not os.path.exists(db):
        return {"available": False, "error": f"{db} not present (RUN_CAPTURE off on the backend?)"}
    per_run = {}
    for rid in run_ids:
        events = rc.read_events(rid) if rid else []
        counts: Dict[str, int] = {}
        for e in events:
            counts[e["event_type"]] = counts.get(e["event_type"], 0) + 1
        outcome = rc.compute_outcome(rid) if rid else "n/a"
        per_run[rid] = {"event_counts": counts, "outcome": outcome,
                        "has_turn": "turn_user" in counts and "turn_agent" in counts,
                        "has_intake_result": "intake_result" in counts,
                        "has_candidate": ("candidate_scored" in counts or "candidate_rejected" in counts),
                        "has_displayed": "results_displayed" in counts}
    return {"available": True, "db": db, "per_run": per_run}


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _top_candidate(cands: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not cands:
        return {}
    return max(cands, key=lambda c: (c.get("suitability") or 0))


def print_summary(records: List[Dict[str, Any]], cap: Dict[str, Any]) -> None:
    print("\n" + "=" * 110)
    print("BATCH SOURCING HARNESS - SUMMARY")
    print("=" * 110)
    hdr = f"{'part':<26}{'identified-as':<34}{'#disp':>5}{'#rej':>5}{'top vendor':<26}{'suit%':>6}{'dur':>6}"
    print(hdr)
    print("-" * 110)
    for r in records:
        if r.get("error") and not r["candidates"]:
            print(f"{r['part_id'][:25]:<26}{('(error) ' + r['error'][:30]):<34}{'-':>5}{'-':>5}{'-':<26}{'-':>6}{r.get('duration_s','-'):>6}")
            continue
        disp = [c for c in r["candidates"] if not c["rejected"]]
        rej = [c for c in r["candidates"] if c["rejected"]]
        top = _top_candidate(disp)
        ident = (r.get("identified_as") or "")[:33]
        print(f"{r['part_id'][:25]:<26}{ident:<34}{len(disp):>5}{len(rej):>5}"
              f"{(top.get('vendor') or '-')[:25]:<26}{(top.get('suitability') or 0):>6.0f}{r.get('duration_s','-'):>6}")
    print("-" * 110)

    # Wrongness flags
    print("\nWRONGNESS FLAGS (noun-class mismatch + above floor; post-hoc, no scorer changes):")
    any_flag = False
    for r in records:
        flags = wrongness_flags(r)
        if flags:
            any_flag = True
            for f in flags:
                print(f"  [{r['part_id']}] {f['vendor']} suit={f['suitability']:.0f}% "
                      f"query={f['query_class']} result={f['result_class']}  {f['url']}")
    if not any_flag:
        print("  (none)")

    # Capture section
    print("\nRUN_CAPTURE FLYWHEEL:")
    if not cap.get("available"):
        print(f"  not available - {cap.get('error')}")
    else:
        print(f"  db: {cap['db']}")
        for rid, info in cap["per_run"].items():
            ok = info["has_turn"] and info["has_intake_result"] and info["has_candidate"] and info["has_displayed"]
            mark = "OK " if ok else "GAP"
            print(f"  [{mark}] {rid[:8]}  outcome={info['outcome']:<26}  events={info['event_counts']}")
            if not ok:
                missing = [k for k, v in [("turn_user/turn_agent", info["has_turn"]),
                                          ("intake_result", info["has_intake_result"]),
                                          ("candidate_*", info["has_candidate"]),
                                          ("results_displayed", info["has_displayed"])] if not v]
                print(f"         MISSING: {missing}")
    print("=" * 110)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Batch sourcing harness (live, costs money).")
    ap.add_argument("--base-url", default="http://localhost:8001")
    ap.add_argument("--parts", type=int, default=0, help="run only the first N parts (0 = all)")
    ap.add_argument("--dry-run", action="store_true", help="hit /api/health only, then exit")
    ap.add_argument("--fixture", default=str(_FIXTURE))
    ap.add_argument("--no-sleep", action="store_true", help="skip the between-parts sleep (still kind: use sparingly)")
    args = ap.parse_args()

    parts: List[Dict[str, Any]] = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    if args.parts > 0:
        parts = parts[:args.parts]

    # Health check first (also tells us demo_mode + capture state).
    probe_session = str(uuid.uuid4())
    backend = Backend(args.base_url, demo_session_id=probe_session)
    try:
        health = backend.health()
    except Exception as exc:
        print(f"ERROR: cannot reach backend at {args.base_url} ({exc}). "
              f"Start it with: uvicorn api_server:app --port 8001", file=sys.stderr)
        return 2

    demo_mode = bool(health.get("demo_mode"))
    capture_on = "capture_failures" in health
    print(f"backend: {args.base_url}  demo_mode={demo_mode}  RUN_CAPTURE={'ON' if capture_on else 'OFF'}"
          f"  capture_failures={health.get('capture_failures')}")
    print(f"COST WARNING: this makes real LLM + Tavily calls per part. "
          f"{'Dry run - health only.' if args.dry_run else f'Running {len(parts)} part(s).'}")

    if args.dry_run:
        print(json.dumps(health, indent=2))
        return 0

    # DEMO_MODE requires a stable X-Session-Id for the run-creation + scoping gates.
    # (A random per-process UUID is fine — the harness is one "session".)
    session_id = probe_session if demo_mode else None
    backend = Backend(args.base_url, demo_session_id=session_id)

    records: List[Dict[str, Any]] = []
    run_ids: List[str] = []
    for i, part in enumerate(parts):
        print(f"\n[{i+1}/{len(parts)}] {part['id']}: {part['desc']}")
        rec = run_one_part(backend, part)
        records.append(rec)
        if rec["run_id"]:
            run_ids.append(rec["run_id"])
        print(f"   -> run {rec['run_id'][:8] if rec['run_id'] else '-'}  phase={rec['phase']}  "
              f"candidates={len(rec['candidates'])}  dur={rec['duration_s']}s"
              + (f"  ERROR: {rec['error']}" if rec.get("error") else ""))
        if i < len(parts) - 1 and not args.no_sleep:
            time.sleep(_BETWEEN_PARTS_SLEEP)

    # Capture report (only meaningful if the backend was started with RUN_CAPTURE=1).
    cap = capture_report(run_ids) if capture_on else {"available": False, "error": "RUN_CAPTURE off on backend"}

    # Write full data
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = _RESULTS_DIR / f"harness_results_{ts}.json"
    out_path.write_text(json.dumps({
        "generated_at": ts,
        "base_url": args.base_url,
        "demo_mode": demo_mode,
        "run_capture_on": capture_on,
        "health": health,
        "capture_report": cap,
        "records": records,
        "wrongness_flags": {r["part_id"]: wrongness_flags(r) for r in records},
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {out_path}")

    print_summary(records, cap)

    # Assert (soft) that every run produced the core capture events when the flag was on.
    if cap.get("available"):
        gaps = [rid for rid, info in cap["per_run"].items()
                if not (info["has_turn"] and info["has_intake_result"]
                        and info["has_candidate"] and info["has_displayed"])]
        if gaps:
            print(f"\n[!] {len(gaps)} run(s) missing core capture events: {[g[:8] for g in gaps]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
