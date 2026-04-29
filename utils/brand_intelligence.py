"""
utils/brand_intelligence.py — Phase 3.1
SQLite-cached, LLM-discovered manufacturer relationship graph.

Replaces four hardcoded dictionaries in sourcing.py:
  RECOGNIZED_PARENT_BRANDS  -> get_brand_relationships() -> parent_company
  _EQUIP_COMPETITORS         -> get_competitors()
  _NICHE_WRONG_TERMS         -> get_wrong_category_terms()
  _TIER2_NICHE_TERMS         -> get_subcategory_refinement()

Cache key: (manufacturer.lower(), equipment_type.lower())
TTL: 90 days — stale records are re-discovered on next access.
"""

import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATA_DIR  = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH   = os.path.join(_DATA_DIR, "brand_intelligence.sqlite")
_TTL_DAYS  = 90

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY")
_INTEL_MODEL       = os.environ.get("BRAND_INTEL_MODEL", "claude-haiku-4-5-20251001")

# ---------------------------------------------------------------------------
# LLM Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an industrial procurement intelligence assistant.
Given a manufacturer name and equipment type, return a JSON object with these exact keys:

{
  "parent_company": string or null,
  "subsidiaries": [list of string brand names],
  "authorized_service_brands": [brands authorized to sell/service this manufacturer's equipment],
  "common_competitors": [top 5-8 competing brands for the same equipment category],
  "subcategory_niche_terms": [3-6 short phrases that identify specialist distributors for this equipment type — used in web search queries],
  "wrong_category_terms": [4-8 terms that would indicate the wrong niche — e.g. if searching for pumps, wrong-niche terms include "motor rewind", "hydraulic cylinder"]
}

Rules:
- parent_company: the parent corporation that owns this brand (e.g. Gusher -> Ruthman Companies).
  Use the dominant name fragment that appears in distributor snippets (e.g. "ruthman").
  null if the brand is already the parent.
- subsidiaries: sibling or child brands owned by the same parent (not competitors).
- authorized_service_brands: brands explicitly authorized by the manufacturer as service partners.
  Empty list if unknown.
- common_competitors: direct competing manufacturers in the same equipment category and tier.
  Focus on brands that list Add-to-Cart prices online (useful for price discovery).
- subcategory_niche_terms: short phrases for Tavily queries to surface specialist distributors,
  e.g. ["industrial pump distributor authorized service center", "centrifugal pump repair"].
- wrong_category_terms: terms that indicate the search result is from the wrong industry niche,
  e.g. for pumps: ["motor rewind", "motor winding", "hydraulic cylinder", "electrical panel"].

Return ONLY valid JSON. No markdown fences, no explanation text."""


_USER_TEMPLATE = "Manufacturer: {manufacturer}\nEquipment type: {equipment_type}"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS brand_intelligence (
    id                       TEXT PRIMARY KEY,
    manufacturer             TEXT NOT NULL,
    equipment_type           TEXT NOT NULL,
    parent_company           TEXT,
    subsidiaries             TEXT DEFAULT '[]',
    authorized_service_brands TEXT DEFAULT '[]',
    common_competitors       TEXT DEFAULT '[]',
    subcategory_niche_terms  TEXT DEFAULT '[]',
    wrong_category_terms     TEXT DEFAULT '[]',
    discovered_at            TEXT NOT NULL,
    last_accessed_at         TEXT NOT NULL,
    ttl_days                 INTEGER DEFAULT 90,
    llm_model_used           TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_brand_intel_key
    ON brand_intelligence(manufacturer, equipment_type);
"""


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# LLM Discovery
# ---------------------------------------------------------------------------

def _discover_via_llm(manufacturer: str, equipment_type: str) -> Optional[dict]:
    """Call Claude to discover brand relationships. Returns parsed dict or None on failure."""
    if not ANTHROPIC_API_KEY:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _INTEL_MODEL,
                "max_tokens": 800,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content":
                    _USER_TEMPLATE.format(manufacturer=manufacturer, equipment_type=equipment_type)}],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        # Track LLM cost (silent fail)
        try:
            from utils.llm_tracker import record_call as _llm_rec
            _u = data.get("usage", {})
            _llm_rec(_u.get("input_tokens", 0), _u.get("output_tokens", 0))
        except Exception:
            pass

        raw = data["content"][0]["text"].strip()
        # Strip markdown fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as exc:
        print(f"[BrandIntel] LLM discovery failed for {manufacturer!r}/{equipment_type!r}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Cache CRUD
# ---------------------------------------------------------------------------

def _is_stale(last_accessed_at: str, ttl_days: int) -> bool:
    try:
        ts = datetime.fromisoformat(last_accessed_at)
        return datetime.utcnow() - ts > timedelta(days=ttl_days)
    except Exception:
        return True


def _upsert(conn: sqlite3.Connection, manufacturer: str, equipment_type: str,
            payload: dict, model_used: str) -> None:
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO brand_intelligence
            (id, manufacturer, equipment_type, parent_company, subsidiaries,
             authorized_service_brands, common_competitors, subcategory_niche_terms,
             wrong_category_terms, discovered_at, last_accessed_at, ttl_days, llm_model_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(manufacturer, equipment_type) DO UPDATE SET
            parent_company            = excluded.parent_company,
            subsidiaries              = excluded.subsidiaries,
            authorized_service_brands = excluded.authorized_service_brands,
            common_competitors        = excluded.common_competitors,
            subcategory_niche_terms   = excluded.subcategory_niche_terms,
            wrong_category_terms      = excluded.wrong_category_terms,
            last_accessed_at          = excluded.last_accessed_at,
            llm_model_used            = excluded.llm_model_used
    """, (
        str(uuid.uuid4()),
        manufacturer.lower().strip(),
        equipment_type.lower().strip(),
        payload.get("parent_company"),
        json.dumps(payload.get("subsidiaries") or []),
        json.dumps(payload.get("authorized_service_brands") or []),
        json.dumps(payload.get("common_competitors") or []),
        json.dumps(payload.get("subcategory_niche_terms") or []),
        json.dumps(payload.get("wrong_category_terms") or []),
        now, now, _TTL_DAYS, model_used,
    ))
    conn.commit()


def _touch(conn: sqlite3.Connection, manufacturer: str, equipment_type: str) -> None:
    """Update last_accessed_at without changing data."""
    conn.execute(
        "UPDATE brand_intelligence SET last_accessed_at = ? WHERE manufacturer = ? AND equipment_type = ?",
        (datetime.utcnow().isoformat(), manufacturer.lower().strip(), equipment_type.lower().strip()),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_brand_relationships(manufacturer: str,
                             equipment_type: str = "general") -> dict:
    """Return cached or freshly-discovered brand relationship record.

    Always returns a dict — keys may be empty/null on LLM failure.
    Graceful fallback: never raises; returns empty structure on any error.

    Return shape:
    {
      "manufacturer": str,
      "equipment_type": str,
      "parent_company": str | None,
      "subsidiaries": list[str],
      "authorized_service_brands": list[str],
      "common_competitors": list[str],
      "subcategory_niche_terms": list[str],
      "wrong_category_terms": list[str],
      "from_cache": bool,
    }
    """
    mfg  = (manufacturer or "").lower().strip()
    etype = (equipment_type or "general").lower().strip()

    _empty = {
        "manufacturer": mfg, "equipment_type": etype,
        "parent_company": None, "subsidiaries": [],
        "authorized_service_brands": [], "common_competitors": [],
        "subcategory_niche_terms": [], "wrong_category_terms": [],
        "from_cache": False,
    }

    if not mfg or mfg in ("unknown", "n/a", "null", "none"):
        return _empty

    try:
        conn = _get_conn()
        row  = conn.execute(
            "SELECT * FROM brand_intelligence WHERE manufacturer = ? AND equipment_type = ?",
            (mfg, etype),
        ).fetchone()

        if row and not _is_stale(row["last_accessed_at"], row["ttl_days"]):
            _touch(conn, mfg, etype)
            return {
                "manufacturer": mfg, "equipment_type": etype,
                "parent_company": row["parent_company"],
                "subsidiaries": json.loads(row["subsidiaries"] or "[]"),
                "authorized_service_brands": json.loads(row["authorized_service_brands"] or "[]"),
                "common_competitors": json.loads(row["common_competitors"] or "[]"),
                "subcategory_niche_terms": json.loads(row["subcategory_niche_terms"] or "[]"),
                "wrong_category_terms": json.loads(row["wrong_category_terms"] or "[]"),
                "from_cache": True,
            }

        # Cache miss or stale — discover via LLM
        print(f"[BrandIntel] Discovering relationships: {manufacturer!r} / {equipment_type!r}")
        payload = _discover_via_llm(manufacturer, equipment_type)
        if payload:
            _upsert(conn, mfg, etype, payload, _INTEL_MODEL)
            return {
                "manufacturer": mfg, "equipment_type": etype,
                "parent_company": payload.get("parent_company"),
                "subsidiaries": payload.get("subsidiaries") or [],
                "authorized_service_brands": payload.get("authorized_service_brands") or [],
                "common_competitors": payload.get("competitors") or payload.get("common_competitors") or [],
                "subcategory_niche_terms": payload.get("subcategory_niche_terms") or [],
                "wrong_category_terms": payload.get("wrong_category_terms") or [],
                "from_cache": False,
            }
    except Exception as exc:
        print(f"[BrandIntel] Error: {exc}")

    return _empty


def get_competitors(manufacturer: str, equipment_type: str = "general") -> list[str]:
    """Return list of competing brand names (empty list on failure)."""
    return get_brand_relationships(manufacturer, equipment_type).get("common_competitors", [])


def get_subcategory_refinement(manufacturer: str, equipment_type: str) -> Optional[str]:
    """Return the primary niche search term for Tier 2 Tavily queries, or None."""
    terms = get_brand_relationships(manufacturer, equipment_type).get("subcategory_niche_terms", [])
    return terms[0] if terms else None


def get_wrong_category_terms(manufacturer: str, equipment_type: str) -> tuple[str, ...]:
    """Return wrong-category exclusion terms as a tuple (for suitability scoring)."""
    terms = get_brand_relationships(manufacturer, equipment_type).get("wrong_category_terms", [])
    return tuple(terms)


def get_parent_brand(manufacturer: str, equipment_type: str = "general") -> Optional[str]:
    """Return parent company name fragment (lowercase) or None."""
    return get_brand_relationships(manufacturer, equipment_type).get("parent_company")


# ---------------------------------------------------------------------------
# Bulk cache warm-up (for CLI refresh script)
# ---------------------------------------------------------------------------

def warm_cache(pairs: list[tuple[str, str]]) -> list[dict]:
    """Discover and cache brand relationships for a list of (manufacturer, equipment_type) pairs.

    Returns list of result dicts. Skips pairs already cached and fresh.
    """
    results = []
    for mfg, etype in pairs:
        r = get_brand_relationships(mfg, etype)
        results.append(r)
    return results


def all_cached_entries() -> list[dict]:
    """Return all records in the brand_intelligence cache (for CLI display)."""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT * FROM brand_intelligence ORDER BY last_accessed_at DESC"
        ).fetchall()
        out = []
        for r in rows:
            out.append({
                "manufacturer":             r["manufacturer"],
                "equipment_type":           r["equipment_type"],
                "parent_company":           r["parent_company"],
                "subsidiaries":             json.loads(r["subsidiaries"] or "[]"),
                "authorized_service_brands": json.loads(r["authorized_service_brands"] or "[]"),
                "common_competitors":        json.loads(r["common_competitors"] or "[]"),
                "subcategory_niche_terms":   json.loads(r["subcategory_niche_terms"] or "[]"),
                "wrong_category_terms":      json.loads(r["wrong_category_terms"] or "[]"),
                "discovered_at":             r["discovered_at"],
                "last_accessed_at":          r["last_accessed_at"],
                "ttl_days":                  r["ttl_days"],
                "llm_model_used":            r["llm_model_used"],
            })
        return out
    except Exception as exc:
        print(f"[BrandIntel] all_cached_entries error: {exc}")
        return []


def invalidate(manufacturer: str, equipment_type: str) -> bool:
    """Force re-discovery by setting last_accessed_at far in the past."""
    try:
        conn = _get_conn()
        conn.execute(
            "UPDATE brand_intelligence SET last_accessed_at = '2000-01-01T00:00:00' "
            "WHERE manufacturer = ? AND equipment_type = ?",
            (manufacturer.lower().strip(), equipment_type.lower().strip()),
        )
        conn.commit()
        return conn.total_changes > 0
    except Exception:
        return False
