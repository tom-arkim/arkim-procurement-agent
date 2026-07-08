"""
Night 5 — Tier 1 runtime NOTIFY tests (T3: the notify≫display asymmetry).

Covers:
  - The notify gate: brand-match OR core-class clears notify; brand-neutral +
    incidental-class is displayed but NOT notified.
  - The per-RFQ cap (5-8): at most `cap` suppliers notified, best-scored first;
    over-cap qualifiers are recorded as capped_out.
  - Notification EVENTS are recorded (supplier_notifications table) with the send
    behind the stubbed/flagged EmailSender.
  - Zero live sends possible: the double-gate (EMAIL_SEND_ENABLED=False (conftest
    safety net) + TIER1_V2) is asserted — every send_status is "stubbed", never
    "sent". A fake EmailSender is injected for the cap/gate tests so the send seam
    is deterministic and offline.
  - Flag-off → [] (the notify surface is dormant — T5 inertness).
  - Fail-soft: a sender that raises is recorded as "error", never crashes the run.

The notify layer is exercised DIRECTLY here; the live-faithful path (through the
real _run_tier1 → notify via the API) is in test_tier1_runtime_live.py.
"""
from __future__ import annotations

import pytest

from utils import supplier_registry as sr
from utils import email_sender
from utils.procurement_agent import tier1_matcher as tm
from utils.procurement_agent import tier1_notify as tn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def reg(tmp_path, monkeypatch):
    """Isolated supplier_registry + TIER1_V2 ON + EMAIL_SEND_ENABLED OFF (the
    conftest safety net already force-sets it, but be explicit for the double-gate
    assertions)."""
    monkeypatch.setattr(sr, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(sr, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(sr, "TIER1_V2", True)
    monkeypatch.setattr(email_sender, "EMAIL_SEND_ENABLED", False)
    return sr


class _FakeSendResult:
    def __init__(self, status="stubbed", message_id=None):
        self.status = status
        self.message_id = message_id


class _FakeSender:
    """A recording fake EmailSender — offline, deterministic. Always stubbed unless
    constructed with a status override. Raises on send if ``raises`` is set."""
    def __init__(self, status="stubbed", raises=None, message_id="fake-mid-1"):
        self.status = status
        self.raises = raises
        self.message_id = message_id
        self.sent: list = []

    def send(self, message):
        self.sent.append(message)
        if self.raises:
            raise self.raises
        return _FakeSendResult(status=self.status, message_id=self.message_id)


def _onboard(reg, domain, name, *, classes, brands=None, ship_area=None):
    """Onboard a supplier with the given scope (mirrors test_tier1_matcher._onboard)."""
    reg._ensure_supplier_row(domain, name=name)
    reg.set_supplier_classes(domain, [
        {"class_id": c["class_id"], "is_core": c.get("is_core", False),
         "confidence": 0.8, "source": "manual"}
        for c in classes
    ])
    if brands:
        reg.set_supplier_brands(domain, [
            {"brand_id": b["brand_id"], "relationship": b["relationship"],
             "confidence": 0.9, "source": "manual"}
            for b in brands
        ])
    if ship_area:
        reg.set_supplier_territory(domain, ship_area)
    reg.tier1_transition(domain, "discovered")
    reg.tier1_transition(domain, "contacted")
    reg.tier1_transition(domain, "quoted")
    reg.tier1_transition(domain, "onboarding")
    reg.tier1_transition(domain, "onboarded")
    return reg.lookup_by_domain(domain)


# ---------------------------------------------------------------------------
# The notify gate (brand-match OR core-class)
# ---------------------------------------------------------------------------

class TestNotifyGate:
    def test_core_class_clears_gate(self, reg):
        """A brand-neutral, CORE-class match clears the notify gate (core-class)."""
        _onboard(reg, "sealco.com", "SealCo",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert len(res) == 1
        assert res[0].notified is True
        assert res[0].notify_reason == "core_class"

    def test_brand_match_clears_gate(self, reg):
        """A brand-matched, INCIDENTAL-class match clears the notify gate (brand-match)."""
        _onboard(reg, "carries.com", "CarriesCo",
                 classes=[{"class_id": "SEAL", "is_core": False}],
                 brands=[{"brand_id": "Goulds", "relationship": "CARRIES"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert res[0].notified is True
        assert res[0].notify_reason == "brand_match"

    def test_brand_neutral_incidental_not_notified(self, reg):
        """A brand-neutral, INCIDENTAL-class match is DISPLAYED but NOT notified —
        the conservative asymmetry (notify threshold > display threshold)."""
        _onboard(reg, "broadline.com", "Broadline",
                 classes=[{"class_id": "SEAL", "is_core": False}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert len(res) == 1
        assert res[0].notified is False
        assert res[0].send_status == "not_notified"
        assert res[0].capped_out is False
        # No notification event recorded for a not-notified match.
        assert sr.get_supplier_notifications(run_id="r1") == []

    def test_aftermarket_brand_match_clears_gate(self, reg):
        """An AFTERMARKET_COMPATIBLE brand match clears the notify gate (brand-match)."""
        _onboard(reg, "aftermarket.com", "AftermarketShop",
                 classes=[{"class_id": "SEAL", "is_core": False}],
                 brands=[{"brand_id": "Goulds",
                          "relationship": "AFTERMARKET_COMPATIBLE"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert res[0].notified is True
        assert res[0].notify_reason == "brand_match"

    def test_brand_match_and_core_carries_both_reason(self, reg):
        """A match with BOTH brand-match AND core-class records the combined reason."""
        _onboard(reg, "auth.com", "AuthDistributor",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert res[0].notified is True
        assert res[0].notify_reason == "brand_match_or_core_class"


# ---------------------------------------------------------------------------
# The per-RFQ cap (5-8)
# ---------------------------------------------------------------------------

class TestNotifyCap:
    def test_cap_enforced_best_scored_first(self, reg):
        """8 qualifying suppliers, cap=6 → exactly 6 notified (best-scored first), 2
        capped_out. The authorized brand suppliers (highest score) are notified; the
        lowest-scoring core-class-only suppliers are capped."""
        # 2 AUTHORIZED (score ~highest), 2 CARRIES, 4 core-class-only (lowest).
        for i in range(2):
            _onboard(reg, f"auth{i}.com", f"Auth{i}",
                     classes=[{"class_id": "SEAL", "is_core": True}],
                     brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                     ship_area={"kind": "NATIONWIDE_US"})
        for i in range(2):
            _onboard(reg, f"carries{i}.com", f"Carries{i}",
                     classes=[{"class_id": "SEAL", "is_core": True}],
                     brands=[{"brand_id": "Goulds", "relationship": "CARRIES"}],
                     ship_area={"kind": "NATIONWIDE_US"})
        for i in range(4):
            _onboard(reg, f"core{i}.com", f"Core{i}",
                     classes=[{"class_id": "SEAL", "is_core": True}],
                     ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        assert len(matches) == 8
        res = tn.notify_tier1(matches, run_id="r1", cap=6, sender=_FakeSender())
        notified = [r for r in res if r.notified]
        capped = [r for r in res if r.capped_out]
        assert len(notified) == 6
        assert len(capped) == 2
        # The 2 capped are the lowest-scored (core-class-only, no brand).
        assert all(r.notify_reason == "core_class" for r in capped)
        # The 6 notified: the 4 brand-matched (2 AUTH + 2 CARRIES, all core+brand →
        # brand_match_or_core_class) + the 2 highest core-class-only.
        notified_reasons = sorted(r.notify_reason for r in notified)
        assert notified_reasons.count("brand_match_or_core_class") == 4
        assert notified_reasons.count("core_class") == 2
        # The notified set includes all 4 brand-matched suppliers (best-scored).
        notified_domains = {r.supplier_domain for r in notified}
        assert {"auth0.com", "auth1.com", "carries0.com", "carries1.com"} <= notified_domains
        # The capped set is core-only suppliers (the lowest-scored two).
        assert all(r.supplier_domain.startswith("core") for r in capped)

    def test_cap_zero_notifies_none(self, reg):
        """cap=0 → no supplier notified (all qualifying matches capped_out)."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", cap=0, sender=_FakeSender())
        assert len(res) == 1
        assert res[0].notified is False
        assert res[0].capped_out is True
        assert sr.get_supplier_notifications(run_id="r1") == []

    def test_cap_in_default_5_to_8_range(self):
        """The default cap sits in the brief's 5-8 range."""
        assert 5 <= tn.NOTIFY_CAP_DEFAULT <= 8


# ---------------------------------------------------------------------------
# Notification events recorded + zero live sends (the double-gate)
# ---------------------------------------------------------------------------

class TestNotificationEventsAndDoubleGate:
    def test_event_recorded_with_stubbed_status(self, reg):
        """A notified supplier has a supplier_notifications event recorded with
        send_status='stubbed' (the EmailSender gate is OFF — the conftest safety net
        + this fixture's explicit EMAIL_SEND_ENABLED=False)."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender(status="stubbed"))
        assert res[0].notified is True
        events = sr.get_supplier_notifications(run_id="r1")
        assert len(events) == 1
        e = events[0]
        assert e["send_status"] == "stubbed"     # NOTHING sent live
        assert e["notify_reason"] == "brand_match_or_core_class"
        assert e["noun_class"] == "SEAL"
        assert e["threshold"] == "notify"
        assert e["supplier_domain"] == "auth.com"

    def test_no_live_send_possible_default_sender(self, reg):
        """With the DEFAULT GmailSender + EMAIL_SEND_ENABLED=False (the repo/test
        default), the send is stubbed — zero live sends possible. This is the
        double-gate asserted end-to-end through the real send seam (not a fake)."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        # Default sender (GmailSender) — no fake injected.
        res = tn.notify_tier1(matches, run_id="r1")
        assert res[0].send_status == "stubbed"
        assert res[0].send_status != "sent"
        events = sr.get_supplier_notifications(run_id="r1")
        assert all(e["send_status"] != "sent" for e in events)
        assert all(e["send_status"] == "stubbed" for e in events)

    def test_send_status_reflects_sender_result(self, reg):
        """The recorded send_status mirrors the EmailSender's SendResult.status."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        # A fake sender returning a non-stubbed status is reflected faithfully.
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender(status="sent",
                                                                       message_id="mid-9"))
        assert res[0].send_status == "sent"
        assert res[0].message_id == "mid-9"
        events = sr.get_supplier_notifications(run_id="r1")
        assert events[0]["send_status"] == "sent"
        assert events[0]["message_id"] == "mid-9"

    def test_no_recipient_recorded_as_stubbed(self, reg, monkeypatch):
        """A supplier with no resolvable contact → the notify is recorded as stubbed
        (the FYI is queued, not sent) — the event is NOT silently dropped."""
        _onboard(reg, "nocontact.com", "NoContact",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 ship_area={"kind": "NATIONWIDE_US"})
        # assemble_recipient_set constructs sales@{domain} on a miss, so to exercise
        # the no-recipient branch we monkeypatch it on the registry module (the notify
        # layer calls sr.assemble_recipient_set). Using monkeypatch.setattr (not a
        # manual setattr/del) guarantees the restore and avoids leaking the deletion
        # to other tests in the session.
        def _no_recipients(domain_or_url):
            return {"to": [], "cc": [], "status": "needs_human"}
        monkeypatch.setattr(sr, "assemble_recipient_set", _no_recipients)
        matches = tm.match_tier1(detected_type="mechanical seal",
                                 manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert res[0].notified is True
        assert res[0].send_status == "stubbed"  # no recipient → queued, not sent


# ---------------------------------------------------------------------------
# Fail-soft: a sender that raises is recorded as "error", never crashes the run
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_sender_raises_recorded_as_error(self, reg):
        """A sender that raises is recorded with send_status='error' — fail-soft,
        never raises into the sourcing pipeline, never blocks the run."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        res = tn.notify_tier1(matches, run_id="r1",
                              sender=_FakeSender(raises=RuntimeError("boom")))
        assert res[0].send_status == "error"
        assert res[0].notified is False  # an error is not a successful notify
        events = sr.get_supplier_notifications(run_id="r1")
        assert events[0]["send_status"] == "error"


# ---------------------------------------------------------------------------
# Flag-off inertness (T5)
# ---------------------------------------------------------------------------

class TestFlagOffInertness:
    def test_flag_off_returns_empty(self, reg, monkeypatch):
        """TIER1_V2 OFF → notify_tier1 returns [] always (the notify surface is
        dormant, byte-identical to pre-Night-5). Onboard under flag-on (so scope
        writes land), build matches, then flip the flag off for the notify call."""
        _onboard(reg, "auth.com", "Auth",
                 classes=[{"class_id": "SEAL", "is_core": True}],
                 brands=[{"brand_id": "Goulds", "relationship": "AUTHORIZED"}],
                 ship_area={"kind": "NATIONWIDE_US"})
        matches = tm.match_tier1(detected_type="mechanical seal", manufacturer="Goulds")
        assert len(matches) == 1
        # Flip the flag OFF for the notify call (the notify surface must be dormant).
        monkeypatch.setattr(sr, "TIER1_V2", False)
        res = tn.notify_tier1(matches, run_id="r1", sender=_FakeSender())
        assert res == []
        # No notification events recorded flag-off (record_supplier_notification no-ops).
        assert sr.get_supplier_notifications(run_id="r1") == []
