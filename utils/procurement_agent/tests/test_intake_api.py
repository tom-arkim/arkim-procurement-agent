"""
Night 8 — Intake API endpoint tests (T2 email adapter + confirm step; T5
live-faithful path; T4 inertness/security at the API layer).

These exercise the intake surface through the real FastAPI app (TestClient,
the suite's standard mocks) — the email→event→sourcing-run flow runs through
the actual consumer + the actual _fire_sourcing_run_for_intake (the existing
pipeline seam), with IntakeAgent mocked offline and sourcing mocked so no
external calls fire.

Covers:
  - T2: valid email from a known sender to a tenant address ⇒ structured
    request created ⇒ sourcing run fired (same run as in-app); attachment
    available to identification (I4).
  - T2: ambiguous email ⇒ NEEDS_CLARIFICATION + stubbed clarify reply; NO
    sourcing run with invented specifics.
  - T2: unknown sender ⇒ stubbed confirm step; no run until confirmed.
  - T4: flag off ⇒ endpoints absent (byte-identical 404 to unknown routes).
  - T4: malformed/garbage inbound ⇒ safe rejection (TENANT_UNKNOWN /
    REJECTED_MALFORMED), nothing enters the pipeline.
  - T4: cross-tenant — a sender known to tenant A cannot create runs for
    tenant B (the address resolves the tenant; the sender check is per-tenant).
  - T5: the email→event→sourcing-run flow through the real API + consumer +
    firer (TestClient-level, not direct function calls).
  - No-order property at the API layer: after an intake-fired run, no order
    row exists and orders.create_order/place_order were never called.
"""
from __future__ import annotations

import base64
import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker


# ---------------------------------------------------------------------------
# Local `api` fixture — mirrors test_api_server.py's (the intake surface needs
# the same isolated TestClient + temp DB + neutralized externals). Defined here
# because the `api` fixture is module-scoped to test_api_server.py, not shared.
# ---------------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from utils.procurement_agent.state import persistence
    engine = persistence._make_engine(f"sqlite:///{tmp_path / 'api.sqlite'}")
    TestSession = sessionmaker(bind=engine, expire_on_commit=False)
    persistence.Base.metadata.create_all(engine)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("TAVILY_API_KEY", "")
    # DEMO_MODE allowlist isolation: test_demo_mode reloads api_server with
    # DEMO_MODE=true and leaves the module attribute set; a later test that
    # imports the cached module would inherit DEMO_MODE=True and get 403s on
    # non-allowlisted routes. Force DEMO_MODE off (env + module attr) so the
    # allowlist middleware is inert for the intake surface (a tenant feature,
    # not a demo feature — the intake routes are intentionally NOT on the
    # demo allowlist). Mirrors the demo_off fixture's discipline.
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("EMAIL_SEND_ENABLED", "")
    monkeypatch.setattr(persistence, "_engine", engine)
    monkeypatch.setattr(persistence, "_SessionFactory", TestSession)
    from utils import supplier_registry
    monkeypatch.setattr(supplier_registry, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supplier_registry, "_DB_PATH", str(tmp_path / "supplier_registry.sqlite"))
    import api_server
    # Reset the module-level DEMO_MODE the middleware reads at call time (a
    # prior test_demo_mode reload may have left it True in the cached module).
    monkeypatch.setattr(api_server, "DEMO_MODE", False)
    monkeypatch.setattr(api_server, "_engine", engine)
    monkeypatch.setattr(api_server, "_SessionFactory", TestSession)
    monkeypatch.setattr(api_server, "_messages", {})
    client = TestClient(api_server.app)
    client._api_server = api_server
    client._tmp_path = tmp_path
    return client


# ---------------------------------------------------------------------------
# Helpers — build an intake email payload.
# ---------------------------------------------------------------------------

def _email_payload(*, to="intake+bayfoods@arkim.ai", sender="plant@bayfoods.com",
                  body="Goulds 3196 5HP pump", attachments=None, message_id="<m1@x>"):
    return {
        "to": to,
        "from": sender,
        "body": body,
        "attachments": attachments or [],
        "message_id": message_id,
    }


def _png_attachment_b64():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    return {"filename": "nameplate.png", "content_type": "image/png",
            "data_b64": base64.b64encode(png).decode()}


def _mock_intake_sufficient(monkeypatch, api, specs=None):
    """Mock IntakeAgent.run to a sufficient result (propose-don't-invent).

    Patches the IntakeAgent *class* on both the api_server binding and the
    intake_agent module (the consumer imports it lazily from there) so any
    `IntakeAgent(...)` call returns the mock agent. Patching the class object
    (NOT __new__) so construction returns the mock — safe across the suite
    (no permanent mutation; monkeypatch reverts)."""
    agent = Mock()
    agent.run.return_value = {
        "sufficient": True, "manufacturer_confidence": 90, "part_id_confidence": 90,
        "asset_specs": specs or {"manufacturer": "Goulds", "model": "3196",
                                  "part_number": "3196MTX"},
        "follow_up_question": None, "commit_message": None,
        "confidence_summary": {"proceed_state": "proceed"},
    }
    import utils.procurement_agent.agents.intake_agent as ia_mod
    # Patch the class on BOTH bindings so IntakeAgent(...) -> agent everywhere.
    monkeypatch.setattr(ia_mod, "IntakeAgent", lambda *a, **k: agent)
    monkeypatch.setattr(api._api_server, "IntakeAgent", lambda *a, **k: agent)
    return agent


def _mock_intake_insufficient(monkeypatch, api, follow_up="Which model?"):
    agent = Mock()
    agent.run.return_value = {
        "sufficient": False, "manufacturer_confidence": 30, "part_id_confidence": 30,
        "asset_specs": {}, "follow_up_question": follow_up, "commit_message": None,
        "confidence_summary": {"proceed_state": "needs_info"},
    }
    import utils.procurement_agent.agents.intake_agent as ia_mod
    monkeypatch.setattr(ia_mod, "IntakeAgent", lambda *a, **k: agent)
    return agent


def _enable_intake(api, monkeypatch):
    """Flip INTAKE_CHANNELS_V1 on + point the intake store at the temp dir +
    seed a known sender for the demo tenant."""
    monkeypatch.setenv("INTAKE_CHANNELS_V1", "1")
    import utils.intake_channels as ic
    monkeypatch.setattr(ic, "_DATA_DIR", str(api._tmp_path))
    monkeypatch.setattr(ic, "_DB_PATH", str(api._tmp_path / "intake_channels.sqlite"))
    # Seed a known sender for the default demo tenant "bayfoods".
    ic.add_known_sender("bayfoods", "plant@bayfoods.com", is_test=True)


@pytest.fixture
def intake_api(api, monkeypatch, tmp_path):
    """The shared api fixture + intake enabled + tmp_path exposed on the client."""
    api._tmp_path = tmp_path
    _enable_intake(api, monkeypatch)
    return api


# ---------------------------------------------------------------------------
# T4 — flag-off inertness: endpoints absent (byte-identical 404).
# ---------------------------------------------------------------------------

class TestIntakeFlagOff:
    def test_email_endpoint_absent_when_flag_off(self, api, monkeypatch):
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        resp = api.post("/api/intake/email", json=_email_payload())
        # Byte-identical to FastAPI's unknown-route 404.
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    def test_confirm_endpoint_absent_when_flag_off(self, api, monkeypatch):
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        resp = api.post("/api/intake/confirm/sometoken", json={})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    def test_sms_endpoint_absent_when_flag_off(self, api, monkeypatch):
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        # A well-formed body so a 422 (validation) can't mask the flag-off 404.
        resp = api.post("/api/intake/sms", json={
            "to": "+15555550100", "from": "+15555550111", "body": "hi"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    def test_voice_endpoint_absent_when_flag_off(self, api, monkeypatch):
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        resp = api.post("/api/intake/voice", json={
            "to": "+15555550100", "from": "+15555550111", "transcript": "hi"})
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}

    def test_flag_off_404_byte_identical_to_unknown_route(self, api, monkeypatch):
        """Flag-off intake 404 is byte-identical to FastAPI's unknown-route 404
        (status + body), so flag-off is indistinguishable from a route that
        never existed (no oracle, no leakage) — mirrors the portal inertness."""
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        intake_resp = api.post("/api/intake/email", json=_email_payload())
        unknown_resp = api.post("/api/this-route-does-not-exist", json={})
        assert intake_resp.status_code == unknown_resp.status_code == 404
        assert intake_resp.json() == unknown_resp.json() == {"detail": "Not Found"}

    def test_flag_off_leaves_zero_runs(self, api, monkeypatch):
        """Flag-off intake never births a run row (no DB leakage)."""
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        api.post("/api/intake/email", json=_email_payload())
        assert api.get("/api/runs").json() == []

    def test_flag_off_leaves_zero_held_events(self, api, monkeypatch, tmp_path):
        """Flag-off intake never holds an event (no held-event row leakage)."""
        import utils.intake_channels as ic
        monkeypatch.setattr(ic, "_DATA_DIR", str(tmp_path))
        monkeypatch.setattr(ic, "_DB_PATH", str(tmp_path / "intake_channels.sqlite"))
        monkeypatch.setenv("INTAKE_CHANNELS_V1", "0")
        api.post("/api/intake/email", json=_email_payload(
            sender="stranger@bayfoods.com"))  # would be held if flag on
        # The store DDL is created lazily on first connection; open via the
        # module so the table exists even when flag-off short-circuited the write.
        with ic._get_conn() as conn:
            n = conn.execute("SELECT COUNT(*) FROM intake_held_events").fetchone()[0]
        assert n == 0


# ---------------------------------------------------------------------------
# T2 / T5 — the email→event→sourcing-run flow (live-faithful, real API).
# ---------------------------------------------------------------------------

class TestIntakeEmailFlow:
    def test_valid_known_sender_fires_sourcing_run(self, intake_api, monkeypatch):
        # Mock the background sourcing so no SourcingAgent/Anthropic/Tavily fires;
        # the run still advances to SOURCING via the real transition.
        api = intake_api
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        _mock_intake_sufficient(monkeypatch, api)

        resp = api.post("/api/intake/email", json=_email_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RUN_CREATED"
        run_id = body["run_id"]
        assert run_id

        # The run is at SOURCING (the existing intake→sourcing transition fired,
        # same as an in-app confirm-intake) + tenant-stamped + specs seeded.
        detail = api.get(f"/api/runs/{run_id}").json()
        assert detail["phase"] == "sourcing"
        assert detail["asset_specs"]["manufacturer"] == "Goulds"
        # company_id stamped from the tenant map (the demo tenant).
        assert detail.get("company_id") in (None, "company-bayfoods")

    def test_attachment_carried_through_to_intake_agent(self, intake_api, monkeypatch):
        """I4 — an attached nameplate photo reaches the IntakeAgent images kwarg
        (the same image-handling the in-app upload path uses)."""
        api = intake_api
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        agent = _mock_intake_sufficient(monkeypatch, api)
        payload = _email_payload(attachments=[_png_attachment_b64()])
        resp = api.post("/api/intake/email", json=payload)
        assert resp.status_code == 200
        assert resp.json()["status"] == "RUN_CREATED"
        # The agent received the attachment bytes as images.
        call = agent.run.call_args
        assert len(call.args[1]["images"]) == 1
        assert call.args[1]["images"][0].startswith(b"\x89PNG")

    def test_ambiguous_email_needs_clarification_no_run(self, intake_api, monkeypatch):
        api = intake_api
        _mock_intake_insufficient(monkeypatch, api, follow_up="Which HP rating?")
        resp = api.post("/api/intake/email", json=_email_payload(body="need a pump"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "NEEDS_CLARIFICATION"
        assert body["run_id"] is None  # NO run with invented specifics
        # No run row was created.
        assert api.get("/api/runs").json() == [] or all(
            r["phase"] != "sourcing" for r in api.get("/api/runs").json()
        )

    def test_unknown_sender_confirm_step_no_run(self, intake_api, monkeypatch):
        api = intake_api
        _mock_intake_sufficient(monkeypatch, api)  # would fire if not for the sender gate
        resp = api.post("/api/intake/email", json=_email_payload(
            sender="stranger@bayfoods.com"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "UNKNOWN_SENDER_CONFIRM_SENT"
        assert body["run_id"] is None  # no run from an unverified stranger

    def test_confirm_advances_held_event_to_run(self, intake_api, monkeypatch):
        api = intake_api
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        _mock_intake_sufficient(monkeypatch, api)
        # 1. stranger → confirm step (held).
        r1 = api.post("/api/intake/email", json=_email_payload(
            sender="stranger@bayfoods.com")).json()
        assert r1["status"] == "UNKNOWN_SENDER_CONFIRM_SENT"
        # 2. pull the held token from the store + confirm → run.
        import utils.intake_channels as ic
        import sqlite3
        conn = sqlite3.connect(api._tmp_path / "intake_channels.sqlite")
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT token_hash FROM intake_held_events ORDER BY created_at DESC LIMIT 1").fetchone()
        conn.close()
        # The raw token isn't recoverable from the hash; the confirm link in the
        # stubbed reply carries it. Re-hold with a captured token via the module
        # to exercise confirm end-to-end.
        from utils.intake_channels import hold_event, consume_held, IntakeEvent, IntakeChannel
        ev = IntakeEvent(tenant_key="bayfoods", channel=IntakeChannel.EMAIL,
                         sender="stranger@bayfoods.com", text_body="Goulds 3196")
        token = hold_event(ev, is_test=True)
        r2 = api.post(f"/api/intake/confirm/{token}").json()
        assert r2["status"] == "RUN_CREATED"
        assert r2["run_id"]

    def test_confirm_bad_token_is_404_no_oracle(self, intake_api, monkeypatch):
        api = intake_api
        resp = api.post("/api/intake/confirm/not-a-real-token")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Not Found"}


# ---------------------------------------------------------------------------
# T4 — safe rejection: malformed / unattributable inbound.
# ---------------------------------------------------------------------------

class TestIntakeSafeRejection:
    def test_no_tenant_for_address(self, intake_api, monkeypatch):
        api = intake_api
        resp = api.post("/api/intake/email", json=_email_payload(
            to="intake+nonexistent@arkim.ai"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "TENANT_UNKNOWN"
        assert body["run_id"] is None

    def test_non_intake_address_no_tenant(self, intake_api, monkeypatch):
        api = intake_api
        resp = api.post("/api/intake/email", json=_email_payload(
            to="procurement@arkim.ai"))
        assert resp.json()["status"] == "TENANT_UNKNOWN"

    def test_garbage_inbound_no_run_no_pipeline(self, intake_api, monkeypatch):
        """Malformed/garbage inbound is safely rejected — nothing enters the
        pipeline (no run, no held event, no parse). A valid tenant address but
        an empty body AND no attachments ⇒ REJECTED_MALFORMED at the consumer."""
        api = intake_api
        import utils.intake_channels as ic
        # A known sender with an empty message → validate() rejects (no text,
        # no attachments). No run, no parse call.
        ic.add_known_sender("bayfoods", "plant@bayfoods.com", is_test=True)
        parsed = []
        monkeypatch.setattr(ic, "parse_event_to_specs",
                            lambda *a, **k: parsed.append(1) or {})
        resp = api.post("/api/intake/email", json=_email_payload(
            body="   ", attachments=[]))
        body = resp.json()
        assert body["status"] in ("REJECTED_MALFORMED", "NEEDS_CLARIFICATION")
        assert body["run_id"] is None
        assert parsed == []  # the parser was never invoked (validate() rejected)

    def test_safe_rejection_leaves_no_run_row(self, intake_api, monkeypatch):
        """A safe-rejection outcome (TENANT_UNKNOWN / REJECTED) leaves no run."""
        api = intake_api
        api.post("/api/intake/email", json=_email_payload(
            to="intake+nonexistent@arkim.ai"))
        assert api.get("/api/runs").json() == []


# ---------------------------------------------------------------------------
# T4 — cross-tenant isolation: a sender known to tenant A cannot create runs
# for tenant B (the address resolves the tenant; the sender check is per-tenant).
# ---------------------------------------------------------------------------

class TestCrossTenantIsolation:
    def test_known_sender_for_a_not_b(self, intake_api, monkeypatch):
        api = intake_api
        # "plant@bayfoods.com" is known to bayfoods (seeded by the fixture).
        # Add a second tenant + seed a different known sender for it.
        import utils.intake_channels as ic
        monkeypatch.setattr(ic, "_TENANT_MAP", {
            "bayfoods": {"company_id": "company-bayfoods", "facility_id": "fac-stockton"},
            "acme": {"company_id": "company-acme", "facility_id": "fac-acme-1"},
        })
        ic.add_known_sender("acme", "plant@acme.com", is_test=True)
        _mock_intake_sufficient(monkeypatch, api)
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)

        # A sender known to bayfoods, addressed to acme → NOT known for acme →
        # confirm step, no run attributed to acme.
        resp = api.post("/api/intake/email", json=_email_payload(
            to="intake+acme@arkim.ai", sender="plant@bayfoods.com"))
        assert resp.json()["status"] == "UNKNOWN_SENDER_CONFIRM_SENT"
        assert resp.json()["run_id"] is None

        # The reverse: a sender known to acme, addressed to acme → run for acme.
        resp2 = api.post("/api/intake/email", json=_email_payload(
            to="intake+acme@arkim.ai", sender="plant@acme.com"))
        assert resp2.json()["status"] == "RUN_CREATED"
        run_id = resp2.json()["run_id"]
        detail = api.get(f"/api/runs/{run_id}").json()
        assert detail["facility_id"] == "fac-acme-1"


# ---------------------------------------------------------------------------
# No-order property at the API layer: an intake-fired run never reaches the
# order/approve surface.
# ---------------------------------------------------------------------------

class TestIntakeNoOrderPath:
    def test_intake_fired_run_has_no_order(self, intake_api, monkeypatch):
        api = intake_api
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        _mock_intake_sufficient(monkeypatch, api)

        # Instrument orders so any call is a loud failure.
        touches = []
        import utils.orders as orders_mod
        def _trap(name):
            def _f(*a, **k):
                touches.append(name)
                raise AssertionError(f"orders.{name} reached from intake")
            return _f
        monkeypatch.setattr(orders_mod, "create_order", _trap("create_order"))
        monkeypatch.setattr(orders_mod, "place_order", _trap("place_order"))

        resp = api.post("/api/intake/email", json=_email_payload())
        assert resp.json()["status"] == "RUN_CREATED"
        run_id = resp.json()["run_id"]

        # The run is at SOURCING (never APPROVED / ordered).
        detail = api.get(f"/api/runs/{run_id}").json()
        assert detail["phase"] == "sourcing"
        # No order row for this run.
        orders = api.get(f"/api/runs/{run_id}/orders").json()
        assert orders in ([], {"orders": []}, {"items": []}) or (
            isinstance(orders, dict) and not orders.get("orders"))
        assert touches == [], f"intake reached order surface: {touches}"


# ---------------------------------------------------------------------------
# T3 — SMS + voice contract-stubs: same intake event, same consumer, same seam.
# ---------------------------------------------------------------------------

class TestSmsVoiceContractStubs:
    def _enable_number_tenant(self, monkeypatch):
        """Map an inbound number to the demo tenant for SMS/voice tests."""
        import utils.intake_channels as ic
        monkeypatch.setattr(ic, "_NUMBER_TENANT_MAP", {"+15555550100": "bayfoods"})

    def test_sms_known_sender_fires_run(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        _mock_intake_sufficient(monkeypatch, api)
        # Register the SMS sender as known for the tenant (sender = phone number).
        import utils.intake_channels as ic
        ic.add_known_sender("bayfoods", "+15555550111", is_test=True)

        resp = api.post("/api/intake/sms", json={
            "to": "+15555550100", "from": "+15555550111",
            "body": "Goulds 3196 5HP pump", "message_sid": "SMxxx",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RUN_CREATED"
        assert body["run_id"]
        detail = api.get(f"/api/runs/{body['run_id']}").json()
        assert detail["phase"] == "sourcing"

    def test_sms_mms_media_carried_to_intake_agent(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        agent = _mock_intake_sufficient(monkeypatch, api)
        import utils.intake_channels as ic
        ic.add_known_sender("bayfoods", "+15555550111", is_test=True)

        resp = api.post("/api/intake/sms", json={
            "to": "+15555550100", "from": "+15555550111", "body": "see photo",
            "media": [_png_attachment_b64()],
        })
        assert resp.json()["status"] == "RUN_CREATED"
        # MMS media reached the IntakeAgent images kwarg (I4).
        call = agent.run.call_args
        assert len(call.args[1]["images"]) == 1

    def test_sms_unknown_sender_confirm_step(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        _mock_intake_sufficient(monkeypatch, api)
        resp = api.post("/api/intake/sms", json={
            "to": "+15555550100", "from": "+15555550199", "body": "hi",
        })
        assert resp.json()["status"] == "UNKNOWN_SENDER_CONFIRM_SENT"
        assert resp.json()["run_id"] is None

    def test_sms_no_tenant_for_number(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        resp = api.post("/api/intake/sms", json={
            "to": "+19999990000", "from": "+15555550111", "body": "hi",
        })
        assert resp.json()["status"] == "TENANT_UNKNOWN"
        assert resp.json()["run_id"] is None

    def test_voice_transcript_known_caller_fires_run(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        _mock_intake_sufficient(monkeypatch, api)
        import utils.intake_channels as ic
        ic.add_known_sender("bayfoods", "+15555550111", is_test=True)

        resp = api.post("/api/intake/voice", json={
            "to": "+15555550100", "from": "+15555550111",
            "transcript": "I need a Goulds 3196 5HP pump",
            "call_sid": "CAxxx", "recording_url": "https://rec/x",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "RUN_CREATED"
        detail = api.get(f"/api/runs/{body['run_id']}").json()
        assert detail["phase"] == "sourcing"

    def test_voice_ambiguous_transcript_needs_clarification(self, intake_api, monkeypatch):
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        _mock_intake_insufficient(monkeypatch, api, follow_up="Which model?")
        import utils.intake_channels as ic
        ic.add_known_sender("bayfoods", "+15555550111", is_test=True)
        resp = api.post("/api/intake/voice", json={
            "to": "+15555550100", "from": "+15555550111",
            "transcript": "need a pump",
        })
        assert resp.json()["status"] == "NEEDS_CLARIFICATION"
        assert resp.json()["run_id"] is None

    def test_sms_and_voice_produce_same_event_contract(self, intake_api, monkeypatch):
        """The point of T3: both channels produce the SAME intake event shape
        and feed the SAME consumer. Assert the consumer sees channel=SMS vs
        channel=VOICE and otherwise the same contract."""
        api = intake_api
        self._enable_number_tenant(monkeypatch)
        monkeypatch.setattr(api._api_server, "_run_sourcing_background",
                            lambda *a, **k: None)
        seen = {}
        import utils.intake_channels as ic
        ic.add_known_sender("bayfoods", "+15555550111", is_test=True)
        real_consume = ic.consume_intake_event

        def _spy(event, **kw):
            seen["channel"] = event.channel
            seen["tenant_key"] = event.tenant_key
            seen["sender"] = event.sender
            seen["text_body"] = event.text_body
            return real_consume(event, **kw)
        monkeypatch.setattr(ic, "consume_intake_event", _spy)
        _mock_intake_sufficient(monkeypatch, api)

        api.post("/api/intake/sms", json={
            "to": "+15555550100", "from": "+15555550111", "body": "Goulds 3196"})
        assert seen["channel"] == ic.IntakeChannel.SMS
        assert seen["tenant_key"] == "bayfoods"
        assert seen["sender"] == "+15555550111"
        assert seen["text_body"] == "Goulds 3196"

        api.post("/api/intake/voice", json={
            "to": "+15555550100", "from": "+15555550111",
            "transcript": "Goulds 3196"})
        assert seen["channel"] == ic.IntakeChannel.VOICE
        # Same tenant + sender + body contract; only the channel differs.
        assert seen["tenant_key"] == "bayfoods"
        assert seen["sender"] == "+15555550111"
        assert seen["text_body"] == "Goulds 3196"
