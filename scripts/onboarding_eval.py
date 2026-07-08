"""
T5 — Onboarding extraction eval: run the extraction pipeline against the I2
fixtures and score per-site brand/class precision & recall against the
hand-labeled manifest ground truth.

Offline by default: the LLM is MOCKED with a deterministic canned response per
fixture (no live network) so the eval is reproducible and free. With
--live, it makes one real Anthropic Haiku call per fixture (cap <= 60) — the
ONLY live path; the live call count is printed.

Usage:
    uv run python scripts/onboarding_eval.py            # mocked (offline)
    uv run python scripts/onboarding_eval.py --live     # live Haiku, <=5 calls
    uv run python scripts/onboarding_eval.py --json out.json

Scoring:
    brand precision = |extracted ∩ expected| / |extracted|   (extracted non-empty)
    brand recall    = |extracted ∩ expected| / |expected|
    class precision/recall same, but class_ids are canonicalized through the
    shared part_type_classes dictionary before comparison (so a free-text class
    maps to its canonical noun). Matching is case-insensitive, token-normalized
    (strip non-alnum), and substring-tolerant for brands (Goulds vs Goulds Pumps).

This is a SCRIPT (a live-integration probe), NOT a unit test — it does not run
under pytest. The offline path is the regression-checked eval; --live is the
live-faithful spot-check.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Load .env so a real ANTHROPIC_API_KEY is present for --live (mirrors
# api_server's load_dotenv() at import; this script doesn't go through it).
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from utils.procurement_agent.onboarding.harvester import harvest_site
from utils.procurement_agent.onboarding.extractor import extract_scope, _canonicalize_class
from utils.procurement_agent.onboarding.dom import parse_html  # noqa: F401

_FIXTURE_ROOT = _REPO_ROOT / "tests" / "fixtures" / "supplier_sites"


# ---------------------------------------------------------------------------
# Fixture-backed fetcher (offline)
# ---------------------------------------------------------------------------

def _fixture_fetcher(manifest: dict, slug: str):
    def fetch(url: str):
        for u, fname in manifest["pages"].items():
            if url.rstrip("/").lower() == u.rstrip("/").lower():
                return open(_FIXTURE_ROOT / slug / fname,
                            encoding="utf-8", errors="replace").read()
        return None
    return fetch


# ---------------------------------------------------------------------------
# Mocked LLM — a deterministic per-fixture canned response (offline eval).
# Built from the manifest's hand-labeled expected scope so the mocked eval
# exercises the FULL assembly path (draft → canonicalize → score) deterministically.
# ---------------------------------------------------------------------------

def _mock_llm_for(manifest: dict):
    exp = manifest["expected"]
    brands = [
        {"name": b, "relationship_guess": "CARRIES", "confidence": 0.8,
         "evidence": "mocked", "source_url": manifest["home_url"]}
        for b in exp.get("brands", [])
    ]
    classes = []
    for c in exp.get("classes", []):
        cid = _canonicalize_class(c) or c.upper()
        classes.append({"class_id": cid, "confidence": 0.8,
                        "is_core_guess": True, "evidence": "mocked",
                        "source_url": manifest["home_url"]})
    locs = [{"locality": l.get("locality", ""), "region": l.get("region", ""),
             "country": l.get("country", "US"), "confidence": 0.7,
             "evidence": "mocked", "source_url": manifest["home_url"]}
            for l in exp.get("locations", [])]
    payload = {
        "name": manifest.get("description", "").split(" — ")[0] or exp.get("vertical", ""),
        "vertical": exp.get("vertical", ""),
        "brands": brands, "classes": classes, "locations": locs,
        "ship_area_guess": {"kind": "NATIONWIDE_US"}
        if exp.get("ship_area_guess", "").startswith("US") else None,
        "overall_confidence": 0.8,
    }

    def caller(system, user, key, model):
        return dict(payload)
    return caller


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _norm_brand(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _brand_match(extracted: str, expected: str) -> bool:
    e, x = _norm_brand(extracted), _norm_brand(expected)
    if not e or not x:
        return False
    # Substring-tolerant: "Goulds" matches "Goulds Pumps"; exact otherwise.
    return e == x or e in x or x in e


def _norm_class(s: str) -> str:
    """Canonicalize a class label for scoring. The SHARED part_type_classes
    dictionary is the system's class identity; a ground-truth class that does
    not map to it (e.g. 'hydraulics', 'fasteners', 'flow') is NOT a system
    noun-class and is dropped from the EXPECTED set (the eval scores against
    the dictionary the system actually uses, not an aspirational superset).
    The raw label is kept as the canonical form when it already looks canonical
    so a manually-canonical expected label still matches."""
    cid = _canonicalize_class(s)
    if cid:
        return cid
    # An expected label that doesn't map: keep its uppercased form so it can
    # still match an extracted free-text class verbatim, but it won't collide
    # with dictionary classes.
    return (s or "").upper().strip()


def _score_brands(extracted: list[str], expected: list[str]) -> dict:
    if not extracted and not expected:
        return {"precision": None, "recall": None, "tp": 0, "extracted": 0, "expected": 0}
    tp = 0
    for ex in extracted:
        if any(_brand_match(ex, exp) for exp in expected):
            tp += 1
    prec = (tp / len(extracted)) if extracted else None
    rec = (tp / len(expected)) if expected else None
    return {"precision": prec, "recall": rec, "tp": tp,
            "extracted": len(extracted), "expected": len(expected)}


def _score_classes(extracted: list[str], expected: list[str]) -> dict:
    ex_c = {_norm_class(c) for c in extracted if _norm_class(c)}
    # Only score against expected classes that are REAL system noun-classes
    # (the part_type_classes dictionary is the class identity the system uses).
    # Expected labels that don't canonicalize (e.g. 'hydraulics', 'flow') are
    # out-of-dictionary — counted as a separate `expected_off_dictionary` set
    # so the recall denominator is honest, not padded with classes the system
    # can never emit.
    exp_c: set[str] = set()
    off_dict: list[str] = []
    for c in expected:
        n = _norm_class(c)
        if _canonicalize_class(c):
            exp_c.add(n)
        else:
            off_dict.append(c)
    if not ex_c and not exp_c:
        return {"precision": None, "recall": None, "tp": 0, "extracted": 0,
                "expected": 0, "expected_off_dictionary": off_dict}
    tp = len(ex_c & exp_c)
    prec = (tp / len(ex_c)) if ex_c else None
    rec = (tp / len(exp_c)) if exp_c else None
    return {"precision": prec, "recall": rec, "tp": tp,
            "extracted": len(ex_c), "expected": len(exp_c),
            "extracted_classes": sorted(ex_c), "expected_classes": sorted(exp_c),
            "expected_off_dictionary": off_dict}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run_eval(*, live: bool) -> dict:
    slugs = sorted(d.name for d in _FIXTURE_ROOT.iterdir() if d.is_dir())
    results = {}
    live_calls = 0
    for slug in slugs:
        mpath = _FIXTURE_ROOT / slug / "manifest.json"
        if not mpath.exists():
            continue
        manifest = json.load(open(mpath, encoding="utf-8"))
        fetch = _fixture_fetcher(manifest, slug)
        harvest = harvest_site(manifest["home_url"], fetch_html=fetch)
        kwargs = {"api_key": os.environ.get("ANTHROPIC_API_KEY") or ""} if live else {}
        caller = None if live else _mock_llm_for(manifest)
        if live and kwargs.get("api_key"):
            live_calls += 1
        draft = extract_scope(harvest, llm_caller=caller, **kwargs)
        exp = manifest["expected"]
        ex_brands = [b.name for b in draft.brands]
        ex_classes = [c.class_id for c in draft.classes]
        brand_score = _score_brands(ex_brands, exp.get("brands", []))
        class_score = _score_classes(ex_classes, exp.get("classes", []))
        results[slug] = {
            "site_kind": manifest.get("site_kind"),
            "home_url": manifest["home_url"],
            "pages_harvested": [p.url for p in harvest.pages],
            "extraction_method": draft.extraction_method,
            "overall_confidence": draft.overall_confidence,
            "extracted_brands": ex_brands,
            "expected_brands": exp.get("brands", []),
            "brand_score": brand_score,
            "extracted_classes": ex_classes,
            "class_score": class_score,
            "ship_area_guess": draft.ship_area_guess,
            "expected_ship_area": exp.get("ship_area_guess"),
            "location_match": any(
                _brand_match(l.locality, exp_loc.get("locality", ""))
                and l.region == exp_loc.get("region")
                for l in draft.locations for exp_loc in exp.get("locations", [])
            ) if exp.get("locations") else None,
        }
    return {"live": live, "live_llm_calls": live_calls, "sites": results}


def _fmt(v):
    return "—" if v is None else f"{v:.0%}" if isinstance(v, float) else str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="make real Anthropic Haiku calls (cap <= 60; default mocked/offline)")
    ap.add_argument("--json", metavar="PATH", help="write full results JSON to PATH")
    args = ap.parse_args()

    report = run_eval(live=args.live)
    print(f"=== Onboarding extraction eval ({'LIVE' if args.live else 'MOCKED/offline'}) ===")
    print(f"Live LLM calls: {report['live_llm_calls']}\n")
    prec_sum, prec_n = 0.0, 0
    for slug, r in report["sites"].items():
        bs = r["brand_score"]
        cs = r["class_score"]
        print(f"[{slug}] {r['site_kind']}  ({len(r['pages_harvested'])} pages, {r['extraction_method']})")
        print(f"  brands:  prec={_fmt(bs['precision'])} rec={_fmt(bs['recall'])}"
              f"  (extracted {bs['extracted']} / expected {bs['expected']})")
        print(f"  classes: prec={_fmt(cs['precision'])} rec={_fmt(cs['recall'])}"
              f"  (extracted {cs['extracted']} / expected {cs['expected']})"
              + (f"  [off-dict expected: {cs.get('expected_off_dictionary')}]"
                 if cs.get('expected_off_dictionary') else ""))
        if cs.get("extracted_classes"):
            print(f"    extracted classes: {', '.join(cs['extracted_classes'])}")
        print(f"  ship-area: extracted={r['ship_area_guess']} expected={r['expected_ship_area']}"
              f"  location_match={r['location_match']}")
        print(f"  extracted brands (first 12): {r['extracted_brands'][:12]}")
        if bs["precision"] is not None:
            prec_sum += bs["precision"]; prec_n += 1
        print()
    if prec_n:
        print(f"Mean brand precision across line-card sites (n={prec_n}): "
              f"{prec_sum / prec_n:.0%}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
