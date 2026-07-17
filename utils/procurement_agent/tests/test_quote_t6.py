"""
Night 11 T6 — buyer-side card fields, the {quote_link} template placeholder,
and the send-time substitution (QUOTE_SUBMIT_V1).

Covers:
  - RFQ template: flag OFF ⇒ byte-identical to rfq-v1 (no placeholder); flag
    ON ⇒ the QUOTE_LINK_LINE appended mechanically (founder copy untouched),
  - send-time substitution in rfq_send: token minted per RFQ send (single-RFQ
    scope, recorded in the quote_tokens store), the link replaces the
    placeholder in BOTH the delivered body and the recorded sent_messages row,
    the raw placeholder NEVER reaches a supplier (flag-off stale draft and
    mint-failure both drop the line whole), flag-off no-placeholder drafts are
    returned untouched,
  - _quote_overlay: structured-quote payloads add quoteConfirmedAt /
    quoteId / pnDiffers / quotedPartNumber; legacy email-quote payloads keep
    the EXACT pre-Night-11 key set (parity pinned).
"""
from __future__ import annotations

import sqlite3

import pytest

from utils import quote_tokens as qt
from utils.procurement_agent import outreach


@pytest.fixture()
def stores(monkeypatch, tmp_path):
    """Isolated supplier_registry + quote_tokens stores, flag OFF by default
    (the conftest pin); tests opt in per-case."""
    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH",
                        str(tmp_path / "supplier_registry.sqlite"))
    monkeypatch.setattr(qt, "_DB_PATH", str(tmp_path / "quote_tokens.sqlite"))
    return tmp_path


_SPECS = {"manufacturer": "Gusher Pumps", "model": "", "part_number": "84004-28"}


# ---------------------------------------------------------------------------
# Template placeholder (outreach._make_draft)
# ---------------------------------------------------------------------------

class TestTemplatePlaceholder:
    def test_flag_off_template_is_byte_identical_no_placeholder(self):
        draft = outreach._make_draft("DXP Enterprises", _SPECS)
        assert "{quote_link}" not in draft
        assert "Submit your quote" not in draft
        # The founder letter, unchanged end-to-end.
        assert draft.endswith("Regards,\nArkim Procurement\nprocurement@arkim.ai")

    def test_flag_on_appends_the_placeholder_line(self, monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
        draft = outreach._make_draft("DXP Enterprises", _SPECS)
        assert draft.endswith(outreach.QUOTE_LINK_LINE)
        assert "{quote_link}" in draft
        # The founder copy above the appended line is untouched.
        off = draft[: -len("\n\n" + outreach.QUOTE_LINK_LINE)]
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        assert off == outreach._make_draft("DXP Enterprises", _SPECS)

    def test_flag_on_composes_with_contact_ask(self, monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
        draft = outreach._make_draft("DXP", _SPECS, request_contact=True)
        assert outreach.CONTACT_NOMINATION_ASK in draft
        assert draft.endswith(outreach.QUOTE_LINK_LINE)


# ---------------------------------------------------------------------------
# Send-time substitution (rfq_send)
# ---------------------------------------------------------------------------

def _candidate() -> dict:
    return {"vendor_name": "DXP Enterprises", "source_url": "https://dxpe.com"}


def _draft_with_placeholder() -> str:
    return (
        "Subject: Quote Request — Gusher Pumps 84004-28\n\n"
        "Hello DXP Enterprises,\n\nPlease reply with unit price.\n\n"
        "Regards,\nArkim Procurement\nprocurement@arkim.ai\n\n"
        f"{outreach.QUOTE_LINK_LINE}"
    )


def _send(stores, draft, **kw):
    from utils import supplier_registry
    from utils.rfq_send import Approval, send_rfq
    supplier_registry.create_stub("DXP Enterprises", domain="dxpe.com")
    supplier_registry.upsert_contact("dxpe.com", {
        "contact_email": "sales@dxpe.com", "contact_method": "generic_inbox",
        "contact_status": "resolved"})
    kwargs = dict(run_id="run-t6", part_key="gusher pumps|8400428")
    kwargs.update(kw)
    return send_rfq(_candidate(), draft, Approval("tom"), **kwargs)


def _token_rows(tmp_path):
    path = tmp_path / "quote_tokens.sqlite"
    if not path.exists():
        return []  # flag-off never even creates the store
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM quote_tokens").fetchall()]
    conn.close()
    return rows


class TestSendTimeSubstitution:
    def test_flag_on_mints_and_substitutes(self, stores, monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
        monkeypatch.setenv("ARKIM_PUBLIC_BASE_URL", "https://app.arkim.ai")
        res = _send(stores, _draft_with_placeholder())
        assert res["status"] == "stubbed"  # send gate off — no delivery
        (tok,) = _token_rows(stores)
        assert tok["run_id"] == "run-t6"
        assert tok["supplier_domain"] == "dxpe.com"
        assert tok["part_key"] == "gusher pumps|8400428"
        assert tok["is_test"] == 0  # a live send flow mints a live token
        # The recorded body carries the real link, never the placeholder.
        from utils import supplier_registry
        (row,) = supplier_registry.get_sent_messages(run_id="run-t6")
        assert "{quote_link}" not in row["body"]
        assert "https://app.arkim.ai/quote/" in row["body"]
        # Hashed at rest: the raw token appears in the BODY (that's the link)
        # but never in the token store.
        link_token = row["body"].rsplit("/quote/", 1)[1].split()[0]
        assert link_token not in str(_token_rows(stores))
        assert qt.validate_token(link_token)["rfq_id"] == tok["rfq_id"]

    def test_token_scoped_to_this_send(self, stores, monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
        _send(stores, _draft_with_placeholder())
        _send(stores, _draft_with_placeholder(), run_id="run-other")
        rows = _token_rows(stores)
        assert len(rows) == 2
        assert {r["run_id"] for r in rows} == {"run-t6", "run-other"}
        assert rows[0]["rfq_id"] != rows[1]["rfq_id"]  # one token per RFQ send

    def test_flag_off_stale_placeholder_line_is_dropped_whole(self, stores):
        # A draft approved while the flag was on, sent after a kill-switch:
        # the supplier must never see the literal placeholder.
        res = _send(stores, _draft_with_placeholder())
        assert res["status"] == "stubbed"
        from utils import supplier_registry
        (row,) = supplier_registry.get_sent_messages(run_id="run-t6")
        assert "{quote_link}" not in row["body"]
        assert "Prefer a form?" not in row["body"]
        assert _token_rows(stores) == []  # nothing minted flag-off

    def test_flag_off_plain_draft_untouched(self, stores):
        plain = "Subject: Q\n\nHello,\n\nRegards,\nArkim Procurement"
        _send(stores, plain)
        from utils import supplier_registry
        (row,) = supplier_registry.get_sent_messages(run_id="run-t6")
        assert row["body"] == plain  # byte-identical flag-off parity

    def test_mint_failure_drops_the_line_never_the_send(self, stores,
                                                        monkeypatch):
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
        monkeypatch.setattr(qt, "mint_for_rfq", lambda **kw: None)
        res = _send(stores, _draft_with_placeholder())
        assert res["status"] == "stubbed"  # the send flow survived
        from utils import supplier_registry
        (row,) = supplier_registry.get_sent_messages(run_id="run-t6")
        assert "{quote_link}" not in row["body"]


# ---------------------------------------------------------------------------
# _quote_overlay — buyer-card fields (flag-inert: payload-driven)
# ---------------------------------------------------------------------------

class TestQuoteOverlayCardFields:
    _LEGACY_PAYLOAD_KEYS = {
        "evidenceState", "quoteConfirmed", "supplierConfirmed",
        "quoteUnverified", "quoteThreadId", "quoteCurrency", "price",
        "leadTime", "leadTimeSource",
    }

    def test_legacy_email_quote_overlay_is_byte_identical(self):
        import api_server
        overlay = api_server._quote_overlay({
            "thread_id": "t-1", "confidence": 0.9,
            "payload": {"unit_price": 189.0, "lead_time": "3 days",
                        "currency": "USD"},
        })
        # Exactly the pre-Night-11 key set — no new keys on email quotes.
        assert set(overlay) == self._LEGACY_PAYLOAD_KEYS

    def test_structured_quote_adds_card_provenance(self):
        import api_server
        from utils import quote_store
        rec = quote_store.as_confirmation_record({
            "id": "q-1", "supplier_domain": "dxpe.com",
            "vendor_name": "DXP Enterprises", "unit_price": 189.0,
            "currency": "USD", "lead_time": "3 days",
            "submitted_at": "2026-07-16T01:00:00+00:00",
            "submitted_via": "rfq_link", "pn_differs": False,
        })
        overlay = api_server._quote_overlay(rec)
        assert overlay["quoteConfirmedAt"] == "2026-07-16T01:00:00+00:00"
        assert overlay["quoteId"] == "q-1"
        assert overlay["price"] == 189.0
        assert "pnDiffers" not in overlay        # pn confirmed ⇒ no PN relabel
        assert "quotedPartNumber" not in overlay

    def test_pn_differs_quote_labels_the_quoted_pn(self):
        import api_server
        from utils import quote_store
        rec = quote_store.as_confirmation_record({
            "id": "q-2", "supplier_domain": "dxpe.com", "unit_price": 175.0,
            "currency": "USD", "pn_differs": True,
            "quoted_part_number": "84004-28SP",
            "submitted_at": "2026-07-16T01:00:00+00:00",
        })
        overlay = api_server._quote_overlay(rec)
        assert overlay["pnDiffers"] is True
        assert overlay["quotedPartNumber"] == "84004-28SP"
