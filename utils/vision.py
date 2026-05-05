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
Given raw nameplate text, a product listing title/description, or any text describing industrial equipment,
extract ALL visible specifications.

CRITICAL — SHAFT SIZE (check this first):
Scan the ENTIRE input for shaft size patterns:
  - "[number]-[fraction]\" Shaft"  → shaft_size = "1-5/8\""
  - "[number].[number]\" shaft"    → shaft_size = "1.625\""
  - "[number]mm Shaft"             → shaft_size = "42mm"
Product listing titles VERY OFTEN encode shaft size as "[Mfr], [PN], [Type], [SIZE] Shaft, [Model]".
Extract the SIZE VALUE only. If shaft size text is present, you MUST populate shaft_size.

Extract ALL dimensional and fit specifications visible ANYWHERE in the input — including
product titles, subtitles, description text, specification tables, and part number suffixes.
Do not limit extraction to nameplate-style fields only.
Return null for any field not visible. Never omit a field — always return the key with null if not found.

Return ONLY valid JSON with these exact keys:
{
  "category":                "Part" or "Equipment",
  "detected_type":           string or null,
  "manufacturer":            string or null,
  "manufacturer_confidence": integer 0-100,
  "manufacturer_candidates": list of strings,
  "model":                   string or null,
  "part_number":             string or null,
  "voltage":                 string or null,
  "phase":                   string or null,
  "hp":                      string or null,
  "rpm":                     string or null,
  "serial_number":           string or null,
  "description":             string,
  "gpm":                     string or null,
  "psi":                     string or null,
  "frame":                   string or null,
  "physical_magnitude":      "parcel" | "heavy_parcel" | "LTL_freight",
  "shaft_size":              string or null,
  "bore_diameter":           string or null,
  "seal_face_size":          string or null,
  "connection_size":         string or null,
  "material_spec":           string or null
}

manufacturer_confidence rules (integer 0-100):
  90-100: manufacturer name is EXPLICITLY VISIBLE in the text — name literally present.
  60-79:  manufacturer CONFIDENTLY INFERRED from a well-known PN prefix or model pattern
          (e.g. "PMC" prefix → Endress Hauser, "8536SC" → Square D, "22B-D" → Allen-Bradley).
  30-59:  manufacturer GUESSED from partial context — possible but uncertain.
  0-29:   manufacturer unknown or PN has no recognisable OEM pattern.
manufacturer_candidates: up to 3 plausible manufacturer names ranked by likelihood.
  REQUIRED when manufacturer_confidence < 80.  Empty list [] when confidence >= 80.

Category rules:
- "Equipment" = full assembled unit: pump, motor, compressor, blower, drive unit, conveyor, fan.
- "Part"      = replacement component: bearing, seal, contactor, relay, starter, VFD, sensor, belt, coupling.

detected_type rules:
- A precise, searchable equipment description, e.g.:
    "Vertical Multi-Stage Centrifugal Pump", "3-Phase TEFC Induction Motor",
    "Variable Frequency Drive", "Horizontal Belt Conveyor", "Rotary Screw Air Compressor",
    "Magnetic Motor Starter", "Roller Bearing", "Mechanical Seal".
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
- For description: one concise line including all key specs found, e.g.
  "Mechanical seal, 1-5/8\" shaft, Type 21, Viton elastomers" or
  "3 HP TEFC induction motor, 3-phase, 56C frame, 1750 RPM".

Dimensional and fit-critical fields:
- shaft_size     : Look for "[dimension] Shaft" anywhere in title, description, or spec table.
                   Examples: "1-5/8\" Shaft" → "1-5/8\"", "42mm Shaft" → "42mm", "1.625\" shaft" → "1.625\"".
                   Product titles often have format: "[Mfr], [PN], [Type], [Size] Shaft, [Model]".
- bore_diameter  : Bore or ID specification. Look in description or spec table.
- seal_face_size : For mechanical seals specifically — face diameter if stated.
- connection_size: For fittings, valves, flanged connections — e.g. "1-1/2\" NPT", "2\" flanged".
- material_spec  : Elastomer or material type. For seals: "Viton", "EPDM", "Buna-N", "Silicon", "PTFE",
                   "Carbon/Silicon Carbide". For Gusher and similar brands, part number suffixes encode
                   material (e.g. "C238CBC" style suffixes indicate seal face and elastomer combinations
                   — note the full suffix in part_number AND infer or state the material in material_spec
                   if the encoding is recognisable; otherwise set material_spec to the suffix itself).

CRITICAL — Parts category extraction rule:
  When category == "Part" (bearing, seal, contactor, relay, VFD, belt, coupling, sensor, etc.),
  you MUST set voltage, hp, rpm, gpm, psi, and phase to null.
  These numbers describe the parent equipment, NOT the part itself.
  A mechanical seal does not have a voltage. A bearing does not have HP.
  Extract ONLY: manufacturer, model, part_number, description, physical_magnitude,
  shaft_size, bore_diameter, seal_face_size, connection_size, material_spec.

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

        mfg_conf = d.get("manufacturer_confidence")
        try:
            mfg_conf = int(mfg_conf) if mfg_conf is not None else 100
        except (TypeError, ValueError):
            mfg_conf = 100
        mfg_conf = max(0, min(100, mfg_conf))

        print(
            f"[Vision] Extracted → [{cat}] {d.get('detected_type')} | "
            f"{d.get('manufacturer')} {d.get('model')} (conf={mfg_conf}) | "
            f"PN: {d.get('part_number')} | Phase: {d.get('phase')} | "
            f"GPM: {d.get('gpm')} | PSI: {d.get('psi')}"
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
            shaft_size=d.get("shaft_size") or None,
            bore_diameter=d.get("bore_diameter") or None,
            seal_face_size=d.get("seal_face_size") or None,
            connection_size=d.get("connection_size") or None,
            material_spec=d.get("material_spec") or None,
            manufacturer_confidence=mfg_conf,
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
