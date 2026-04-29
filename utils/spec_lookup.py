"""
utils/spec_lookup.py — Phase 3.4
Equipment spec enrichment via LLM inference.

enrich_equipment_specs(specs: AssetSpecs) -> AssetSpecs
  Fills missing technical fields (voltage, rpm, frame, phase) by asking Claude
  to infer them from manufacturer + model. Results are cached in SQLite.

  Enriched fields are flagged so confidence_score can apply a -10 penalty:
    specs._enriched_fields = {"voltage", "rpm", ...}

Only applies to Equipment category; Parts are returned unchanged.
"""

import json
import os
import re
import sqlite3
import uuid
from copy import copy
from datetime import datetime
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_DATA_DIR   = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH    = os.path.join(_DATA_DIR, "spec_cache.sqlite")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_ENRICH_MODEL     = os.environ.get("SPEC_ENRICH_MODEL", "claude-haiku-4-5-20251001")

# Fields that spec enrichment may fill; all are strings in AssetSpecs
_ENRICHABLE_FIELDS = ("voltage", "rpm", "frame", "phase", "hp", "gpm", "psi", "detected_type")

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS spec_cache (
    id             TEXT PRIMARY KEY,
    manufacturer   TEXT NOT NULL,
    model          TEXT NOT NULL,
    enriched_json  TEXT NOT NULL,
    discovered_at  TEXT NOT NULL,
    llm_model_used TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spec_cache_key ON spec_cache(manufacturer, model);
"""

# ---------------------------------------------------------------------------
# LLM Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an industrial equipment database assistant.
Given a manufacturer name and model number/description, infer the most likely
technical specifications for that specific equipment.

Return ONLY a JSON object with any of these keys you can confidently infer.
Omit any key you cannot determine with reasonable confidence.

{
  "voltage":       "e.g. 208-230/460V, 120V, 24VDC",
  "phase":         "e.g. 3-phase, single-phase, DC",
  "rpm":           "e.g. 1800, 3600, 1200",
  "frame":         "NEMA frame code, e.g. 56, 143T, 182T",
  "hp":            "e.g. 1.5 HP, 5 HP, 0.5 HP",
  "gpm":           "flow rate for pumps, e.g. 15 GPM",
  "psi":           "pressure for pumps/compressors, e.g. 50 PSI",
  "detected_type": "specific equipment type, e.g. Vertical Multi-Stage Pump, TEFC Motor"
}

Rules:
- Only include specs that can be reasonably inferred from the model number or common configurations.
- Do NOT guess random values. If uncertain, omit the key.
- voltage: include full range if multi-voltage (e.g. "208-230/460V").
- hp: include unit, e.g. "2 HP" not just "2".
- Return {} if you cannot confidently infer any specs.
- Return ONLY valid JSON. No explanation, no markdown."""

_USER_TEMPLATE = "Manufacturer: {manufacturer}\nModel: {model}"

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_DDL)
    conn.commit()
    return conn


def _lookup_cache(manufacturer: str, model: str) -> Optional[dict]:
    try:
        conn = _get_conn()
        row  = conn.execute(
            "SELECT enriched_json FROM spec_cache WHERE manufacturer = ? AND model = ?",
            (manufacturer.lower().strip(), model.lower().strip()),
        ).fetchone()
        if row:
            return json.loads(row["enriched_json"])
    except Exception:
        pass
    return None


def _save_cache(manufacturer: str, model: str, enriched: dict) -> None:
    try:
        conn = _get_conn()
        conn.execute("""
            INSERT INTO spec_cache (id, manufacturer, model, enriched_json, discovered_at, llm_model_used)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(manufacturer, model) DO UPDATE SET
                enriched_json  = excluded.enriched_json,
                discovered_at  = excluded.discovered_at,
                llm_model_used = excluded.llm_model_used
        """, (
            str(uuid.uuid4()),
            manufacturer.lower().strip(),
            model.lower().strip(),
            json.dumps(enriched),
            datetime.utcnow().isoformat(),
            _ENRICH_MODEL,
        ))
        conn.commit()
    except Exception as exc:
        print(f"[SpecLookup] Cache save error: {exc}")


# ---------------------------------------------------------------------------
# LLM Inference
# ---------------------------------------------------------------------------


def _infer_specs(manufacturer: str, model: str) -> dict:
    """Call Claude to infer missing specs. Returns {} on failure."""
    if not ANTHROPIC_API_KEY:
        return {}
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": _ENRICH_MODEL,
                "max_tokens": 400,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content":
                    _USER_TEMPLATE.format(manufacturer=manufacturer, model=model)}],
            },
            timeout=20,
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
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        if isinstance(result, dict):
            return result
    except Exception as exc:
        print(f"[SpecLookup] LLM inference failed for {manufacturer!r}/{model!r}: {exc}")
    return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def enrich_equipment_specs(specs) -> object:
    """Fill missing technical fields in an AssetSpecs via LLM inference.

    Returns a shallow copy of specs with inferred fields applied.
    Only applies to Equipment category — Parts are returned unchanged.

    Side-effect: adds `_enriched_fields: set[str]` attribute to the returned
    object so callers (e.g. confidence_score) can apply a -10 penalty per field.
    The original `specs` object is never mutated.
    """
    if getattr(specs, "category", "Part") != "Equipment":
        return specs

    mfg   = (specs.manufacturer or "").strip()
    model = (specs.model or "").strip()

    if not mfg or mfg.lower() in ("unknown", "n/a", "null") or not model:
        return specs

    # Check fields that are already populated — only ask for missing ones
    missing = [f for f in _ENRICHABLE_FIELDS if not getattr(specs, f, None)]
    if not missing:
        return specs

    print(f"[SpecLookup] Enriching specs for {mfg} {model} (missing: {', '.join(missing)})")

    # Cache lookup first
    cached = _lookup_cache(mfg, model)
    if cached is None:
        cached = _infer_specs(mfg, model)
        if cached:
            _save_cache(mfg, model, cached)
            print(f"[SpecLookup] Inferred {list(cached.keys())} (cached)")
        else:
            print(f"[SpecLookup] No specs inferred — returning original")
            return specs
    else:
        print(f"[SpecLookup] Cache hit: {list(cached.keys())}")

    # Apply inferred fields to a shallow copy (never mutate the original)
    enriched     = copy(specs)
    filled_fields: set[str] = set()

    for field in _ENRICHABLE_FIELDS:
        if field in cached and not getattr(enriched, field, None):
            try:
                setattr(enriched, field, cached[field])
                filled_fields.add(field)
            except Exception:
                pass

    # Tag enriched fields so confidence_score can apply a penalty
    enriched._enriched_fields = filled_fields  # type: ignore[attr-defined]

    if filled_fields:
        print(f"[SpecLookup] Applied enriched fields: {filled_fields}")
    return enriched


def get_cached_spec_entries() -> list[dict]:
    """Return all records in the spec_cache (for diagnostics)."""
    try:
        conn = _get_conn()
        rows = conn.execute(
            "SELECT manufacturer, model, enriched_json, discovered_at, llm_model_used "
            "FROM spec_cache ORDER BY discovered_at DESC"
        ).fetchall()
        return [
            {
                "manufacturer":   r["manufacturer"],
                "model":          r["model"],
                "enriched":       json.loads(r["enriched_json"]),
                "discovered_at":  r["discovered_at"],
                "llm_model_used": r["llm_model_used"],
            }
            for r in rows
        ]
    except Exception:
        return []
