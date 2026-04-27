"""
Module A — Vision / Text Extraction
Uses Claude Sonnet to parse nameplate text into AssetSpecs.
Extracts category, detected_type, phase, GPM, PSI, Frame for equipment-equivalence searches.
"""

import os
import re
import json
import requests
from utils.models import AssetSpecs

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_VISION_MODEL     = os.environ.get("OS_VISION_MODEL", "claude-sonnet-4-6")

_SYSTEM = """You are an industrial equipment data extractor.
Given raw nameplate text or a text description of industrial equipment, extract all specifications.

Return ONLY valid JSON with these exact keys:
{
  "category":           "Part" or "Equipment",
  "detected_type":      string or null,
  "manufacturer":       string or null,
  "model":              string or null,
  "part_number":        string or null,
  "voltage":            string or null,
  "phase":              string or null,
  "hp":                 string or null,
  "rpm":                string or null,
  "serial_number":      string or null,
  "description":        string,
  "gpm":                string or null,
  "psi":                string or null,
  "frame":              string or null,
  "physical_magnitude": "parcel" | "heavy_parcel" | "LTL_freight"
}

Category rules:
- "Equipment" = full assembled unit: pump, motor, compressor, blower, drive unit, conveyor, fan.
- "Part"      = replacement component: bearing, seal, contactor, relay, starter, VFD, sensor, belt, coupling.

detected_type rules:
- A precise, searchable equipment description, e.g.:
    "Vertical Multi-Stage Centrifugal Pump", "3-Phase TEFC Induction Motor",
    "Variable Frequency Drive", "Horizontal Belt Conveyor", "Rotary Screw Air Compressor",
    "Magnetic Motor Starter", "Roller Bearing".
- For Parts, use the component name, e.g. "Magnetic Motor Starter", "Deep Groove Ball Bearing".
- Never use brand names in detected_type — it must be a generic searchable category.

Technical requirement rules:
- phase : electrical phase, e.g. "3-phase", "single-phase", "1-phase".
- gpm   : flow rate in gallons per minute (pumps). Include unit, e.g. "45 GPM".
- psi   : pressure rating (pumps, compressors). Include unit, e.g. "175 PSI".
- frame : NEMA frame designation, e.g. "56C", "182T", "213T".
- rpm   : motor shaft speed, e.g. "1750 RPM", "3450 RPM". Motors only; null for pumps/parts.
- Extract phase / gpm / psi / frame / rpm even when the brand is unknown or unreadable.
- Set any field to null if not found — do not invent values.
- For description: one concise line, e.g. "3 HP TEFC induction motor, 3-phase, 56C frame, 1750 RPM".

physical_magnitude rules (shipping size classification — required, never null):
- "LTL_freight"  : Motor > 10 HP, OR Pump > 15 HP, OR compressor/blower/conveyor of any size,
                   OR weight clearly > 100 lbs — requires truck/freight shipment.
- "heavy_parcel" : Motor 1-10 HP, or Pump ≤ 15 HP, or Parts > ~30 lbs (large bearings,
                   gearboxes, large VFDs, starters ≥ NEMA Size 4).
- "parcel"       : small Parts — relays, sensors, small bearings, belts, seals,
                   contactors, small VFDs/starters — ships standard ground.
- When in doubt for a motor or pump with unknown HP, default to "LTL_freight".
"""


def _sonnet_extract(text: str) -> dict:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": _VISION_MODEL,
            "max_tokens": 600,
            "system": _SYSTEM,
            "messages": [{"role": "user", "content": text}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    raw = resp.json()["content"][0]["text"].strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("No JSON object in Sonnet response")
    return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_specs(image_description: str) -> AssetSpecs:
    """Parse nameplate text into AssetSpecs using Claude Sonnet.
    Falls back to regex extraction if the API call fails."""
    print(f"\n[Vision] Sonnet extraction for: '{image_description[:80]}...'")
    try:
        d = _sonnet_extract(image_description)
        cat = d.get("category", "Part")
        if cat not in ("Part", "Equipment"):
            cat = "Part"
        print(
            f"[Vision] Extracted → [{cat}] {d.get('detected_type')} | "
            f"{d.get('manufacturer')} {d.get('model')} | PN: {d.get('part_number')} | "
            f"Phase: {d.get('phase')} | GPM: {d.get('gpm')} | PSI: {d.get('psi')}"
        )
        mag = d.get("physical_magnitude", "")
        if mag not in ("parcel", "heavy_parcel", "LTL_freight"):
            mag = "LTL_freight" if cat == "Equipment" else "parcel"

        rpm_val   = d.get("rpm")
        frame_val = d.get("frame")
        dtype_str = (d.get("detected_type") or "").lower()

        # Flag motors missing both Frame and RPM — these are critical for equivalence search
        _null_vals = {None, "", "null", "N/A", "Unknown"}
        missing_crit = (
            cat == "Equipment"
            and "motor" in dtype_str
            and (frame_val in _null_vals)
            and (rpm_val in _null_vals)
        )

        return AssetSpecs(
            manufacturer=d.get("manufacturer") or "Unknown",
            model=d.get("model") or "Unknown",
            part_number=d.get("part_number") or "UNKNOWN-PN",
            voltage=d.get("voltage") or "N/A",
            category=cat,
            hp=d.get("hp"),
            serial_number=d.get("serial_number"),
            description=d.get("description") or "",
            raw_text=image_description,
            gpm=d.get("gpm"),
            psi=d.get("psi"),
            frame=frame_val,
            phase=d.get("phase"),
            detected_type=d.get("detected_type"),
            physical_magnitude=mag,
            rpm=rpm_val,
            missing_critical_specs=missing_crit,
        )
    except Exception as exc:
        print(f"[Vision] Sonnet call failed ({exc}) — using regex fallback")
        return _regex_fallback(image_description)


def _regex_fallback(text: str) -> AssetSpecs:
    def _find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1).strip() if m else None

    equip_kw = ("pump", "motor", "compressor", "blower", "fan", "drive unit", "conveyor")
    part_kw  = ("relay", "contactor", "starter", "vfd", "bearing", "seal", "sensor")
    tl = text.lower()
    if any(k in tl for k in equip_kw) and not any(k in tl for k in part_kw):
        category = "Equipment"
    else:
        category = "Part"

    # Derive detected_type from keywords
    for kw, dt in [
        ("pump",        "Centrifugal Pump"),
        ("motor",       "Induction Motor"),
        ("compressor",  "Air Compressor"),
        ("blower",      "Industrial Blower"),
        ("conveyor",    "Belt Conveyor"),
        ("vfd",         "Variable Frequency Drive"),
        ("starter",     "Motor Starter"),
        ("bearing",     "Ball Bearing"),
    ]:
        if kw in tl:
            detected_type = dt
            break
    else:
        detected_type = None

    phase = _find(r"(\d-?phase|single.phase|three.phase)")

    return AssetSpecs(
        manufacturer=_find(r"(?:mfr|manufacturer|make)[:\s]+([A-Za-z0-9 \-]+)") or "Unknown",
        model=_find(r"(?:model|mdl)[:\s]+([A-Za-z0-9\-]+)") or "Unknown",
        part_number=_find(r"(?:part[:\s#]*|pn[:\s]+)([A-Za-z0-9\-]+)") or "UNKNOWN-PN",
        voltage=_find(r"(\d+(?:\.\d+)?\s*v(?:dc|ac)?)") or "N/A",
        category=category,
        hp=_find(r"(\d+/?\d*\s*hp)"),
        serial_number=_find(r"s/?n[:\s]+([A-Za-z0-9\-]+)"),
        description=text[:120],
        raw_text=text,
        gpm=_find(r"(\d+(?:\.\d+)?\s*gpm)"),
        psi=_find(r"(\d+(?:\.\d+)?\s*psi)"),
        frame=_find(r"\b(\d{2,3}[A-Z]{1,2})\b"),
        phase=phase,
        detected_type=detected_type,
    )
