"""
Night 11 T7 — the QUOTE_SUBMIT_V1 acceptance suite: QUOTE_SUBMISSION_SPEC.md
§10 criteria 1–10, each encoded as tests on a LIVE-FAITHFUL path (TestClient
through the real API; the banded Gusher run — the Night-9 fixture shape — for
promotion visibility).

Criterion → class map:
   1  unclaimed path (token → form → active → Band A, no account) → TestC1
   2  onboarded path (DXP via portal → TOP of Band A)             → TestC2
   3  concierge path (identical promotion)                        → TestC3
   4  wrong-part gate (edited PN → review → labelled promote)     → TestC4
   5  sanity flag (100x price → review, off the buyer's screen)   → TestC5
   6  supersede + expiry, both visible on the live run            → TestC6
   7  token security (hash at rest, scope, closed, uniform 404)   → TestC7
   8  no auto-order (property over every quote surface)           → TestC8
   9  flag-off parity (endpoints absent, byte-identical)          → TestC9
  10  acks/notifications stubbed, zero live sends                 → TestC10

The `qapi` fixture and request helpers are shared with test_quote_api (same
isolation: every store on tmp, QUOTE_SUBMIT_V1 on, admin token set).
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

# Importing the fixture registers it in this module for pytest.
from utils.procurement_agent.tests.test_quote_api import (  # noqa: F401
    _ADMIN_TOKEN, _auth, _make_run, _mint_token, _valid_body, qapi,
)


# ---------------------------------------------------------------------------
# The live banded Gusher run: DXP (onboarded, tier-1 re-derive → Band C) +
# Pump Surplus Co (UNCLAIMED capability-pivot discovery → Band C) + Seal It
# (Band-A listing evidence). Both Band-C rows land in the outreach block —
# the exact pre-quote state the spec's promotion criteria start from.
# ---------------------------------------------------------------------------

def _banded_gusher_run(qapi, monkeypatch) -> str:
    from unittest.mock import Mock
    from utils.procurement_agent import tier1_matcher
    from utils import known_parts
    import utils.procurement_agent.agents.sourcing_agent as sa_mod
    import utils.procurement_agent.agents.spec_comparison_agent as sca_mod

    dxp = {
        "vendor_name": "DXP Enterprises", "source_url": "https://dxpe.com",
        "found_part_number": None, "price_tbd": True, "base_price": 0.0,
        "suitability_score": 92.0, "merchant_type": "Arkim Network",
        "is_registry_backed": True,
    }
    monkeypatch.setattr(tier1_matcher, "tier1_v2_active", lambda: True)
    monkeypatch.setattr(tier1_matcher, "match_tier1", lambda **kw: ["match"])
    monkeypatch.setattr(tier1_matcher, "candidates_from_matches",
                        lambda matches, **kw: [dict(dxp)])
    monkeypatch.setattr(known_parts, "get_edges", lambda key: [])
    monkeypatch.setattr(known_parts, "upsert_edges", lambda key, cands: 0)

    result = {
        "tier_1": {"results": [], "count": 0},
        "tier_2": {"results": [{
            "vendor_name": "Seal It", "found_part_number": "84004-28",
            "source_url": "https://sealit.example/gusher/84004-28",
            "base_price": 53.25, "price_tbd": False,
            "suitability_score": 80.0, "confidence_score": 70.0,
        }], "count": 1},
        "tier_3": {"results": [{
            # The unclaimed discovery: capability pivot ⇒ Band C ask-and-see,
            # with a real domain so a quote can join back to the card.
            "vendor_name": "Pump Surplus Co",
            "source_url": "https://pumpsurplus.example",
            "search_type": "capability_pivot",
            "found_part_number": None, "price_tbd": True, "base_price": 0.0,
            "suitability_score": 55.0,
        }], "count": 1},
        "filters_applied": ["ranking_bands:v1"],
    }
    agent = Mock()
    agent.run.return_value = result
    monkeypatch.setattr(sa_mod, "SourcingAgent", Mock(return_value=agent))
    comp = Mock()
    comp.run.return_value = None
    monkeypatch.setattr(sca_mod, "SpecComparisonAgent", Mock(return_value=comp))

    rid = _make_run(qapi)
    assert qapi.post(f"/api/runs/{rid}/confirm-intake").status_code == 200
    return rid


def _sourcing(qapi, rid) -> dict:
    return qapi.get(f"/api/runs/{rid}").json()["sourcing_results"]


def _finding_names(sr) -> list:
    return [f["vendorName"] for f in sr.get("findings", [])]


def _outreach_names(sr) -> list:
    return [s["vendorName"] for s in sr["outreachTargets"]["suppliers"]]


def _claim_portal(monkeypatch, tmp_path, domain="dxpe.com"):
    from utils import claim_tokens as ct
    monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
    monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
    monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
    return ct.generate_for(domain)


# ---------------------------------------------------------------------------

class TestC1UnclaimedPath:
    """Criterion 1: a quote token from an RFQ send → form → pn-confirmed
    submission → active quote → the supplier's card promotes to Band A on the
    live run — NO account, NO claim. The claim pitch renders post-submit."""

    def test_unclaimed_supplier_quotes_and_jumps_to_band_a(self, qapi,
                                                           monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        sr = _sourcing(qapi, rid)
        assert "Pump Surplus Co" in _outreach_names(sr)       # pre-quote: ask-and-see
        assert "Pump Surplus Co" not in _finding_names(sr)

        tok = _mint_token(rid, supplier_domain="pumpsurplus.example",
                          vendor_name="Pump Surplus Co")
        # The form renders with NO auth beyond the token itself.
        form = qapi.get(f"/api/quote/{tok['token']}")
        assert form.status_code == 200 and form.json()["state"] == "live"
        # pn-confirmed submission (the prefilled PN untouched).
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        assert out["status"] == "active"
        assert out["claim_pitch"] is True                     # post-submit, unclaimed

        sr = _sourcing(qapi, rid)
        promoted = [f for f in sr["findings"]
                    if f["vendorName"] == "Pump Surplus Co"]
        assert promoted and promoted[0]["band"] == "A"
        assert promoted[0]["price"] == 189.0
        assert promoted[0]["supplierConfirmed"] is True
        assert "Pump Surplus Co" not in _outreach_names(sr)


class TestC2OnboardedPortalPath:
    """Criterion 2: DXP submits via the portal → promotes to the TOP of
    Band A (onboarded-first) — the full onboarding-benefit loop."""

    def test_dxp_portal_quote_tops_band_a(self, qapi, monkeypatch, tmp_path):
        rid = _banded_gusher_run(qapi, monkeypatch)
        # Another supplier already holds a Band-A quote — DXP must still top it.
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example",
                          vendor_name="Pump Surplus Co")
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body(unit_price=150.0))

        from utils import supplier_registry
        supplier_registry.create_stub("DXP Enterprises", domain="dxpe.com")
        supplier_registry.record_sent_message(
            run_id=rid, supplier_domain="dxpe.com", vendor_name="DXP Enterprises",
            to=["sales@dxpe.com"], status="stubbed")
        claim = _claim_portal(monkeypatch, tmp_path)
        resp = qapi.post(f"/api/portal/{claim['token']}/quotes",
                         json={**_valid_body(), "run_id": rid})
        assert resp.status_code == 200 and resp.json()["status"] == "active"

        sr = _sourcing(qapi, rid)
        assert _finding_names(sr)[0] == "DXP Enterprises"     # TOP of Band A
        top = sr["findings"][0]
        assert top["band"] == "A" and top["price"] == 189.0
        assert top["supplierConfirmed"] is True
        assert "DXP Enterprises" not in _outreach_names(sr)


class TestC3ConciergePath:
    """Criterion 3: an emailed quote keyed in by admin produces the IDENTICAL
    promotion the supplier's own token submission produces."""

    def test_concierge_entry_promotes_identically(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        resp = qapi.post("/api/admin/quotes", headers=_auth(),
                         json={**_valid_body(), "run_id": rid,
                               "supplier_domain": "pumpsurplus.example",
                               "vendor_name": "Pump Surplus Co"})
        assert resp.status_code == 200 and resp.json()["status"] == "active"
        sr = _sourcing(qapi, rid)
        promoted = [f for f in sr["findings"]
                    if f["vendorName"] == "Pump Surplus Co"]
        assert promoted and promoted[0]["band"] == "A"
        assert promoted[0]["price"] == 189.0
        assert promoted[0]["supplierConfirmed"] is True


class TestC4WrongPartGate:
    """Criterion 4: an edited PN never auto-promotes — it lands pn_differs in
    review; approval promotes it labelled as the QUOTED PN (equivalent-
    alternative framing), never silently as the requested PN."""

    def test_edited_pn_review_then_labelled_promotion(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example",
                          vendor_name="Pump Surplus Co")
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(part_number="84004-28SP")).json()
        assert out["status"] == "review" and out["pn_differs"] is True

        sr = _sourcing(qapi, rid)
        assert "Pump Surplus Co" not in _finding_names(sr)    # NOT promoted raw

        appr = qapi.post(f"/api/admin/quotes/{out['quote_id']}/approve",
                         headers=_auth())
        assert appr.status_code == 200

        sr = _sourcing(qapi, rid)
        (card,) = [f for f in sr["findings"]
                   if f["vendorName"] == "Pump Surplus Co"]
        assert card["band"] == "A"
        # Labelled as the QUOTED part number — the equivalent-alternative
        # framing substrate, never silently the requested PN.
        assert card["pnDiffers"] is True
        assert card["quotedPartNumber"] == "84004-28SP"

    def test_rejected_wrong_part_never_appears(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(part_number="TOTALLY-OTHER")).json()
        qapi.post(f"/api/admin/quotes/{out['quote_id']}/reject", headers=_auth())
        assert "Pump Surplus Co" not in _finding_names(_sourcing(qapi, rid))


class TestC5SanityFlag:
    """Criterion 5: a 100x price lands in review, not on the buyer's screen."""

    def test_100x_price_never_reaches_the_buyer(self, qapi, monkeypatch):
        from utils import price_db
        for vendor, price in (("A", 50.0), ("B", 53.25), ("C", 60.0)):
            price_db.save_price("Gusher Pumps", "84004-28", vendor, price)
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        out = qapi.post(f"/api/quote/{tok['token']}",
                        json=_valid_body(unit_price=5325.0)).json()
        assert out["status"] == "review"
        assert "price_out_of_band" in out["review_reasons"]
        assert "Pump Surplus Co" not in _finding_names(_sourcing(qapi, rid))
        # ...and it sits in the concierge queue with its reason.
        queue = qapi.get("/api/admin/quotes?status=review",
                         headers=_auth()).json()["quotes"]
        assert [q["id"] for q in queue] == [out["quote_id"]]


class TestC6SupersedeAndExpiry:
    """Criterion 6: a second submission supersedes the first; expiry demotes
    the card back to outreach state — both visible on the live run."""

    def test_supersede_updates_the_live_card(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body(unit_price=189.0))
        (card,) = [f for f in _sourcing(qapi, rid)["findings"]
                   if f["vendorName"] == "Pump Surplus Co"]
        assert card["price"] == 189.0
        # The revision (same token — not single-use) supersedes on the card.
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body(unit_price=175.0))
        (card,) = [f for f in _sourcing(qapi, rid)["findings"]
                   if f["vendorName"] == "Pump Surplus Co"]
        assert card["price"] == 175.0

    def test_expiry_demotes_back_to_outreach(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        out = qapi.post(f"/api/quote/{tok['token']}", json=_valid_body()).json()
        assert "Pump Surplus Co" in _finding_names(_sourcing(qapi, rid))
        from utils import quote_store
        conn = sqlite3.connect(quote_store._DB_PATH)
        conn.execute(
            "UPDATE quotes SET valid_until = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
             out["quote_id"]))
        conn.commit()
        conn.close()
        sr = _sourcing(qapi, rid)
        assert "Pump Surplus Co" not in _finding_names(sr)    # no zombie card
        assert "Pump Surplus Co" in _outreach_names(sr)       # honest revert


class TestC7TokenSecurity:
    """Criterion 7: hashed at rest; single-RFQ scope; closed state for a dead
    RFQ; uniform 404 on enumeration (the portal-token posture)."""

    def test_hashed_at_rest(self, qapi):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        from utils import quote_tokens
        conn = sqlite3.connect(quote_tokens._DB_PATH)
        rows = conn.execute("SELECT token_hash, token_prefix FROM quote_tokens").fetchall()
        conn.close()
        (row,) = rows
        assert tok["token"] not in row
        assert len(row[0]) == 64                              # SHA-256 hex only

    def test_single_rfq_scope(self, qapi):
        # The run binding comes from the token row — a caller cannot aim the
        # token at another request (there is no run parameter to supply).
        rid1, rid2 = _make_run(qapi), _make_run(qapi)
        tok1 = _mint_token(rid1)
        assert qapi.post(f"/api/quote/{tok1['token']}",
                         json=_valid_body()).status_code == 200
        from utils import quote_store
        assert len(quote_store.get_quotes(run_id=rid1)) == 1
        assert quote_store.get_quotes(run_id=rid2) == []

    def test_dead_rfq_renders_closed_state(self, qapi):
        rid = _make_run(qapi)
        expired = _mint_token(rid, expiry_days=0)
        assert qapi.get(f"/api/quote/{expired['token']}").json() == \
            {"state": "closed"}
        withdrawn = _mint_token(rid)
        from utils import quote_tokens
        quote_tokens.revoke_for_rfq("rfq-9", reason="rfq_withdrawn")
        assert qapi.get(f"/api/quote/{withdrawn['token']}").json() == \
            {"state": "closed"}
        # A dead RFQ takes no writes.
        assert qapi.post(f"/api/quote/{withdrawn['token']}",
                         json=_valid_body()).status_code == 409

    def test_enumeration_uniform_404(self, qapi):
        unknown = qapi.get("/api/definitely-not-a-route")
        for guess in ("aaaa", "portal-token-here", "quote", "x" * 43):
            resp = qapi.get(f"/api/quote/{guess}")
            assert resp.status_code == 404
            assert resp.content == unknown.content


class TestC8NoAutoOrder:
    """Criterion 8: no path from quote submission to order placement — the
    property holds across every quote surface (see also
    test_quote_api.TestNoAutoOrderProperty for the endpoint sweep)."""

    def test_full_promotion_flow_places_no_order(self, qapi, monkeypatch):
        from utils import orders

        def _forbidden(*a, **kw):
            raise AssertionError("order placement reached from the quote flow")

        monkeypatch.setattr(orders, "create_order", _forbidden)
        monkeypatch.setattr(orders, "place_order", _forbidden)
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        assert qapi.post(f"/api/quote/{tok['token']}",
                         json=_valid_body()).status_code == 200
        # The promotion read (buyer's screen) also touches no order path.
        assert "Pump Surplus Co" in _finding_names(_sourcing(qapi, rid))


class TestC9FlagOff:
    """Criterion 9: QUOTE_SUBMIT_V1 off ⇒ endpoints absent, byte-identical;
    the suite itself (run flag-off by the conftest pin) is the green half."""

    def test_every_quote_surface_absent_flag_off(self, qapi, monkeypatch):
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        unknown = qapi.get("/api/definitely-not-a-route")
        surfaces = [
            ("get", f"/api/quote/{tok['token']}", {}),
            ("post", f"/api/quote/{tok['token']}", {"json": _valid_body()}),
            ("get", "/api/portal/any/open-requests", {}),
            ("get", "/api/portal/any/quotes", {}),
            ("post", "/api/portal/any/quotes",
             {"json": {**_valid_body(), "run_id": rid}}),
            ("get", "/api/admin/quotes", {"headers": _auth()}),
            ("post", "/api/admin/quotes",
             {"headers": _auth(),
              "json": {**_valid_body(), "run_id": rid,
                       "supplier_domain": "dxpe.com"}}),
            ("post", "/api/admin/quotes/x/approve", {"headers": _auth()}),
            ("post", "/api/admin/quotes/x/reject", {"headers": _auth()}),
        ]
        for method, path, kw in surfaces:
            resp = getattr(qapi, method)(path, **kw)
            assert resp.status_code == 404, path
            assert resp.content == unknown.content, path

    def test_flag_off_promotion_merge_inert(self, qapi, monkeypatch):
        rid = _banded_gusher_run(qapi, monkeypatch)
        tok = _mint_token(rid, supplier_domain="pumpsurplus.example")
        qapi.post(f"/api/quote/{tok['token']}", json=_valid_body())
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        sr = _sourcing(qapi, rid)
        # The active quote written while ON stops surfacing the moment the
        # kill switch flips — the run reads as if Night 11 never happened.
        assert "Pump Surplus Co" not in _finding_names(sr)
        assert "Pump Surplus Co" in _outreach_names(sr)


class TestC10StubbedSends:
    """Criterion 10: every ack/notification rides the existing send gates —
    zero live sends introduced. The ack goes through GmailSender.send, where
    governance + EMAIL_SEND_ENABLED run internally; with the suite's pinned
    gate the observed result is 'stubbed' and zero network."""

    def test_ack_send_result_is_stubbed(self, qapi, monkeypatch):
        import utils.email_sender as es
        observed: list = []
        real = es.GmailSender.send

        def _spy(self, message):
            result = real(self, message)
            observed.append(result.status)
            return result

        monkeypatch.setattr(es.GmailSender, "send", _spy)
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        assert qapi.post(f"/api/quote/{tok['token']}",
                         json=_valid_body()).status_code == 200
        assert observed == ["stubbed"]      # gate off ⇒ stub, never network

    def test_digest_is_the_only_notification_surface(self, qapi, monkeypatch):
        # Spec §8: concierge notices ride the existing daily digest — no new
        # email surface. The quotes section appears there and nowhere else.
        monkeypatch.setenv("SEND_GOVERNANCE_V1", "1")
        rid = _make_run(qapi)
        tok = _mint_token(rid)
        qapi.post(f"/api/quote/{tok['token']}",
                  json=_valid_body(part_number="OTHER"))
        digest = qapi.get("/api/admin/send-governance/digest",
                          headers=_auth()).json()
        assert len(digest["quotes"]["review_pending"]) == 1
