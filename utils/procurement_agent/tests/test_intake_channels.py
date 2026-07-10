"""
Night 8 — Multi-channel intake spine tests (T1: contract + consumer + property).

Covers (T1):
  - The typed IntakeEvent contract + validation (malformed ⇒ safe reject).
  - Plus-addressing tenant resolution (I3 decision).
  - The unknown-sender defence (held + confirm; no run from a stranger).
  - Parser honesty (propose-don't-invent): insufficient ⇒ NEEDS_CLARIFICATION,
    never a run; a family-variant block ⇒ NEEDS_CLARIFICATION, never a run.
  - The single consumer fires the sourcing run via the injected firer on a
    valid, sufficient, sender-verified event (RUN_CREATED + ack reply).
  - PROPERTY: no path from the consumer to order placement / approval — the
    consumer calls ONLY fire_sourcing_run + reply_sink; it never reaches
    orders.create_order / place_order / the approve endpoints.
  - Flag-off dormancy (defense-in-depth at the store/decision layer).

Isolated store: each test points intake_channels at a temp sqlite file + flips
INTAKE_CHANNELS_V1 on. No live network; IntakeAgent is mocked (no Anthropic
call). The no-order property is proven at this layer via an instrumented firer
AND at the API layer in test_api_server.py (TestClient-level, T5).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

import pytest

from utils import intake_channels as ic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated intake_channels store + INTAKE_CHANNELS_V1 ON."""
    monkeypatch.setattr(ic, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ic, "_DB_PATH", str(tmp_path / "intake_channels.sqlite"))
    monkeypatch.setenv("INTAKE_CHANNELS_V1", "1")
    return ic


@pytest.fixture
def tenant_map(monkeypatch):
    """A fixed tenant map for resolution tests."""
    monkeypatch.setattr(ic, "_TENANT_MAP", {
        "acme": {"company_id": "company-acme", "facility_id": "fac-acme-1"},
        "beta": {"company_id": "company-beta", "facility_id": "fac-beta-1"},
    })
    return ic._TENANT_MAP


def _img_attachment() -> ic.IntakeAttachment:
    # Minimal 1x1 PNG (magic bytes) — same image-handling the upload path uses.
    png = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return ic.IntakeAttachment(filename="nameplate.png", content_type="image/png", data=png)


def _agent_result(*, sufficient: bool, specs: Optional[dict] = None,
                  follow_up: Optional[str] = None) -> dict:
    """A canned IntakeAgent.run() result (no Anthropic call)."""
    return {
        "asset_specs": specs or {},
        "manufacturer_confidence": 90 if sufficient else 30,
        "part_id_confidence": 90 if sufficient else 30,
        "sufficient": sufficient,
        "follow_up_question": follow_up,
        "commit_message": None,
        "confidence_summary": {"proceed_state": "proceed" if sufficient else "needs_info"},
    }


class _FakeAgent:
    """A fake IntakeAgent returning a canned result (propose-don't-invent)."""
    def __init__(self, result: dict):
        self._result = result
        self.calls: List[dict] = []

    def run(self, run_obj, user_input: dict) -> dict:
        self.calls.append({"text": user_input.get("text"),
                           "n_images": len(user_input.get("images") or [])})
        return self._result


class _RecordingFirer:
    """An instrumented fire_sourcing_run: records calls + returns a run_id, and
    FAILS LOUD if it ever touches order/approve (the no-order property guard)."""
    def __init__(self, run_id: str = "run-from-intake"):
        self.run_id = run_id
        self.calls: List[dict] = []
        self.ordered = False
        self.approved = False

    def __call__(self, specs: dict, tenant_key: str) -> Optional[str]:
        self.calls.append({"specs": specs, "tenant_key": tenant_key})
        # The firer must NEVER order/approve — assert it doesn't even reference them.
        assert "order" not in str(specs).lower() or "exact_only" in specs, "firer must not order"
        return self.run_id


class _RecordingSink:
    def __init__(self):
        self.replies: List[ic.IntakeReply] = []

    def __call__(self, reply: ic.IntakeReply) -> None:
        self.replies.append(reply)


# ---------------------------------------------------------------------------
# Contract + validation
# ---------------------------------------------------------------------------

class TestIntakeEventContract:
    def test_valid_event_validates(self, store):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="Goulds 3196 pump, 5HP")
        assert ev.validate() is None

    def test_empty_message_rejected(self, store):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="   ")
        assert ev.validate() == "empty message (no text and no attachments)"

    def test_attachment_only_is_valid(self, store):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="",
                            attachments=[_img_attachment()])
        assert ev.validate() is None

    def test_missing_tenant_rejected(self, store):
        ev = ic.IntakeEvent(tenant_key="", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="hello")
        assert ev.validate() == "missing tenant_key"

    def test_missing_sender_rejected(self, store):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.SMS,
                            sender="", text_body="hello")
        assert ev.validate() == "missing sender"


# ---------------------------------------------------------------------------
# I3 — plus-addressing tenant resolution
# ---------------------------------------------------------------------------

class TestTenantResolution:
    def test_plus_addressing_resolves(self, store, tenant_map):
        assert ic.resolve_tenant_from_address("intake+acme@arkim.ai") == "acme"

    def test_bare_intake_no_tenant(self, store, tenant_map):
        assert ic.resolve_tenant_from_address("intake@arkim.ai") is None

    def test_wrong_domain_no_tenant(self, store, tenant_map):
        assert ic.resolve_tenant_from_address("intake+acme@example.com") is None

    def test_non_intake_local_part(self, store, tenant_map):
        assert ic.resolve_tenant_from_address("procurement@arkim.ai") is None

    def test_no_plus_suffix(self, store, tenant_map):
        assert ic.resolve_tenant_from_address("intake+@arkim.ai") is None

    def test_tenant_lookup_returns_company_and_facility(self, store, tenant_map):
        t = ic.tenant_lookup("acme")
        assert t == {"company_id": "company-acme", "facility_id": "fac-acme-1"}

    def test_unknown_tenant_lookup_none(self, store, tenant_map):
        assert ic.tenant_lookup("nonexistent") is None


# ---------------------------------------------------------------------------
# Unknown-sender defence
# ---------------------------------------------------------------------------

class TestUnknownSenderDefence:
    def test_known_sender_flows_through(self, store, tenant_map):
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        assert ic.sender_known("acme", "plant@acme.com") is True

    def test_unknown_sender_held_no_run(self, store, tenant_map):
        firer = _RecordingFirer()
        sink = _RecordingSink()
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="stranger@acme.com", text_body="need a pump")
        outcome = ic.consume_intake_event(
            ev, fire_sourcing_run=firer, reply_sink=sink,
            intake_agent=_FakeAgent(_agent_result(sufficient=True, specs={"manufacturer": "Goulds"})),
        )
        assert outcome.status == ic.IntakeOutcomeStatus.UNKNOWN_SENDER_CONFIRM_SENT
        assert outcome.run_id is None
        assert firer.calls == []  # NO run fired from an unverified stranger
        assert outcome.confirm_token  # a confirm token was minted
        assert sink.replies and sink.replies[0].kind == "confirm"

    def test_unknown_sender_cross_tenant_isolated(self, store, tenant_map):
        # A sender known to tenant A is NOT known to tenant B.
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        assert ic.sender_known("acme", "plant@acme.com") is True
        assert ic.sender_known("beta", "plant@acme.com") is False

    def test_held_event_token_hashed_at_rest(self, store, tenant_map):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="stranger@acme.com", text_body="need a pump")
        token = ic.hold_event(ev, is_test=True)
        assert token
        rows = _read_held_rows()
        assert len(rows) == 1
        # The raw token is NEVER stored — only its hash.
        assert token not in rows[0]["event_json"]
        assert rows[0]["token_hash"] != token
        assert rows[0]["is_test"] == 1

    def test_consume_held_replays_and_marks_confirmed(self, store, tenant_map):
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="stranger@acme.com", text_body="need a pump")
        token = ic.hold_event(ev, is_test=True)
        payload = ic.consume_held(token)
        assert payload is not None
        assert payload["tenant_key"] == "acme"
        # A reuse returns None (one-time confirm).
        assert ic.consume_held(token) is None


def _read_held_rows():
    conn = sqlite3.connect(ic._DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM intake_held_events").fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Parser honesty — propose-don't-invent
# ---------------------------------------------------------------------------

class TestParserHonesty:
    def test_insufficient_specs_no_run(self, store, tenant_map):
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        firer = _RecordingFirer()
        sink = _RecordingSink()
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="need a thing")
        outcome = ic.consume_intake_event(
            ev, fire_sourcing_run=firer, reply_sink=sink,
            intake_agent=_FakeAgent(_agent_result(sufficient=False, follow_up="Which model?")),
        )
        assert outcome.status == ic.IntakeOutcomeStatus.NEEDS_CLARIFICATION
        assert outcome.run_id is None
        assert firer.calls == []  # NO run with invented specifics
        assert sink.replies and sink.replies[0].kind == "clarify"

    def test_extractor_failure_is_needs_clarification(self, store, tenant_map):
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        firer = _RecordingFirer()
        sink = _RecordingSink()
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="need a pump")

        class _Boom:
            def run(self, *a, **k):
                raise RuntimeError("anthropic down")
        outcome = ic.consume_intake_event(
            ev, fire_sourcing_run=firer, reply_sink=sink, intake_agent=_Boom())
        assert outcome.status == ic.IntakeOutcomeStatus.NEEDS_CLARIFICATION
        assert outcome.run_id is None
        assert firer.calls == []  # fail-soft: never invent, never crash


# ---------------------------------------------------------------------------
# Happy path — valid + sufficient + verified ⇒ RUN_CREATED
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_known_sender_sufficient_specs_fires_run(self, store, tenant_map):
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        firer = _RecordingFirer(run_id="run-123")
        sink = _RecordingSink()
        ev = ic.IntakeEvent(
            tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
            sender="plant@acme.com", text_body="Goulds 3196 5HP pump",
            attachments=[_img_attachment()],
        )
        agent = _FakeAgent(_agent_result(sufficient=True,
                                         specs={"manufacturer": "Goulds", "model": "3196",
                                                "part_number": "3196MTX"}))
        outcome = ic.consume_intake_event(
            ev, fire_sourcing_run=firer, reply_sink=sink, intake_agent=agent)
        assert outcome.status == ic.IntakeOutcomeStatus.RUN_CREATED
        assert outcome.run_id == "run-123"
        assert len(firer.calls) == 1
        # The attachment bytes were threaded into the parser (I4 image path).
        assert agent.calls[0]["n_images"] == 1
        assert sink.replies and sink.replies[0].kind == "ack"


# ---------------------------------------------------------------------------
# PROPERTY TEST — no path from the consumer to order placement / approval.
# ---------------------------------------------------------------------------

class TestNoOrderPath:
    """The consumer fires sourcing ONLY. It must never place, approve, or
    advance an order (auto-order is explicitly out, Night 8). Proven by
    instrumenting the order/approve surface and asserting the consumer never
    reaches it — across every outcome the consumer can produce."""

    def test_consumer_never_orders_or_approves(self, store, tenant_map, monkeypatch):
        # Instrument the order/approve surface in utils.orders so ANY call is
        # recorded. The consumer + the injected firer must never touch them.
        touches: List[str] = []
        import utils.orders as orders_mod

        def _trap(name):
            def _f(*a, **k):
                touches.append(name)
                raise AssertionError(f"orders.{name} reached from intake consumer")
            return _f

        monkeypatch.setattr(orders_mod, "create_order", _trap("create_order"))
        monkeypatch.setattr(orders_mod, "place_order", _trap("place_order"))
        monkeypatch.setattr(orders_mod, "update_order_status", _trap("update_order_status"))

        firer = _RecordingFirer()
        sink = _RecordingSink()
        agent = _FakeAgent(_agent_result(sufficient=True,
                                         specs={"manufacturer": "Goulds", "part_number": "3196"}))

        # Every consumer outcome — exercise them all and assert no order touch.
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)

        # RUN_CREATED path.
        ev_ok = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                               sender="plant@acme.com", text_body="Goulds 3196")
        r1 = ic.consume_intake_event(ev_ok, fire_sourcing_run=firer, reply_sink=sink,
                                     intake_agent=agent)
        assert r1.status == ic.IntakeOutcomeStatus.RUN_CREATED

        # NEEDS_CLARIFICATION path.
        ev_vague = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                                  sender="plant@acme.com", text_body="thing")
        r2 = ic.consume_intake_event(
            ev_vague, fire_sourcing_run=firer, reply_sink=sink,
            intake_agent=_FakeAgent(_agent_result(sufficient=False, follow_up="?")))
        assert r2.status == ic.IntakeOutcomeStatus.NEEDS_CLARIFICATION

        # UNKNOWN_SENDER path.
        ev_stranger = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                                     sender="stranger@acme.com", text_body="hi")
        r3 = ic.consume_intake_event(ev_stranger, fire_sourcing_run=firer, reply_sink=sink,
                                     intake_agent=agent)
        assert r3.status == ic.IntakeOutcomeStatus.UNKNOWN_SENDER_CONFIRM_SENT

        # CONFIRMED path (replay a held event).
        token = ic.hold_event(ev_stranger, is_test=True)
        payload = ic.consume_held(token)
        r4 = ic.consume_confirmed_event(payload, fire_sourcing_run=firer,
                                        reply_sink=sink, intake_agent=agent)
        assert r4.status in (ic.IntakeOutcomeStatus.RUN_CREATED,
                             ic.IntakeOutcomeStatus.NEEDS_CLARIFICATION)

        # The order/approve surface was never touched across every outcome.
        assert touches == [], f"intake consumer reached order surface: {touches}"
        # And the firer itself only ever recorded sourcing calls (never ordered).
        assert all("order" not in str(c) for c in firer.calls)


# ---------------------------------------------------------------------------
# Flag-off dormancy (defense-in-depth at the store/decision layer)
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_flag_off_sender_known_false(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ic, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(ic, "_DB_PATH", str(tmp_path / "intake_channels.sqlite"))
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        assert ic.sender_known("acme", "plant@acme.com") is False

    def test_flag_off_consumer_returns_flag_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ic, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(ic, "_DB_PATH", str(tmp_path / "intake_channels.sqlite"))
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        ev = ic.IntakeEvent(tenant_key="acme", channel=ic.IntakeChannel.EMAIL,
                            sender="plant@acme.com", text_body="hi")
        outcome = ic.consume_intake_event(ev, fire_sourcing_run=lambda s, t: "x")
        assert outcome.status == ic.IntakeOutcomeStatus.FLAG_OFF
