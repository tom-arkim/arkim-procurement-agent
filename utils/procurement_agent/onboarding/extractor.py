"""
utils/procurement_agent/onboarding/extractor.py

T2 — Extraction pipeline: harvested pages → structured supplier-scope draft.

Reuses the repo's LLM call pattern (the intake / brand_intelligence seam):
``requests.post`` to ``api.anthropic.com/v1/messages``, parse
``content[0].text``, strip markdown fences, ``json.loads``. Fail-soft: no key
or any error → a heuristic fallback draft (never raises into the pipeline,
mirrors §9 external-provider discipline + the intake ``_fallback_extract``).

Output: an ``OnboardingDraft`` — the pre-populated supplier profile the
concierge reviews. Each field carries ``confidence`` (0..1), ``evidence`` (a
short quote / signal), and ``source_url`` (which page it came from).

The **must-confirm trio** (brand *relationship*, class *core-competency*,
*ship_area*) is marked ``must_confirm=True`` on EVERY draft regardless of
confidence — these three drive sourcing routing and carry channel/territory
risk, so v1 never auto-applies them. The concierge approve step (T3) is the
only path that writes them to the registry. Asserted in tests.

Class classification uses the SHARED Night 3 dictionary
(``utils.sourcing_archieved.part_type_classes``): the LLM is given the
canonical noun-class list and asked to pick; every picked class_id is
validated/canonicalized via ``classify_noun_class`` so a stray phrase still
maps to the dictionary. Free-text classes that don't map are dropped (we do
not invent noun classes — the dictionary is the single source of truth).

Cost: one consolidated LLM call per site (all pages' pruned text in one
prompt), so a 5-site eval = 5 calls (well under the ≤60 live cap). The budget
allows up to ~8 per-page calls; consolidated is chosen for cross-page dedup +
relationship inference. ``llm_tracker.record_call`` is fed when usage is
present, mirroring brand_intelligence.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from utils.procurement_agent.onboarding.dom import PageContent
from utils.procurement_agent.onboarding.harvester import HarvestResult


# Model: Haiku by default (cheap, fits the ≤60-call eval budget). Env override
# mirrors BRAND_INTEL_MODEL / OS_EXTRACTION_MODEL house convention.
_DEFAULT_MODEL = os.environ.get("ONBOARDING_EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
_MAX_TOKENS = 2048
_TIMEOUT = 45

# Relationship vocabulary — MUST match supplier_registry.BRAND_RELATIONSHIPS so
# the approve step can write brands without translation.
BRAND_AUTHORIZED = "AUTHORIZED"
BRAND_CARRIES = "CARRIES"
BRAND_AFTERMARKET_COMPATIBLE = "AFTERMARKET_COMPATIBLE"
_BRAND_RELATIONSHIPS = (BRAND_AUTHORIZED, BRAND_CARRIES, BRAND_AFTERMARKET_COMPATIBLE)

# The must-confirm trio — always flagged regardless of confidence (v1: nothing
# auto-applies; the concierge is the only writer to the registry).
MUST_CONFIRM_FIELDS = ("brands", "classes", "ship_area")


# ---------------------------------------------------------------------------
# Draft dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BrandDraft:
    name: str
    relationship_guess: str = BRAND_CARRIES   # conservative default
    confidence: float = 0.0
    evidence: str = ""
    source_url: str = ""
    must_confirm: bool = True                  # always True in v1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClassDraft:
    class_id: str                              # NounClass.canonical (validated)
    confidence: float = 0.0
    evidence: str = ""
    source_url: str = ""
    is_core_guess: bool = False
    must_confirm: bool = True                  # always True in v1

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LocationDraft:
    locality: str = ""
    region: str = ""
    country: str = ""
    confidence: float = 0.0
    evidence: str = ""
    source_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OnboardingDraft:
    """The pre-populated supplier profile awaiting concierge review/approve."""
    domain: str = ""
    name: str = ""
    vertical: str = ""
    brands: list[BrandDraft] = field(default_factory=list)
    classes: list[ClassDraft] = field(default_factory=list)
    locations: list[LocationDraft] = field(default_factory=list)
    ship_area_guess: Optional[dict] = None     # {"kind":"NATIONWIDE_US"} | {"kind":"STATES","states":[...]}
    overall_confidence: float = 0.0
    source_urls: list[str] = field(default_factory=list)
    # The must-confirm trio — ALWAYS True in v1 (asserted). Nothing writes to
    # the registry without approve, regardless of overall_confidence.
    must_confirm: dict = field(default_factory=lambda: {
        "brands": True, "classes": True, "ship_area": True,
    })
    extraction_method: str = "llm"             # "llm" | "heuristic_fallback"
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "name": self.name,
            "vertical": self.vertical,
            "brands": [b.to_dict() for b in self.brands],
            "classes": [c.to_dict() for c in self.classes],
            "locations": [l.to_dict() for l in self.locations],
            "ship_area_guess": self.ship_area_guess,
            "overall_confidence": self.overall_confidence,
            "source_urls": list(self.source_urls),
            "must_confirm": dict(self.must_confirm),
            "extraction_method": self.extraction_method,
            "notes": self.notes,
        }

    def enforce_must_confirm(self) -> "OnboardingDraft":
        """Assert the must-confirm trio is True on every draft regardless of
        confidence. Called by both the LLM and fallback paths so the invariant
        is structural, not per-call-site. Returns self for chaining."""
        self.must_confirm = {f: True for f in MUST_CONFIRM_FIELDS}
        for b in self.brands:
            b.must_confirm = True
        for c in self.classes:
            c.must_confirm = True
        return self


# ---------------------------------------------------------------------------
# Canonical class list (from the SHARED Night 3 dictionary)
# ---------------------------------------------------------------------------

def _canonical_classes() -> list[str]:
    """The canonical noun-class labels from part_type_classes (the shared dict)."""
    try:
        from utils.sourcing_archieved import part_type_classes as ptc
        return [nc.canonical for nc in ptc._NOUN_CLASSES]
    except Exception:
        # Degraded: no dictionary -> extractor can't validate class_ids; the
        # fallback path emits no classes. Fail-soft, not a crash.
        return []


def _canonicalize_class(raw: str) -> Optional[str]:
    """Map a free-text / canonical class label to a validated canonical label.

    The LLM is given the canonical list and asked to pick; this validates a
    pick and falls back to ``classify_noun_class`` (substring/synonym match)
    for a stray phrase. None if it doesn't map (we never invent classes).
    """
    if not raw:
        return None
    try:
        from utils.sourcing_archieved import part_type_classes as ptc
    except Exception:
        return None
    label = raw.strip().upper()
    nc = ptc.get_noun_class(label)
    if nc:
        return nc.canonical
    cls = ptc.classify_noun_class(raw)
    return cls  # None if undetectable


# ---------------------------------------------------------------------------
# LLM extraction
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are an industrial supplier-scope extraction assistant for Arkim, a procurement agent for industrial maintenance parts.

You are given the DOM-pruned text of a supplier's website (home + discovered pages: brands/line-card, about, locations, products). Extract a STRUCTURED supplier-scope draft.

Output ONLY valid JSON (no markdown fences, no prose) with this shape:
{
  "name": "<supplier name>",
  "vertical": "<one-line industry vertical, e.g. 'industrial power transmission distribution'>",
  "brands": [
    {"name": "<manufacturer/brand>", "relationship_guess": "AUTHORIZED|CARRIES|AFTERMARKET_COMPATIBLE", "confidence": 0.0-1.0, "evidence": "<short quote/signal>", "source_url": "<page url>"}
  ],
  "classes": [
    {"class_id": "<one of the CANONICAL_CLASSES>", "confidence": 0.0-1.0, "evidence": "<short quote>", "source_url": "<page url>", "is_core_guess": true|false}
  ],
  "locations": [
    {"locality": "<city>", "region": "<state/province code or name>", "country": "<ISO country, default US>", "confidence": 0.0-1.0, "evidence": "<quote>", "source_url": "<page url>"}
  ],
  "ship_area_guess": {"kind": "NATIONWIDE_US"} | {"kind": "STATES", "states": ["NY","CA"]} | null,
  "overall_confidence": 0.0-1.0
}

RULES:
- brands: only REAL manufacturers/brands the supplier CARRIES or is AUTHORIZED for. Do NOT list the supplier's own name as a brand unless it is a house manufacturer. Do NOT list distributor/channel partners (e.g. Motion Industries, Applied Industrial Technologies, Grainger) as brands — those are CHANNEL partners, not carried brands. Use image alt text and a brands/line-card page as the strongest brand signal. relationship_guess: AUTHORIZED if the site says 'authorized distributor/dealer', CARRIES if it just stocks/sells the brand, AFTERMARKET_COMPATIBLE if it sells a cross-reference/aftermarket compatible part.
- classes: pick ONLY from the CANONICAL_CLASSES list provided. is_core_guess=true for the supplier's primary competencies (the classes that dominate the site), false for incidental coverage. Leave a class OUT if the site gives no signal for it — do not pad.
- locations: headquarters / branch addresses visible on the site. region is a US state code where possible.
- ship_area_guess: NATIONWIDE_US if the site says national/US-wide shipping or is a national distributor; STATES with an explicit list only if the site names specific served states; null if unclear.
- confidence: your confidence the field is CORRECT (not just present). Low confidence when the signal is ambiguous (e.g. a name that might be a product line, not a brand).
- If a field has no evidence, return an empty list / null — do not fabricate.
"""


def _build_user_prompt(pages: list[PageContent], canonical_classes: list[str]) -> str:
    cls_list = ", ".join(canonical_classes) if canonical_classes else "(dictionary unavailable)"
    parts = [f"CANONICAL_CLASSES: {cls_list}", ""]
    for i, p in enumerate(pages, 1):
        parts.append(f"--- PAGE {i}: {p.url} ---")
        parts.append(p.text_for_extraction())
        parts.append("")
    return "\n".join(parts)


def _parse_llm_json(raw: str) -> dict:
    """Strip markdown fences + json.loads, mirroring intake_agent._parse_llm_json."""
    raw = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip())
    raw = re.sub(r"\s*```$", "", raw)
    # Tolerate a trailing/leading prose wrapper by extracting the first {...} block.
    if not raw.startswith("{"):
        m = re.search(r"\{.*\}", raw, re.S)
        if m:
            raw = m.group(0)
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _call_llm(system: str, user: str, api_key: str, model: str) -> Optional[dict]:
    """One Anthropic call. Returns parsed JSON dict or None (fail-soft)."""
    try:
        import requests
    except Exception:
        return None
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": _MAX_TOKENS,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        # Cost tracking (silent fail) — mirrors brand_intelligence.
        try:
            from utils.llm_tracker import record_call as _llm_rec
            _u = data.get("usage", {})
            _llm_rec(_u.get("input_tokens", 0), _u.get("output_tokens", 0))
        except Exception:
            pass
        raw_text = data["content"][0]["text"]
        return _parse_llm_json(raw_text)
    except Exception as exc:
        print(f"[OnboardingExtractor] LLM call failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Draft assembly (from parsed LLM JSON or heuristic fallback)
# ---------------------------------------------------------------------------

def _domain_from_url(url: str) -> str:
    from urllib.parse import urlparse
    try:
        host = (urlparse(url).hostname or "").lower().replace("www.", "")
        return host
    except Exception:
        return ""


def _coerce_relationship(raw: str) -> str:
    r = (raw or "").upper().strip()
    if r in _BRAND_RELATIONSHIPS:
        return r
    # Lenient aliases.
    if "AUTHORIZED" in r or "DEALER" in r or "DISTRIBUTOR" in r and "AUTHORIZED" in r:
        return BRAND_AUTHORIZED
    if "AFTERMARKET" in r or "COMPATIBLE" in r or "CROSS" in r:
        return BRAND_AFTERMARKET_COMPATIBLE
    return BRAND_CARRIES  # conservative default


def _assemble_draft(parsed: dict, pages: list[PageContent], *,
                    method: str) -> OnboardingDraft:
    """Build an OnboardingDraft from parsed JSON (LLM) or a heuristic dict."""
    home = pages[0] if pages else None
    home_url = home.url if home else ""
    domain = _domain_from_url(parsed.get("domain") or home_url) or _domain_from_url(home_url)

    # Name: prefer parsed, fall back to title / og:title / domain.
    name = (parsed.get("name") or "").strip()
    if not name and home:
        name = (home.og_title or home.title or "").strip()
    if not name:
        name = domain

    # Brands.
    brands: list[BrandDraft] = []
    seen_brands: set[str] = set()
    for b in (parsed.get("brands") or [])[:60]:
        nm = (b.get("name") or "").strip()
        if not nm:
            continue
        key = nm.lower()
        if key in seen_brands:
            continue
        seen_brands.add(key)
        brands.append(BrandDraft(
            name=nm,
            relationship_guess=_coerce_relationship(b.get("relationship_guess")),
            confidence=_clamp(b.get("confidence", 0.0)),
            evidence=(b.get("evidence") or "").strip()[:300],
            source_url=(b.get("source_url") or "").strip(),
        ))

    # Classes — validate/canonicalize every pick against the shared dictionary.
    classes: list[ClassDraft] = []
    seen_classes: set[str] = set()
    for c in (parsed.get("classes") or [])[:40]:
        cid = _canonicalize_class(c.get("class_id") or c.get("class") or "")
        if not cid or cid in seen_classes:
            continue
        seen_classes.add(cid)
        classes.append(ClassDraft(
            class_id=cid,
            confidence=_clamp(c.get("confidence", 0.0)),
            evidence=(c.get("evidence") or "").strip()[:300],
            source_url=(c.get("source_url") or "").strip(),
            is_core_guess=bool(c.get("is_core_guess")),
        ))

    # Locations.
    locations: list[LocationDraft] = []
    for loc in (parsed.get("locations") or [])[:20]:
        loc = loc or {}
        locality = (loc.get("locality") or "").strip()
        region = (loc.get("region") or "").strip()
        country = (loc.get("country") or "US").strip() or "US"
        if not (locality or region):
            continue
        locations.append(LocationDraft(
            locality=locality, region=region, country=country,
            confidence=_clamp(loc.get("confidence", 0.0)),
            evidence=(loc.get("evidence") or "").strip()[:300],
            source_url=(loc.get("source_url") or "").strip(),
        ))

    # Ship area.
    ship = parsed.get("ship_area_guess")
    if isinstance(ship, dict) and ship.get("kind") in ("NATIONWIDE_US", "STATES"):
        if ship["kind"] == "STATES":
            states = [str(s).upper().strip() for s in (ship.get("states") or []) if str(s).strip()]
            ship = {"kind": "STATES", "states": states} if states else None
        else:
            ship = {"kind": "NATIONWIDE_US"}
    else:
        ship = None

    draft = OnboardingDraft(
        domain=domain,
        name=name,
        vertical=(parsed.get("vertical") or "").strip(),
        brands=brands,
        classes=classes,
        locations=locations,
        ship_area_guess=ship,
        overall_confidence=_clamp(parsed.get("overall_confidence", 0.0)),
        source_urls=[p.url for p in pages],
        extraction_method=method,
    )
    return draft.enforce_must_confirm()


def _clamp(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


# ---------------------------------------------------------------------------
# Heuristic fallback (no API key / LLM failure)
# ---------------------------------------------------------------------------

_LOC_RE = re.compile(
    r"(?<![A-Za-z])"                                   # no preceding letter (word boundary)
    r"([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)\s*,\s([A-Z]{2})\b"
)

# "Street" words that are address-street labels, not city names — a hit whose
# first group is one of these means the regex matched a street line ("4400
# Walden Avenue, Lancaster, NY") and the CITY is the second capitalized token.
_STREET_LABELS = {"avenue", "street", "st", "road", "rd", "boulevard", "blvd",
                  "drive", "dr", "lane", "ln", "way", "court", "ct", "highway",
                  "hwy", "parkway", "pkwy", "place", "pl", "trail", "tl"}
_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}


def _heuristic_draft(pages: list[PageContent]) -> OnboardingDraft:
    """No-key / LLM-fail fallback: extract what's reliably rule-detectable.

    Captures: name (title), domain, classes via the dictionary over the text,
    one location from a 'City, ST' pattern, vertical from meta description.
    Brands are LEFT EMPTY rather than guessed (a wrong brand list is worse than
    none — the must-confirm trio + approve gate protects either way). This
    makes the no-key path honest: it pre-populates the safe fields and leaves
    brand-relationship + ship-area for the human concierge.
    """
    home = pages[0] if pages else None
    home_url = home.url if home else ""
    domain = _domain_from_url(home_url)
    name = (home.og_title or home.title or "").strip() or domain
    vertical = (home.meta_description or "").strip()[:160]

    # Classes: scan the dictionary over the concatenated text + alt texts.
    seen: set[str] = set()
    classes: list[ClassDraft] = []
    try:
        from utils.sourcing_archieved import part_type_classes as ptc
    except Exception:
        ptc = None
    if ptc:
        hay = " ".join((p.text + " " + " ".join(p.alt_texts)) for p in pages).lower()
        # Count synonym hits per class to gauge core vs incidental.
        hit_counts: dict[str, int] = {}
        for nc in ptc._NOUN_CLASSES:
            n = 0
            for syn in nc.synonyms:
                n += hay.count(syn)
            if n > 0:
                hit_counts[nc.canonical] = n
        if hit_counts:
            max_hits = max(hit_counts.values())
            for cid, n in sorted(hit_counts.items(), key=lambda kv: (-kv[1], kv[0])):
                classes.append(ClassDraft(
                    class_id=cid,
                    confidence=min(0.9, 0.4 + 0.4 * (n / max(max_hits, 1))),
                    evidence=f"dictionary synonym hits: {n}",
                    source_url=home_url,
                    is_core_guess=(n >= max_hits * 0.5),
                ))

    # Location: first 'City, ST' pattern in the home page text. A street-line
    # hit ("4400 Walden Avenue, Lancaster, NY") is recognized by the first
    # group being a street label — in that case the CITY is the LAST word of
    # the group's preceding token; re-scan to take the standalone 'City, ST'
    # form (the "Satellite Locations: Rochester, NY" style) which is clean.
    locations: list[LocationDraft] = []
    if home:
        for m in _LOC_RE.finditer(home.text or ""):
            city, st = m.group(1).strip(), m.group(2)
            if st not in _US_STATES:
                continue
            # Skip street-line false positives where the captured "city" is a
            # street label (e.g. "Avenue Lancaster, NY" from "Walden Avenue").
            first_word = city.split()[0].lower().rstrip(",.")
            if first_word in _STREET_LABELS:
                # The real city is likely the following capitalized word; take
                # the second token of the group if present, else skip.
                toks = city.split()
                if len(toks) >= 2 and toks[1].lower().rstrip(",.") not in _STREET_LABELS:
                    city = toks[1]
                else:
                    continue
            locations.append(LocationDraft(
                locality=city, region=st, country="US",
                confidence=0.6, evidence=f"{city}, {st}",
                source_url=home_url,
            ))
            break

    parsed = {
        "name": name, "vertical": vertical, "domain": domain,
        "brands": [], "classes": [], "locations": [],
        "ship_area_guess": None, "overall_confidence": 0.3,
    }
    draft = _assemble_draft(parsed, pages, method="heuristic_fallback")
    # Overlay the rule-detected classes/locations (the LLM path would have
    # supplied them; the fallback supplies its own).
    draft.classes = classes
    draft.locations = locations
    draft.notes = "heuristic fallback (no API key or LLM failure); brands left for human concierge"
    return draft.enforce_must_confirm()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_scope(
    harvest: HarvestResult,
    *,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    llm_caller: Optional[callable] = None,
) -> OnboardingDraft:
    """Extract a supplier-scope draft from a harvested site.

    ``api_key`` defaults to ``ANTHROPIC_API_KEY`` from the env. ``llm_caller``
    is an injectable (system, user, api_key, model) -> parsed-dict|None for
    tests (mocked LLM, no live network). Fail-soft: no key / call failure →
    heuristic fallback draft (never raises).

    The must-confirm trio is enforced on every draft regardless of confidence.
    """
    pages = harvest.pages or []
    key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
    model = model or _DEFAULT_MODEL

    if not pages:
        return OnboardingDraft(extraction_method="empty",
                               notes="no pages harvested").enforce_must_confirm()

    if key or llm_caller:
        canonical = _canonical_classes()
        user_prompt = _build_user_prompt(pages, canonical)
        caller = llm_caller or _call_llm
        parsed = caller(_SYSTEM_PROMPT, user_prompt, key, model)
        if isinstance(parsed, dict) and parsed:
            return _assemble_draft(parsed, pages, method="llm")
        # LLM returned nothing useful -> fall through to heuristic.
        print("[OnboardingExtractor] LLM returned no usable JSON; using heuristic fallback")

    return _heuristic_draft(pages)


def extract_scope_from_dict(parsed: dict, pages: list[PageContent]) -> OnboardingDraft:
    """Assemble a draft from a pre-parsed dict (used by the eval / tests to
    inject a canned LLM JSON without monkeypatching the HTTP call)."""
    return _assemble_draft(parsed, pages, method="llm")
