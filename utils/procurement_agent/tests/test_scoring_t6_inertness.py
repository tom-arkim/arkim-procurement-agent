"""
T6 — the inertness wall.

Independent proof that SCORING_V2 OFF is byte-identical to the pre-redesign
baseline scorer (commit ecfeaf9), across a BROAD battery of fixtures — not just
the seal-vs-pump anchor. This is the audit that T4/T5's test reframings
preserved the REAL guarantee (flag-off byte-identical), since the expected
values below were captured from the baseline scorer itself, not hand-computed
or derived from the new code.

How the expected values were produced: the baseline `utils/sourcing_archieved/
scoring.py` (commit ecfeaf9, before any SCORING_V2 / Stage 0 / TypeGate / Fit
work) was loaded in isolation and run against this exact battery; its outputs
were frozen as ``EXPECTED_FLAGOFF``. The current scorer with ``SCORING_V2``
unset MUST reproduce every value. Because the flag-off path never enters the
``if SCORING_V2:`` branches (T3 detection, T4 TypeGate, T5 Fit), the legacy
additive computation is literally the original code — so any deviation is a
real regression, not a reframing artifact.

The battery spans: clean-PN, placeholder-PN, manufacturer aliases (Crown
Triton / Hyundai Electric / Endress Hauser), equipment-vs-part categories,
collection URLs, marketplace counterfeit penalty, OEM home-page bonus, wrong
manufacturer, genuine PN mismatch, low-value landing pages, and the
wrong-part-on-a-component-request case (pump-on-seal) — i.e. every signal the
additive scorer exercises.

Stage 0 (T2) is the one documented exception: it is GATED by default
(``STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL=False``), so flag-off preserves the
legacy -30-on-placeholder behavior — and indeed ``seal_placeholder_realpn``
remains 25.0 here (the launch-demo byte-identical guarantee). The
seal_placeholder_realpn case therefore also audits the Stage 0 toggle's
default: if it ever drifts to 45 under flag-off, the toggle was flipped.
"""

import pytest

from utils.sourcing_archieved import scoring as scoring_mod
from utils.sourcing_archieved.scoring import _compute_suitability_score
from utils.models import AssetSpecs


# ---------------------------------------------------------------------------
# Fixtures (broad battery, spanning every additive-scorer signal)
# ---------------------------------------------------------------------------

def _seal():
    return AssetSpecs(manufacturer="Gusher Pumps", model="Type 21",
                      part_number="TYPE21", voltage="N/A",
                      category="Part", detected_type="mechanical seal")


def _motor():
    return AssetSpecs(manufacturer="Hyundai Heavy Industries", model="HHI-150-12-447T",
                      part_number="HHI-150-12-447T", voltage="460V",
                      category="Equipment", detected_type="Electric Motor", hp="150")


def _bearing():
    return AssetSpecs(manufacturer="SKF", model="6205", part_number="6205-2RS",
                      voltage="N/A", category="Part", detected_type="ball bearing")


def _valve():
    return AssetSpecs(manufacturer="Acme Industrial Corp", model="X100",
                      part_number="X100-A", voltage="N/A",
                      category="Part", detected_type="valve")


def _sensor():
    return AssetSpecs(manufacturer="Endress+Hauser", model="PMC11",
                      part_number="PMC11-AA1U1HBWBJJ", voltage="N/A",
                      category="Part", detected_type="pressure sensor")


def _seal_placeholder():
    return AssetSpecs(manufacturer="Goulds", model="3196", part_number="UNKNOWN-PN",
                      voltage="N/A", category="Part", detected_type="mechanical seal")


# (name, specs, snippet, url, found_pn, expected_flagoff_score)
# Expected values captured from the baseline scorer (commit ecfeaf9).
EXPECTED_FLAGOFF: list[tuple] = [
    ("seal_clean", _seal(),
     "Gusher Type 21 mechanical seal. In stock, ships same day.",
     "https://springer-pumps.com/seal-kits", None, 70.0),
    ("seal_placeholder_realpn", _seal_placeholder(),
     "Platinum Goulds 3196 mechanical seal, 1.375 inch, Type 1. Cross-reference / interchange. ST-1.375-T1. In stock.",
     "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st", "ST-1.375-T1", 25.0),
    ("motor_alias_crown", _motor(),
     "Crown Triton 150HP 447T frame motor, 460V 3-phase. Electric motor specialist. In stock, ships same day. Price: $3,200.",
     "https://dealersindustrial.com/motors/crown-triton-150hp", None, 35.0),
    ("motor_alias_hyundai", _motor(),
     "Hyundai Electric 150HP TEFC induction motor, frame 447T. In stock. distributor authorized. Price on request.",
     "https://mrosupply.com/motors/hyundai-electric-150hp", None, 43.0),
    ("bearing_exact", _bearing(),
     "SKF 6205-2RS deep groove ball bearing. In stock, ships today.",
     "https://mrosupply.com/bearings/skf-6205-2rs/", "6205-2RS", 95.0),
    ("valve_acme", _valve(),
     "Acme Industrial Corp X100 valve - in stock. Distributor.",
     "https://valve-supply.com/products/acme", None, 45.0),
    ("sensor_eh", _sensor(),
     "Endress Hauser PMC11 pressure sensor. Authorized distributor. PMC11AA1U1HBWBJJ in stock.",
     "https://instrumart.com/products/endress-hauser/pmc11", None, 70.0),
    ("collection_url", _seal(),
     "Gusher Type 21 seal kit page.",
     "https://vendor.com/search/gusher-type-21", None, 5.0),
    ("marketplace_seal", _seal(),
     "Gusher Type 21 mechanical seal bearing belt.",
     "https://ebay.com/itm/seal", None, 20.0),
    ("pump_on_seal_flagoff", _seal_placeholder(),
     "Goulds 3196 centrifugal pump, 5HP. In stock at Zoro.",
     "https://zoro.com/pump/centrifugal/goulds-3196/i/", None, 40.0),
    ("oem_home", _motor(),
     "Hyundai Heavy Industries HHI-150-12-447T motor. Buy now, add to cart. Price $5000.",
     "https://hyundai-electric.com/motors/hhi-150-12-447t", None, 43.0),
    ("wrong_mfg", _motor(),
     "Caterpillar industrial motors. Heavy equipment power solutions.",
     "https://cat.com/motors", None, 18.0),
    ("genuine_mismatch", _motor(),
     "unrelated motor content here",
     "https://vendor.com/motors/sku-999", "WEG-200-14-449T", 0.0),
    ("low_value_landing", _seal(),
     "Gusher Type 21 seal info page.",
     "https://blog.vendor.com/seals/type-21", None, 0.0),
]


# ---------------------------------------------------------------------------
# The wall — flag-off byte-identical to baseline across the whole battery
# ---------------------------------------------------------------------------

class TestInertnessWall:
    @pytest.fixture(autouse=True)
    def _flag_off(self, monkeypatch):
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)

    @pytest.mark.parametrize(
        "name,specs,snippet,url,found_pn,expected",
        EXPECTED_FLAGOFF,
        ids=[c[0] for c in EXPECTED_FLAGOFF],
    )
    def test_flag_off_matches_baseline(self, name, specs, snippet, url, found_pn, expected):
        score = _compute_suitability_score(specs, snippet, url, found_pn=found_pn)
        assert score == expected, (
            f"flag-off byte-identical regression on '{name}': "
            f"got {score}, baseline expected {expected}"
        )

    def test_detection_store_cleared_flag_off(self):
        """Flag-off must not leave noun-class detection state behind (T3 only
        runs under SCORING_V2). Audits that the flag-off path doesn't silently
        invoke the new detection machinery."""
        # Run any fixture; the store should be None on both keys afterward.
        _compute_suitability_score(_bearing(),
                                   "SKF 6205-2RS deep groove ball bearing. In stock, ships today.",
                                   "https://mrosupply.com/bearings/skf-6205-2rs/",
                                   found_pn="6205-2RS")
        assert scoring_mod._last_noun_classes["query"] is None
        assert scoring_mod._last_noun_classes["result"] is None


# ---------------------------------------------------------------------------
# Falsy-token parity — SCORING_V2 must fail safe (only 1/true/yes/on enable)
# ---------------------------------------------------------------------------

class TestFalsyTokenParity:
    @pytest.mark.parametrize("token", ["", "0", "false", "no", "off", "junk", None, "scoring_v2"])
    def test_falsy_token_is_flag_off(self, monkeypatch, token):
        """Every non-truthy token must parse to OFF and reproduce the baseline
        bearing score (95) — the gate must fail safe, never accidentally on."""
        assert scoring_mod._env_truthy(token) is False
        # Re-read the flag the way the module would, then set it explicitly to
        # mirror the parse, and assert parity with the baseline expected value.
        monkeypatch.setattr(scoring_mod, "SCORING_V2", scoring_mod._env_truthy(token))
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score = _compute_suitability_score(_bearing(),
                                           "SKF 6205-2RS deep groove ball bearing. In stock, ships today.",
                                           "https://mrosupply.com/bearings/skf-6205-2rs/",
                                           found_pn="6205-2RS")
        assert score == 95.0, f"falsy token {token!r} should be flag-off (95), got {score}"

    @pytest.mark.parametrize("token", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
    def test_truthy_token_enables_flag(self, monkeypatch, token):
        """The truthy tokens enable SCORING_V2; the bearing then drifts from 95
        (legacy) due to the intended T4 auth cap + T5 exact-PN demotion —
        proving the parse actually flips the path on (a stuck-off parse would
        leave the score at 95)."""
        assert scoring_mod._env_truthy(token) is True
        monkeypatch.setattr(scoring_mod, "SCORING_V2", True)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score = _compute_suitability_score(_bearing(),
                                           "SKF 6205-2RS deep groove ball bearing. In stock, ships today.",
                                           "https://mrosupply.com/bearings/skf-6205-2rs/",
                                           found_pn="6205-2RS")
        assert score != 95.0, (
            f"truthy token {token!r} should enable SCORING_V2 and drift the bearing off 95, got {score}"
        )
        assert score >= 30.0  # still passes the floor


# ---------------------------------------------------------------------------
# Stage 0 toggle default — flag-off placeholder case stays at legacy 25
# ---------------------------------------------------------------------------

class TestStage0ToggleDefaultAudit:
    def test_placeholder_case_stays_legacy_under_flag_off(self, monkeypatch):
        """Audits the Stage 0 toggle default (GATED): under flag-off the
        placeholder-PN seal keeps the legacy -30 penalty (25.0, below floor).
        If this drifts to ~45, the toggle was flipped to unconditional — which
        would change launch-demo scoring and must be a deliberate decision."""
        monkeypatch.setattr(scoring_mod, "SCORING_V2", False)
        monkeypatch.setattr(scoring_mod, "STAGE0_PLACEHOLDER_FIX_UNCONDITIONAL", False)
        score = _compute_suitability_score(_seal_placeholder(),
                                           "Platinum Goulds 3196 mechanical seal, 1.375 inch, Type 1. Cross-reference / interchange. ST-1.375-T1. In stock.",
                                           "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st",
                                           found_pn="ST-1.375-T1")
        assert score == 25.0, (
            f"Stage 0 toggle default must be GATED: placeholder case should stay "
            f"legacy 25.0 under flag-off, got {score} (toggle may have been flipped)"
        )
