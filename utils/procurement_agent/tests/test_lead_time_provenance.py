"""
Lead-time provenance marker (Stage 1) — the honesty flag that mirrors price_tbd.

Each candidate carries lead_time_source ∈ {extracted, defaulted, placeholder, quoted} so a
synthetic/heuristic lead time is never indistinguishable from a real one. This stage marks it
at the assignment sites; nothing downstream reads it yet (no behaviour change).
"""

from utils.models import SourcingOption, lead_time_source_for

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
