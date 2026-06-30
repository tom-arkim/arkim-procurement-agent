"""
Lead-time provenance marker (Stage 1) — the honesty flag that mirrors price_tbd.

Each candidate carries lead_time_source ∈ {extracted, defaulted, placeholder, quoted} so a
synthetic/heuristic lead time is never indistinguishable from a real one. This stage marks it
at the assignment sites; nothing downstream reads it yet (no behaviour change).
"""

from utils.models import SourcingOption, lead_time_source_for, lead_time_speed_confidence

ALLOWED = {"extracted", "defaulted", "placeholder", "quoted"}


class TestLeadTimeSourceHelper:
    def test_present_value_is_extracted(self):
        # Tier-1 catalog: a row that actually states lead_days is a real (extracted) value.
        assert lead_time_source_for({"lead_days": 3}) == "extracted"
        assert lead_time_source_for({"lead_days": 0}) == "extracted"   # 0 is a stated value, not "missing"

    def test_absent_or_none_is_defaulted(self):
        # No stated value -> the caller falls to a synthetic default -> 'defaulted', never 'extracted'.
        assert lead_time_source_for({}) == "defaulted"
        assert lead_time_source_for({"lead_days": None}) == "defaulted"
        assert lead_time_source_for({"other": 5}) == "defaulted"

    def test_custom_key(self):
        assert lead_time_source_for({"ld": 4}, key="ld") == "extracted"
        assert lead_time_source_for({"lead_days": 4}, key="ld") == "defaulted"

    def test_only_returns_allowed_values(self):
        for item in ({"lead_days": 2}, {}, {"lead_days": None}):
            assert lead_time_source_for(item) in ALLOWED


class TestSourcingOptionDefault:
    def test_default_is_defaulted_conservative(self):
        # The conservative default covers every Tier-2 SourcingOption (marketplace/cache/national/
        # aftermarket): an LLM/heuristic lead time is instructed to default for unknown, so it is
        # never trusted as 'extracted' — it reads 'defaulted' unless a caller proves otherwise.
        opt = SourcingOption(
            vendor_name="V", base_price=10.0, lead_time_days=5,
            reliability_score=80.0, merchant_type="National Specialist",
        )
        assert opt.lead_time_source == "defaulted"

    def test_can_be_set_explicitly(self):
        opt = SourcingOption(
            vendor_name="V", base_price=10.0, lead_time_days=2,
            reliability_score=95.0, merchant_type="Arkim Network",
            lead_time_source="extracted",
        )
        assert opt.lead_time_source == "extracted" and opt.lead_time_source in ALLOWED


# ---------------------------------------------------------------------------
# Stage 4 — scoring: a fabricated/estimated lead time must not out-rank a genuinely-known
# one on SPEED. Provenance gates the speed contribution at both ranking sites.
# ---------------------------------------------------------------------------

class TestLeadTimeSpeedConfidence:
    def test_confidence_by_provenance(self):
        assert lead_time_speed_confidence("extracted") == 1.0
        assert lead_time_speed_confidence("quoted") == 1.0
        assert lead_time_speed_confidence("defaulted") == 0.5
        assert lead_time_speed_confidence("placeholder") == 0.0
        assert lead_time_speed_confidence(None) == 0.5          # conservative
        assert lead_time_speed_confidence("weird") == 0.5


def _opt(source: str, days: int = 2) -> SourcingOption:
    # Identical except lead-time provenance, so only the speed multiplier differs.
    return SourcingOption(vendor_name="v", base_price=100.0, lead_time_days=days,
                          reliability_score=80.0, merchant_type="National Specialist",
                          lead_time_source=source)


class TestSpeedScoreProvenanceQuoting:
    def test_extracted_outranks_defaulted_outranks_placeholder_same_days(self):
        from utils.quoting import _compute_tca_score
        ext = _compute_tca_score(_opt("extracted"))
        dfl = _compute_tca_score(_opt("defaulted"))
        plc = _compute_tca_score(_opt("placeholder"))
        assert ext > dfl > plc          # a real fast lead beats an estimated one beats a fabricated one

    def test_placeholder_contributes_zero_speed(self):
        from utils.quoting import _compute_tca_score, SPEED_WEIGHT, MAX_LEAD_TIME
        # A placeholder's fake fast lead yields the SAME score as a slow placeholder -> no speed credit.
        fast = _compute_tca_score(_opt("placeholder", days=1))
        slow = _compute_tca_score(_opt("placeholder", days=13))
        assert fast == slow

    def test_defaulted_is_half_of_extracted_speed(self):
        from utils.quoting import _compute_tca_score, SPEED_WEIGHT, MAX_LEAD_TIME
        # The only difference is the speed multiplier (1.0 vs 0.5) -> a known delta.
        ext = _compute_tca_score(_opt("extracted"))
        dfl = _compute_tca_score(_opt("defaulted"))
        full_speed = max(0.0, (MAX_LEAD_TIME - 2) / MAX_LEAD_TIME) * 100
        assert round(ext - dfl, 2) == round(full_speed * 0.5 * SPEED_WEIGHT, 2)


class TestRankSpeedProvenance:
    def _rank_ids(self, cands):
        from utils.procurement_agent.agents.sourcing_agent import SourcingAgent
        ranked = SourcingAgent()._rank(cands, {"price": 0.0, "speed": 1.0, "reliability": 0.0})
        return [c["id"] for c in ranked]

    def test_real_fast_lead_outranks_estimated_and_placeholder(self):
        cands = [
            {"id": "ext", "base_price": 100.0, "reliability_score": 80, "lead_time_days": 2, "lead_time_source": "extracted"},
            {"id": "dfl", "base_price": 100.0, "reliability_score": 80, "lead_time_days": 2, "lead_time_source": "defaulted"},
            {"id": "plc", "base_price": 100.0, "reliability_score": 80, "lead_time_days": 2, "lead_time_source": "placeholder"},
        ]
        assert self._rank_ids(cands) == ["ext", "dfl", "plc"]   # speed weight only -> pure provenance order

    def test_missing_lead_gets_no_fabricated_speed(self):
        # Previously `or 7` gave a missing lead a free ~7-day speed bonus; now it gets 0.
        cands = [
            {"id": "real", "base_price": 100.0, "reliability_score": 80, "lead_time_days": 8, "lead_time_source": "extracted"},
            {"id": "missing", "base_price": 100.0, "reliability_score": 80},  # no lead_time_days, no source
        ]
        # real 8-day extracted lead must beat the missing-lead candidate (which used to be "7 days").
        assert self._rank_ids(cands) == ["real", "missing"]
