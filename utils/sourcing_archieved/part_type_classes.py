"""
utils/sourcing_archieved/part_type_classes.py
MRO noun-class dictionary + classifier for the SCORING_V2 TypeGate.

Pure data — no network, no I/O on import. Each class has a canonical name,
a list of human-language synonyms (matched against free text / titles), and a
set of slug tokens (matched against URL path segments, where the vendor's own
category lives, e.g. ``/mechanical-seals/goulds/...`` vs ``/pump/centrifugal/...``).

The dictionary is intentionally compact and MRO-focused: the categories that
drive the "wrong-part-from-a-big-vendor beats right-part-from-a-specialist"
failure (seal-vs-pump is the anchor case). Adding a class here is data-only and
does not change any scorer behavior until the TypeGate (T4) consumes it.

Used by the SCORING_V2 path (T3-T5). Flag-off scoring never imports this module's
classification into the score, so it stays byte-identical.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class NounClass:
    """One MRO part/equipment noun-class."""

    canonical: str                       # short canonical label, e.g. "SEAL"
    synonyms: tuple[str, ...]            # human-language phrases, lowercase
    slug_tokens: tuple[str, ...] = field(default_factory=tuple)  # URL slug fragments, no slashes
    # UNSPSC crosswalk — the commodity code for this class. PROVISIONAL best-effort
    # mapping (see UNSPSC_PINNED_RELEASE below); structurally a string so callers can
    # treat it as opaque. Embedded here so the dictionary is the single source of truth
    # for class identity, slug tokens, AND the UNSPSC crosswalk (Night 3 T1 / I4).
    unspsc: str = ""


# ---------------------------------------------------------------------------
# UNSPSC crosswalk (I4 / Night 3 T1)
# ---------------------------------------------------------------------------
# A per-class UNSPSC commodity code is carried on each NounClass (`unspsc`).
# This is the static class->code mapping the supplier-scope schema (Night 3 T2)
# references so a supplier's class coverage maps to a pinned commodity taxonomy.
#
# PROVISIONAL: the codes below are best-effort segment-level codes (typically the
# 8-digit family/commodity level). They are NOT yet verified against an official
# pinned UNSPSC release — they are placeholders that carry the STRUCTURE of the
# crosswalk. Before production sourcing/reporting consumes them, verify each code
# against a pinned official release (UNSPSC is a separate maintained taxonomy)
# and freeze the mapping. The pinned-release identifier will be recorded here at
# that time. Treat the codes as opaque identifiers until then.
UNSSPSC_PINNED_RELEASE = "provisional-unverified"   # replace with a real release tag at verification

# ---------------------------------------------------------------------------
# The dictionary
# ---------------------------------------------------------------------------
# Slug tokens are the site's own category breadcrumb segment — the single
# highest-leverage noun-class signal (the URL slug encodes the catalog category
# the vendor itself filed the page under). Synonyms cover titles / snippets /
# detected_type strings. Keep both lists lowercase; matching is case-insensitive.
#
# EXPANSION NOTE (Night 3 T1): the dictionary below grew from 10 -> 27 classes.
# The expansion is ADDITIONS ONLY — no existing class was renamed, removed, or
# had its synonyms/slug_tokens edited (the shared-asset guard: SCORING_V2
# detection is live against this dictionary, so any change to an existing
# class's match surface could reclassify existing results). The new classes
# (PACKING, HOSE, FILTER, SENSOR, GEARBOX, CONVEYOR, BELTING, LUBRICANT,
# WEAR RING, CARBON BRUSH, DIAPHRAGM, CHAIN, SOLENOID, SWITCH, GEAR,
# TRANSFORMER, ENCLOSURE) were chosen so their synonyms/slug tokens do NOT
# appear as substrings in any existing detection/scoring fixture, verified by
# simulation (zero reclassifications across the t3/t4/t6 + detection-eval
# battery). Each carries a provisional UNSPSC code (see UNSSPSC_PINNED_RELEASE).

_NOUN_CLASSES: tuple[NounClass, ...] = (
    NounClass(
        canonical="SEAL",
        synonyms=(
            "mechanical seal", "cartridge seal", "shaft seal", "seal kit",
            "lip seal", "gland seal", "component seal", "split seal",
            "elastomeric seal", "seal assembly", "seal",
        ),
        slug_tokens=("mechanical-seals", "seal-kit", "seals", "shaft-seal", "seal"),
        unspsc="31162701",  # mechanical seals (provisional)
    ),
    NounClass(
        canonical="PUMP",
        synonyms=(
            "centrifugal pump", "ansi pump", "centrifugal", "vacuum pump",
            "diaphragm pump", "gear pump", "metering pump", "submersible pump",
            "sump pump", "booster pump", "process pump", "pump",
        ),
        slug_tokens=("centrifugal-pumps", "pump", "pumps", "centrifugal-pump"),
        unspsc="40121700",  # centrifugal pumps (provisional)
    ),
    NounClass(
        canonical="BEARING",
        synonyms=(
            "ball bearing", "roller bearing", "sleeve bearing", "thrust bearing",
            "plain bearing", "pillow block bearing", "bearing unit",
            "bearing assembly", "bearing",
        ),
        slug_tokens=("bearings", "bearing", "ball-bearings", "roller-bearings"),
        unspsc="31171500",  # bearings (provisional)
    ),
    NounClass(
        canonical="GASKET",
        synonyms=(
            "gasket", "head gasket", "flange gasket", "spiral wound gasket",
            "o-ring", "oring", "o ring", "gasket set", "gasket kit",
        ),
        slug_tokens=("gaskets", "gasket", "o-rings", "oring", "seals-gaskets"),
        unspsc="31161500",  # gaskets (provisional)
    ),
    NounClass(
        canonical="VALVE",
        synonyms=(
            "ball valve", "butterfly valve", "gate valve", "globe valve",
            "check valve", "solenoid valve", "control valve", "relief valve",
            "valve",
        ),
        slug_tokens=("valves", "valve", "ball-valves", "butterfly-valves"),
        unspsc="40141800",  # valves (provisional)
    ),
    NounClass(
        canonical="MOTOR",
        synonyms=(
            "electric motor", "ac motor", "dc motor", "induction motor",
            "servo motor", "stepper motor", "gearmotor", "gear motor",
            "brake motor", "motor",
        ),
        slug_tokens=("motors", "motor", "electric-motors", "ac-motors"),
        unspsc="31151500",  # ac motors (provisional)
    ),
    NounClass(
        canonical="DRIVE",
        synonyms=(
            "variable frequency drive", "vfd", "variable speed drive",
            "frequency drive", "drive controller", "inverter", "soft starter",
            "motor starter", "starter", "drive",
        ),
        slug_tokens=("drives", "drive", "vfd", "vfds", "inverters", "soft-starters"),
        unspsc="31202300",  # motor drives / controllers (provisional)
    ),
    NounClass(
        canonical="SLEEVE",
        synonyms=(
            "shaft sleeve", "wear sleeve", "spacer sleeve", "sleeve",
            "bushing sleeve",
        ),
        slug_tokens=("sleeves", "sleeve", "shaft-sleeves"),
        unspsc="31171800",  # bushings / sleeves (provisional)
    ),
    NounClass(
        canonical="IMPELLER",
        synonyms=(
            "impeller", "pump impeller", "open impeller", "closed impeller",
            "semi-open impeller", "impeller assembly",
        ),
        slug_tokens=("impellers", "impeller", "pump-impellers"),
        unspsc="40121717",  # pump impellers (provisional)
    ),
    NounClass(
        canonical="COUPLING",
        synonyms=(
            "coupling", "shaft coupling", "flexible coupling", "rigid coupling",
            "jaw coupling", "grid coupling", "gear coupling", "coupling assembly",
        ),
        slug_tokens=("couplings", "coupling", "shaft-couplings"),
        unspsc="31201500",  # couplings (provisional)
    ),
    # --- Night 3 T1 expansion (ADDITIONS ONLY; see EXPANSION NOTE above) -------
    # GASKET/PACKING split: packing is a distinct compression-seal commodity from
    # a flat gasket (gland/pump packing is braided cord, not a cut sheet).
    NounClass(
        canonical="PACKING",
        synonyms=(
            "gland packing", "pump packing", "compression packing",
            "packing set", "packing kit", "packing",
        ),
        slug_tokens=("packing", "gland-packing", "packing-kits", "packings"),
        unspsc="31162400",  # packing (provisional)
    ),
    NounClass(
        canonical="HOSE",
        synonyms=(
            "hydraulic hose", "industrial hose", "air hose", "water hose",
            "hose assembly", "hose",
        ),
        slug_tokens=("hose", "hoses", "hydraulic-hose", "hose-assemblies"),
        unspsc="31192700",  # hose (provisional)
    ),
    NounClass(
        canonical="FILTER",
        synonyms=(
            "filter element", "filter cartridge", "filter housing",
            "filter bag", "air filter", "oil filter", "strainer", "filter",
        ),
        slug_tokens=("filters", "filter", "strainers", "filter-element",
                     "filter-housings"),
        unspsc="40101700",  # filters (provisional)
    ),
    # sensor/instrument: the part_type_registry's sensor_instrument family maps
    # here (instruments + transmitters + gauges + sensors share a sourcing lane).
    NounClass(
        canonical="SENSOR",
        synonyms=(
            "pressure sensor", "level sensor", "temperature sensor",
            "flow sensor", "pressure transmitter", "level transmitter",
            "temperature transmitter", "pressure gauge", "gauge",
            "instrument", "sensor",
        ),
        slug_tokens=("sensors", "transmitters", "gauges", "instruments",
                     "pressure-sensors", "level-transmitters"),
        unspsc="41111700",  # sensors / transmitters (provisional)
    ),
    NounClass(
        canonical="GEARBOX",
        synonyms=(
            "gear reducer", "speed reducer", "gearbox assembly", "gear box",
            "gearbox", "reducer",
        ),
        slug_tokens=("gearboxes", "gear-reducers", "speed-reducers", "gearbox"),
        unspsc="31201800",  # gearboxes / speed reducers (provisional)
    ),
    # conveyor components (rollers/idlers/pulleys); conveyor *belt* is BELTING.
    NounClass(
        canonical="CONVEYOR",
        synonyms=(
            "conveyor roller", "conveyor component", "conveyor idler",
            "conveyor pulley", "conveyor",
        ),
        slug_tokens=("conveyors", "conveyor-rollers", "conveyor-components",
                     "conveyor-idlers"),
        unspsc="31182700",  # conveyor components (provisional)
    ),
    NounClass(
        canonical="BELTING",
        synonyms=(
            "conveyor belt", "timing belt", "v-belt", "v belt", "fan belt",
            "flat belt", "drive belt", "synchronous belt", "belting",
        ),
        slug_tokens=("belts", "v-belts", "timing-belts", "conveyor-belts",
                     "drive-belts", "belting"),
        unspsc="31201600",  # belts (provisional)
    ),
    NounClass(
        canonical="LUBRICANT",
        synonyms=(
            "lubricating grease", "gear oil", "gearbox oil", "lubricating oil",
            "hydraulic oil", "grease", "lubricant",
        ),
        slug_tokens=("lubricants", "greases", "gear-oil", "lubricating-oil"),
        unspsc="12320000",  # lubricants (provisional)
    ),
    # wear ring — documented dictionary debt (CLEANUP §7.5 names wear ring as a
    # non-seal component that lacks a noun-class entry). Distinct from SLEEVE
    # (wear *sleeve* stays SLEEVE); a wear ring is a pump-casing clearance part.
    NounClass(
        canonical="WEAR RING",
        synonyms=(
            "wear ring", "wear part", "wear component", "wear plate",
        ),
        slug_tokens=("wear-rings", "wear-ring", "wear-plates"),
        unspsc="31171817",  # wear rings (provisional)
    ),
    # carbon brush — documented dictionary debt (CLEANUP §7.5). Motor-commutator
    # consumable; distinct from MOTOR (the motor itself).
    NounClass(
        canonical="CARBON BRUSH",
        synonyms=(
            "carbon brush", "carbon brush holder", "brush holder",
            "motor brush", "brush",
        ),
        slug_tokens=("carbon-brush", "brushes", "brush-holders", "motor-brushes"),
        unspsc="31151600",  # motor brushes (provisional)
    ),
    # diaphragm kit — documented dictionary debt (CLEANUP §7.5). A diaphragm
    # repair kit; "diaphragm pump" stays PUMP (a diaphragm pump is a pump).
    NounClass(
        canonical="DIAPHRAGM",
        synonyms=(
            "diaphragm kit", "diaphragm assembly", "diaphragm",
        ),
        slug_tokens=("diaphragms", "diaphragm-kit", "diaphragm-kits"),
        unspsc="31162900",  # diaphragms (provisional)
    ),
    NounClass(
        canonical="CHAIN",
        synonyms=(
            "roller chain", "drive chain", "conveyor chain", "chain link",
            "chain",
        ),
        slug_tokens=("chains", "roller-chains", "drive-chains", "chain-links"),
        unspsc="31201700",  # chains (provisional)
    ),
    NounClass(
        canonical="SOLENOID",
        synonyms=(
            "solenoid coil", "solenoid actuator", "solenoid",
        ),
        slug_tokens=("solenoids", "solenoid-coils", "solenoid-valves"),
        unspsc="32101700",  # solenoids (provisional)
    ),
    NounClass(
        canonical="SWITCH",
        synonyms=(
            "pressure switch", "limit switch", "selector switch",
            "toggle switch", "switch",
        ),
        slug_tokens=("switches", "limit-switches", "pressure-switches",
                     "selector-switches"),
        unspsc="39121400",  # switches (provisional)
    ),
    # GEAR — bare "gear" intentionally NOT a synonym: it is a substring of
    # "gearmotor" (a MOTOR synonym) and would reclassify it. Multi-word gear
    # phrases only; the canonical "GEAR" still self-references via "gear wheel".
    NounClass(
        canonical="GEAR",
        synonyms=(
            "spur gear", "helical gear", "bevel gear", "worm gear",
            "pinion gear", "gear wheel", "gear set", "gear rack",
        ),
        slug_tokens=("gears", "spur-gears", "helical-gears", "bevel-gears",
                     "gear-racks"),
        unspsc="31201900",  # gears (provisional)
    ),
    NounClass(
        canonical="TRANSFORMER",
        synonyms=(
            "power transformer", "control transformer", "isolation transformer",
            "transformer",
        ),
        slug_tokens=("transformers", "power-transformers", "control-transformers"),
        unspsc="39121000",  # transformers (provisional)
    ),
    NounClass(
        canonical="ENCLOSURE",
        synonyms=(
            "electrical enclosure", "junction enclosure", "nema enclosure",
            "enclosure",
        ),
        slug_tokens=("enclosures", "nema-enclosures", "junction-enclosures"),
        unspsc="39121500",  # enclosures (provisional)
    ),
)

# Canonical -> NounClass lookup (built once at import).
_BY_CANONICAL: dict[str, NounClass] = {nc.canonical: nc for nc in _NOUN_CLASSES}


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Lowercase + strip to [a-z0-9-]. Empty on no useful content."""
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def _classes() -> tuple[NounClass, ...]:
    """Return the dictionary tuple (stable ordering for deterministic matching)."""
    return _NOUN_CLASSES


def get_noun_class(canonical: str) -> Optional[NounClass]:
    """Fetch a NounClass by its canonical label (e.g. 'SEAL'), or None."""
    return _BY_CANONICAL.get((canonical or "").upper())


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify_noun_class(text: str) -> Optional[str]:
    """Classify a free-text string (title, snippet, detected_type, query) into a
    noun-class canonical label, or None if undetectable.

    Multi-word synonyms are matched first (longest first) so 'mechanical seal'
    resolves to SEAL, not the bare 'seal' inside another phrase. Matching is
    case-insensitive substring against the lowercased text. Returns the canonical
    label (e.g. 'SEAL', 'PUMP'); None means "undetectable" — the caller must
    treat that as the 0.4-0.5 TypeGate floor, NEVER zero (ESCI lesson).
    """
    if not text:
        return None
    haystack = (text or "").lower()
    if not haystack.strip():
        return None

    # Longest-synonym-first so multi-word phrases win over their single-word
    # substrings (e.g. "mechanical seal" before "seal", "variable frequency
    # drive" before "drive").
    candidates: list[tuple[int, str]] = []
    for nc in _NOUN_CLASSES:
        for syn in nc.synonyms:
            if syn in haystack:
                candidates.append((len(syn), nc.canonical))
    if candidates:
        candidates.sort(key=lambda c: (-c[0], c[1]))
        return candidates[0][1]
    return None


def classify_noun_class_from_url(url: str) -> Optional[str]:
    """Classify a result URL into a noun-class using its path slug segments.

    The URL slug encodes the site's own category (``/mechanical-seals/goulds/...``
    vs ``/pump/centrifugal/...``) — the highest-leverage signal. Matches against
    the slugified path segments (host/query excluded, so a domain like
    ``pumpcatalog.com`` does not classify the page as a pump). Returns the
    canonical label or None if undetectable.
    """
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        path = (urlparse(url.lower()).path or "")
    except Exception:
        return None
    if not path:
        return None

    # Break the path into slug segments and test each against the slug tokens.
    # First hit wins in dictionary order (deterministic); path order is
    # irrelevant because the category segment is the most-specific one and we
    # trust the vendor's own breadcrumb.
    segments = [seg for seg in (_slugify(seg) for seg in path.split("/")) if seg]
    for nc in _NOUN_CLASSES:
        for token in nc.slug_tokens:
            token_slug = _slugify(token)
            if not token_slug:
                continue
            for seg in segments:
                if seg == token_slug or seg.startswith(f"{token_slug}-") or seg.endswith(f"-{token_slug}"):
                    return nc.canonical
    return None


def _registered_domain(url: str) -> Optional[str]:
    """Return the registered domain (e.g. 'shoppumps.com' from a full URL),
    stripped of www./subdomains. Structural identity signal — what the vendor
    IS, immune to query echo. Returns None on parse failure.
    """
    if not url:
        return None
    from urllib.parse import urlparse
    try:
        hostname = (urlparse(url.lower()).hostname or "").replace("www.", "")
    except Exception:
        return None
    if not hostname:
        return None
    # Reduce to the last two labels (the registered domain). Coarse heuristic
    # (ignores public-suffix edge cases like .co.uk) but adequate for a noun-
    # class corroboration signal — false negatives (None) are safe.
    parts = [p for p in hostname.split(".") if p]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname or None


def classify_noun_class_from_domain(url: str) -> Optional[str]:
    """Classify a result by its registered DOMAIN (vendor identity signal).

    Unlike ``classify_noun_class_from_url`` (path-only by design, so
    ``pumpcatalog.com`` does not classify a seal page as a pump), THIS reads the
    domain itself — ``shoppumps.com`` -> PUMP, ``sealspecialist.com`` -> SEAL.
    The domain is a structural signal about what the vendor sells, so it is a
    legitimate noun-class corroboration on the opaque-URL fallback path (where
    there is no path slug to read). Returns the canonical label or None.
    """
    domain = _registered_domain(url)
    if not domain:
        return None
    label = domain.rsplit(".", 1)[0]  # drop the TLD
    if not label:
        return None
    return classify_noun_class(label)


def classify_result_noun_class(title: str, url: str) -> Optional[str]:
    """Combine title + URL-slug evidence into a single noun-class verdict for a
    result. Title-only and URL-only both classify; if they disagree, the URL
    wins (the vendor's own category breadcrumb is more authoritative than
    marketing title copy). Either-way-undetectable -> None (caller applies the
    0.4-0.5 floor, never zero).

    Backward-compatible 2-arg form (no vendor) — kept for existing callers/tests.
    Does NOT apply the dominant-class structural-signal logic; use
    ``classify_result_noun_class_dominant`` (which takes the vendor) for the
    opaque-URL fallback where query-echo contamination bites.
    """
    url_cls = classify_noun_class_from_url(url)
    title_cls = classify_noun_class(title)
    if url_cls:
        return url_cls
    return title_cls


def classify_result_noun_class_dominant(
    vendor: Optional[str], title: Optional[str], snippet: str, url: str,
) -> Optional[str]:
    """Dominant-class result noun-class detection (the query-echo fix).

    Problem this solves: on the opaque-URL fallback path, detection falls back
    to the snippet, which may ECHO the query noun (a pump page that mentions
    "mechanical seal" because the query asked for a seal). The legacy
    longest-synonym-first rule then lets the echoed phrase ("mechanical seal",
    len 15) beat the product noun ("pump", len 4) -> wrong class -> same-class
    TypeGate ~1.0 -> wrong part survives.

    Fix: structural identity signals — the VENDOR NAME and the registered
    DOMAIN — are immune to query echo (they describe what the vendor IS, not
    what the query asked for). On the opaque-URL path, a structural signal
    overrides a snippet echo. This generalizes across all noun-classes (no
    seal/pump hand-tuning).

    Resolution order:
      1. URL path slug -> authoritative (vendor's own category breadcrumb).
      2. Opaque URL -> structural signals (vendor name + registered domain).
         If they yield a class, return it (overrides any snippet echo).
         Vendor + domain are aggregated; if they agree, that's the verdict. If
         they disagree, the vendor name wins (it is the more deliberate identity
         signal than a domain coinage).
      3. No structural signal -> fall back to the legacy text verdict (title +
         snippet, longest-synonym-first via ``classify_result_noun_class``).
         This is deliberately conservative: without structural corroboration we
         do NOT query-echo-flip the text verdict, because a real "mechanical
         seal for Goulds 3196 pump" page (a SEAL page that names its application)
         must NOT be collateral. The residual uncorroborated-opaque-URL case is
         a documented known-gap sized by the flywheel with real data.

    ``snippet`` is accepted (and used in the text fallback) because the live
    path may pass only a snippet when no clean title is available.
    """
    # 1. URL path slug — authoritative, unchanged.
    url_cls = classify_noun_class_from_url(url)
    if url_cls:
        return url_cls

    # 2. Structural signals (vendor name + registered domain) — immune to echo.
    vendor_cls = classify_noun_class(vendor) if vendor else None
    domain_cls = classify_noun_class_from_domain(url)
    if vendor_cls or domain_cls:
        # Vendor name is the more deliberate identity signal than a domain
        # coinage; prefer it when present.
        return vendor_cls or domain_cls

    # 3. No structural signal -> conservative legacy text verdict (no echo-flip).
    return classify_result_noun_class(title if title else snippet, url)
