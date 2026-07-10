"""
utils/intake_channels.py — the channel-agnostic intake spine (Night 8).

Requests are born in email, texts, and phone calls — not in an app. This module
is the ONE normalized intake event every channel adapter produces, plus the
single consumer that feeds a valid event into the EXISTING intake pipeline seam
(``api_server.confirm_intake`` → ``_run_sourcing_background``) and fires the
sourcing run exactly as an in-app request does. A transport, never a parallel
pipeline, never an auto-purchase trigger.

Built to the house standard as a standalone module: clean, typed, injector-DI,
fail-soft, tested (CLAUDE.md §5 / §9). The module owns the typed contract + the
pure decision logic + a small standalone provenance store; the run-firing
(DB row + background sourcing transition) is an injected callable so the
consumer is fully testable without the FastAPI app, and so a property test can
prove there is NO path from the consumer to order placement / approval.

Settled design (see NIGHT8_EMAIL_INTAKE_BRIEF.md):
  1. One normalized ``IntakeEvent`` (tenant, channel, sender + verification,
     text body, photo attachments, channel metadata, received_at).
  2. Per-tenant addressing — plus-addressing ``intake+<tenant-key>@arkim.ai``
     (decision I3, documented in the morning report). Mirrors the claim-token
     tenant-keying pattern: identity is encoded in the address, no separate
     mapping table to keep in sync, one credential set, scales without
     per-tenant provisioning. Gmail (the wired provider) supports RFC 5233
     subaddressing natively. A small fixture-overridable tenant map resolves
     the plus-local-part to a (company_id, default facility_id).
  3. Unknown-sender defence — a sender not recognized for the tenant is held
     + sent a stubbed "confirm this came from your plant" reply; no run is
     created from an unverified stranger. Known senders flow straight through.
  4. Parser honesty — the parser PROPOSES a structured request via the EXISTING
     ``IntakeAgent`` (propose-don't-invent). Ambiguous / underdetermined
     messages land as NEEDS_CLARIFICATION with a stubbed clarifying reply —
     never a confidently-wrong request entering sourcing. The existing intake
     clarification logic is fed AS IT IS (not fixed — Night 7's territory).
  5. Fires the sourcing run through the existing seam (injected firer).
  6. Acknowledgement reply — stubbed under the send double-gate.
  7. AUTO-ORDER IS OUT — nothing here places, approves, or advances an order.

Flag gating (guardrail 3): ``INTAKE_CHANNELS_V1`` is the route kill switch,
read LIVE (``_intake_enabled``, honors monkeypatched os.environ). Flag off ⇒
no intake endpoints exist (byte-identical 404, raised by the route layer) and
the store/decision functions no-op / return safe empty results so flag-off is
byte-identical to pre-Night-8 in tests that flip the flag off. This module's
flag is defense-in-depth; the ROUTE gate in api_server is load-bearing.

Standalone store (``data/intake_channels.sqlite``, separate from
supplier_registry's non-WAL sqlite — mirrors claim_tokens):
  - ``intake_known_senders``  (tenant_key, sender_email, is_test) — the
    recognized-sender registry the unknown-sender defence reads.
  - ``intake_held_events``    (token_hash, event_json, tenant_key, sender,
    is_test, created_at, confirmed_at) — the held unknown-sender events a
    confirm step advances. Token hashed at rest (mirrors claim_tokens); the
    raw token exists only in the stubbed confirm reply.

Any DB rows created here (and by tests) carry ``is_test = 1`` provenance.
Fail-soft: every store op degrades (returns None / [] / False) on error or
flag-off — never raises into the request path (CLAUDE.md §9 external-provider
discipline applies to its own I/O too).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Feature flag — mirrors SUPPLIER_PORTAL_V2 / SCORING_V2 strict truthy parse.
# Default OFF. Read live so a monkeypatched os.environ is honored (tests).
# Defense-in-depth: the ROUTE gates on this in api_server; the store/decision
# functions also check it so flag-off is byte-identical to pre-Night-8.
# ---------------------------------------------------------------------------
def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _intake_enabled() -> bool:
    """Live check for the intake surface gate (honors monkeypatched os.environ)."""
    return _env_truthy(os.environ.get("INTAKE_CHANNELS_V1"))


# The inbound address the intake adapter targets. Plus-addressing resolves the
# tenant: ``intake+<tenant-key>@<INTAKE_DOMAIN>``. The domain is configurable;
# the local prefix "intake" is fixed so the adapter can distinguish intake mail
# from RFQ-reply mail in the SAME inbox (the RFQ reply path reads
# procurement@arkim.ai with NO recipient filter — see inbox_reader.fetch_replies;
# intake mail carries the ``To: intake+...@`` signal the adapter keys on).
_INTAKE_LOCAL_PREFIX = "intake"
INTAKE_DOMAIN: str = os.environ.get("INTAKE_DOMAIN", "arkim.ai")


# ---------------------------------------------------------------------------
# Typed contract — the one normalized intake event every adapter produces.
# ---------------------------------------------------------------------------

class IntakeChannel(str, Enum):
    EMAIL = "EMAIL"
    SMS = "SMS"
    VOICE = "VOICE"


class SenderVerification(str, Enum):
    KNOWN = "KNOWN"            # recognized sender for the tenant — flows straight through
    UNKNOWN = "UNKNOWN"        # not recognized — held + confirm step, no run
    CONFIRMED = "CONFIRMED"    # a held event advanced via the confirm step


class IntakeOutcomeStatus(str, Enum):
    RUN_CREATED = "RUN_CREATED"                       # parsed + fired sourcing run; ack reply
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"       # ambiguous; stubbed clarify reply; NO run
    UNKNOWN_SENDER_CONFIRM_SENT = "UNKNOWN_SENDER_CONFIRM_SENT"  # held + stubbed confirm; NO run
    REJECTED_MALFORMED = "REJECTED_MALFORMED"         # garbage inbound; safe reject; NO run
    TENANT_UNKNOWN = "TENANT_UNKNOWN"                 # to-address resolves to no tenant; NO run
    FLAG_OFF = "FLAG_OFF"                             # INTAKE_CHANNELS_V1 off; no-op


@dataclass
class IntakeAttachment:
    """One media attachment on an inbound message (a nameplate photo, an MMS
    image). Held as raw bytes so the same image-handling the in-app upload
    path uses (IntakeAgent.run images kwarg) consumes it directly — no disk
    I/O, no path (I4)."""
    filename: str
    content_type: str
    data: bytes

    def to_dict(self) -> dict:
        return {"filename": self.filename, "content_type": self.content_type,
                "size_bytes": len(self.data)}


@dataclass
class IntakeEvent:
    """The ONE normalized intake event every channel adapter produces.

    tenant_key — the resolved tenant (plus-local-part for email, mapped number
      for SMS/voice). Set by the adapter from the to-address/number; re-validated
      by the consumer.
    channel — EMAIL | SMS | VOICE.
    sender — the originating address/number (customer side, NOT supplier).
    text_body — the message text (email body, SMS text, voice transcript).
    attachments — nameplate photos / MMS media (raw bytes).
    channel_metadata — provider ids for correlation (message-id, phone number,
      call transcript ref). Opaque to the consumer; carried for audit.
    received_at — ISO 8601 UTC.
    """
    tenant_key: str
    channel: IntakeChannel
    sender: str
    text_body: str
    attachments: List[IntakeAttachment] = field(default_factory=list)
    channel_metadata: Dict[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def validate(self) -> Optional[str]:
        """Return None if the event is well-formed, else a human rejection reason.
        A malformed event is safely rejected (NEVER enters the pipeline)."""
        if not self.tenant_key or not isinstance(self.tenant_key, str):
            return "missing tenant_key"
        if not isinstance(self.channel, IntakeChannel):
            return "invalid channel"
        if not self.sender or not isinstance(self.sender, str):
            return "missing sender"
        # A message with no text AND no attachments is garbage (nothing to parse).
        if not (self.text_body or "").strip() and not self.attachments:
            return "empty message (no text and no attachments)"
        if len(self.text_body) > 20000:
            return "message text too large"
        return None


@dataclass
class IntakeReply:
    """A stubbed reply the consumer emits (ack / clarify / confirm). Recorded,
    never live-sent under the EMAIL_SEND_ENABLED double-gate."""
    to: str
    subject: str
    body: str
    kind: str  # "ack" | "clarify" | "confirm"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IntakeOutcome:
    """The single consumer's result. status drives the response; run_id is set
    only on RUN_CREATED; reply is the stubbed reply to send (always stubbed);
    clarify_attrs / confirm_token carry the NEEDS_CLARIFICATION / confirm-step
    detail. is_test marks provenance on any rows the consumer caused."""
    status: IntakeOutcomeStatus
    run_id: Optional[str] = None
    reply: Optional[IntakeReply] = None
    clarify_attrs: Optional[List[str]] = None
    confirm_token: Optional[str] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Per-tenant addressing (I3 — plus-addressing).
# ---------------------------------------------------------------------------

# tenant_key -> {"company_id", "facility_id"}. Fixture-overridable in tests
# (monkeypatch intake_channels._TENANT_MAP). Production would back this from a
# tenants table; the mapping is the address→tenant resolution, kept separate
# from the run's company_id stamp (which the firer sets from this map).
_TENANT_MAP: Dict[str, Dict[str, str]] = {
    # Default demo tenant — "Bay Foods" owns the mock facilities.
    "bayfoods": {"company_id": "company-bayfoods", "facility_id": "fac-stockton"},
}


def get_tenant_map() -> Dict[str, Dict[str, str]]:
    """The live tenant map (tests monkeypatch _TENANT_MAP or this)."""
    return _TENANT_MAP


def resolve_tenant_from_address(to_address: str) -> Optional[str]:
    """Resolve a tenant_key from an inbound to-address via plus-addressing:
    ``intake+<tenant-key>@<INTAKE_DOMAIN>``. Returns the tenant_key or None
    when the address is not an intake address / the local-part resolves to no
    known tenant. Pure + testable.

    Gmail delivers ``intake+foo@arkim.ai`` to the ``intake@`` inbox and
    preserves the full ``To`` header, so the plus-local-part is the tenant
    signal. A bare ``intake@`` (no plus) resolves to None — no tenant ⇒ reject
    (TENANT_UNKNOWN), never a run from an unattributable address.
    """
    if not to_address or "@" not in to_address:
        return None
    local, _, domain = to_address.partition("@")
    if domain.lower() != INTAKE_DOMAIN.lower():
        return None
    local = local.strip().lower()
    if not local.startswith(_INTAKE_LOCAL_PREFIX):
        return None
    rest = local[len(_INTAKE_LOCAL_PREFIX):]
    if rest.startswith("+"):
        tenant_key = rest[1:].strip().lower()
    elif rest == "":
        return None  # bare intake@ — no tenant attribution
    else:
        return None  # not an intake address
    if not tenant_key:
        return None
    return tenant_key


def tenant_lookup(tenant_key: str) -> Optional[Dict[str, str]]:
    """Look up the tenant's (company_id, facility_id) for run stamping. None if
    the tenant_key is unknown to the map (TENANT_UNKNOWN — no run)."""
    if not tenant_key:
        return None
    return get_tenant_map().get(tenant_key.strip().lower())


# SMS/voice: number -> tenant_key mapping (same shape as email plus-addressing,
# decision 2). Fixture-overridable. Production would back this from a
# number-provisioning table; the mapping is the address→tenant resolution for
# telephony channels, kept in the same spirit (identity encoded in the address).
_NUMBER_TENANT_MAP: Dict[str, str] = {
    # Default demo tenant — a tenant's dedicated inbound SMS/voice number.
    "+15555550100": "bayfoods",
}


def get_number_tenant_map() -> Dict[str, str]:
    """The live number→tenant map (tests monkeypatch _NUMBER_TENANT_MAP)."""
    return _NUMBER_TENANT_MAP


def resolve_tenant_from_number(number: str) -> Optional[str]:
    """Resolve a tenant_key from an inbound SMS/voice number. Returns the
    tenant_key or None when the number maps to no tenant. Pure + testable.

    Mirrors resolve_tenant_from_address: the inbound number is the tenant
    signal for telephony channels (a tenant's dedicated number). E.164
    normalized (strip whitespace/dashes; keep the leading +)."""
    if not number:
        return None
    n = "".join(number.split())            # collapse internal spaces
    n = n.replace("-", "").replace("(", "").replace(")", "")
    n = n.strip()
    if not n:
        return None
    key = get_number_tenant_map().get(n)
    if not key:
        # Try without a leading "+" (some webhooks deliver the bare number).
        key = get_number_tenant_map().get(n.lstrip("+"))
    return key or None


# ---------------------------------------------------------------------------
# Standalone provenance store — known senders + held unknown-sender events.
# Mirrors claim_tokens: own sqlite file, token hashed at rest, is_test marking,
# fail-soft (never raises into the request path).
# ---------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "intake_channels.sqlite")

_KNOWN_SENDERS_DDL = """
CREATE TABLE IF NOT EXISTS intake_known_senders (
    id              TEXT PRIMARY KEY,
    tenant_key      TEXT NOT NULL,
    sender_email    TEXT NOT NULL,
    is_test         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE(tenant_key, sender_email)
);
"""
_HELD_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS intake_held_events (
    id              TEXT PRIMARY KEY,
    tenant_key      TEXT NOT NULL,
    sender          TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    event_json      TEXT NOT NULL,
    is_test         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT
);
"""
_IX_KS = "CREATE INDEX IF NOT EXISTS ix_iks_tenant ON intake_known_senders (tenant_key);"
_IX_HE_HASH = "CREATE INDEX IF NOT EXISTS ix_ihe_hash ON intake_held_events (token_hash);"
_IX_HE_TENANT = "CREATE INDEX IF NOT EXISTS ix_ihe_tenant ON intake_held_events (tenant_key);"

_TOKEN_BYTES = 32
_PREFIX_LEN = 8


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_KNOWN_SENDERS_DDL)
    conn.execute(_HELD_EVENTS_DDL)
    conn.execute(_IX_KS)
    conn.execute(_IX_HE_HASH)
    conn.execute(_IX_HE_TENANT)
    conn.commit()
    return conn


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_sender(raw: str) -> str:
    """Lowercase the email/number for matching. Tolerant: strips mailto:, angles."""
    s = (raw or "").strip().lower()
    if s.startswith("mailto:"):
        s = s[len("mailto:"):]
    s = s.strip("<>").strip()
    return s


# --- known senders ----------------------------------------------------------

def sender_known(tenant_key: str, sender: str) -> bool:
    """True iff ``sender`` is a recognized sender for ``tenant_key``. Flag-off →
    False (the surface is dormant). Fail-soft: False on any error."""
    if not _intake_enabled():
        return False
    tk = (tenant_key or "").strip().lower()
    addr = _normalize_sender(sender)
    if not tk or not addr:
        return False
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT 1 FROM intake_known_senders WHERE tenant_key = ? AND sender_email = ?",
                (tk, addr),
            ).fetchone()
            return row is not None
    except Exception as exc:
        print(f"[IntakeChannels] sender_known failed: {exc}")
        return False


def add_known_sender(tenant_key: str, sender: str, *, is_test: bool = True) -> bool:
    """Register a recognized sender for a tenant (admin / seed / test). Idempotent
    on (tenant, sender). Returns True on a write, False on flag-off / error.
    ``is_test`` defaults True — test/seed rows are marked; production onboarding
    would set False."""
    if not _intake_enabled():
        return False
    tk = (tenant_key or "").strip().lower()
    addr = _normalize_sender(sender)
    if not tk or not addr:
        return False
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO intake_known_senders (id, tenant_key, sender_email, is_test, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), tk, addr, 1 if is_test else 0, _now()),
            )
            conn.commit()
            return True
    except Exception as exc:
        print(f"[IntakeChannels] add_known_sender failed: {exc}")
        return False


# --- held unknown-sender events ---------------------------------------------

def hold_event(event: IntakeEvent, *, is_test: bool = True) -> Optional[str]:
    """Hold an unknown-sender event and return a one-time confirm token (raw),
    or None on flag-off / error. The token hash is stored; the raw token exists
    only in the returned value (built into the stubbed confirm reply). The full
    event is serialized to ``event_json`` so the confirm step can replay it as a
    CONFIRMED event without re-reading the mailbox."""
    if not _intake_enabled():
        return None
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    digest = _hash_token(raw)
    prefix = raw[:_PREFIX_LEN]
    try:
        payload = {
            "tenant_key": event.tenant_key,
            "channel": event.channel.value,
            "sender": event.sender,
            "text_body": event.text_body,
            "attachments": [a.to_dict() for a in event.attachments],  # bytes omitted (not JSON-safe)
            "channel_metadata": event.channel_metadata,
            "received_at": event.received_at,
        }
        with closing(_get_conn()) as conn:
            conn.execute(
                "INSERT INTO intake_held_events "
                "(id, tenant_key, sender, token_hash, token_prefix, event_json, is_test, created_at, confirmed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), event.tenant_key.strip().lower(), _normalize_sender(event.sender),
                 digest, prefix, json.dumps(payload), 1 if is_test else 0, _now(), None),
            )
            conn.commit()
        return raw
    except Exception as exc:
        print(f"[IntakeChannels] hold_event failed: {exc}")
        return None


def consume_held(token: str) -> Optional[Dict[str, Any]]:
    """Validate a confirm token and return the held event's payload (marked
    confirmed_at). None on flag-off / bad token / error. Lookup is by hash —
    never a string compare over raw tokens (mirrors claim_tokens). A held event
    confirms ONCE (confirmed_at set); a reuse returns None."""
    if not _intake_enabled():
        return None
    if not token or not isinstance(token, str):
        return None
    digest = _hash_token(token)
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM intake_held_events WHERE token_hash = ?", (digest,)
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("confirmed_at"):
                return None  # already confirmed / reused
            conn.execute(
                "UPDATE intake_held_events SET confirmed_at = ? WHERE id = ?",
                (_now(), d["id"]),
            )
            conn.commit()
            return json.loads(d["event_json"])
    except Exception as exc:
        print(f"[IntakeChannels] consume_held failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Parser — propose-don't-invent via the EXISTING IntakeAgent (fed AS IT IS).
# ---------------------------------------------------------------------------

def parse_event_to_specs(
    event: IntakeEvent,
    *,
    intake_agent: Optional[Any] = None,
    anthropic_api_key: Optional[str] = None,
) -> Optional[dict]:
    """Propose a structured request (asset_specs) from the event by running the
    EXISTING IntakeAgent over the message text + photo attachments — the same
    extractor the in-app intake chat / upload path uses. Returns the agent's
    full result dict ({asset_specs, sufficient, follow_up_question,
    commit_message, confidence_summary, ...}) or None on an extractor failure.

    Propose-don't-invent: the agent's ``sufficient`` flag + the caller's
    family-variant check gate what enters sourcing. An ambiguous message yields
    sufficient=False + a follow_up_question → the consumer replies NEEDS_CLARIFICATION,
    never a confidently-wrong request. The existing clarification logic is fed
    AS IT IS (not fixed — Night 7's territory).

    Fail-soft: an extractor exception returns None (the consumer treats None as
    a NEEDS_CLARIFICATION with a generic ask — never invents specs, never crashes).
    """
    from utils.procurement_agent.agents.intake_agent import IntakeAgent
    from utils.models import SourcingRun

    agent = intake_agent or IntakeAgent(anthropic_api_key=anthropic_api_key)
    run_obj = SourcingRun(id=str(uuid.uuid4()), current_phase="intake", asset_specs_json={})
    images = [a.data for a in event.attachments if a.data]
    try:
        return agent.run(run_obj, {"text": event.text_body or "", "images": images})
    except Exception as exc:
        print(f"[IntakeChannels] IntakeAgent parse failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Reply builders — always stubbed under the EMAIL_SEND_ENABLED double-gate.
# ---------------------------------------------------------------------------

_ACK_SUBJECT = "Re: your parts request — received"
_ACK_BODY = ("Got it — we're on it. Your request has been received and sourcing "
             "has started. We'll follow up with options.")

_CLARIFY_SUBJECT = "Re: your parts request — a few more details"
_CONFIRM_SUBJECT = "Please confirm your parts request"
_CONFIRM_BODY = ("We received a parts request from this address but don't have "
                 "it on file as an authorized sender for your plant. Reply to "
                 "confirm this request came from you, or ignore this message.")


def build_ack_reply(sender: str) -> IntakeReply:
    return IntakeReply(to=sender, subject=_ACK_SUBJECT, body=_ACK_BODY, kind="ack")


def build_clarify_reply(sender: str, question: str, missing_labels: Optional[List[str]] = None) -> IntakeReply:
    if missing_labels:
        body = (f"We need a few more details before we can source this part. "
                f"Please confirm: {', '.join(missing_labels)}.")
    else:
        body = (f"We need a few more details before we can source this part. "
                f"{question}" if question else
                "We need a few more details before we can source this part — "
                "please reply with the manufacturer, model, and part number.")
    return IntakeReply(to=sender, subject=_CLARIFY_SUBJECT, body=body, kind="clarify")


def build_confirm_reply(sender: str, confirm_token: str) -> IntakeReply:
    # The confirm link carries the token in the path (mirrors the supplier portal).
    # Stubbed — never live-sent. The token is the ONLY place the raw value lives.
    link = f"https://procurement.arkim.ai/api/intake/confirm/{confirm_token}"
    body = _CONFIRM_BODY + f"\n\nConfirm: {link}"
    return IntakeReply(to=sender, subject=_CONFIRM_SUBJECT, body=body, kind="confirm",
                       metadata={"confirm_token": confirm_token})


# ---------------------------------------------------------------------------
# The single consumer — maps a valid event into the existing pipeline seam.
# Injector DI: the run-firing + reply sending are injected callables so the
# consumer is testable without the FastAPI app and so the no-order property
# can be proven against the real firer.
# ---------------------------------------------------------------------------

# fire_sourcing_run(specs_dict, tenant_key) -> Optional[str]
#   Creates a run (Phase.INTAKE, company_id/facility from the tenant map), seeds
#   the specs, and fires the confirm-intake transition (Phase.SOURCING + the
#   background sourcing task). Returns the run_id, or None if the transition
#   refused (e.g. a family-variant block the consumer should surface as
#   NEEDS_CLARIFICATION). NEVER places/approves/advances an order.
FireSourcingRun = Callable[[dict, str], Optional[str]]

# reply_sink(reply: IntakeReply) -> None — sends (stubbed) / records the reply.
ReplySink = Callable[[IntakeReply], None]


def _default_reply_sink(reply: IntakeReply) -> None:
    """Default sink: record (print) the stubbed reply. The api_server wiring
    replaces this with the email_sender-backed sink (itself stubbed under the
    EMAIL_SEND_ENABLED double-gate). Either way, nothing is live-sent."""
    print(f"[IntakeChannels] STUBBED reply ({reply.kind}) -> {reply.to}: {reply.subject}")


def consume_intake_event(
    event: IntakeEvent,
    *,
    fire_sourcing_run: FireSourcingRun,
    reply_sink: Optional[ReplySink] = None,
    intake_agent: Optional[Any] = None,
    anthropic_api_key: Optional[str] = None,
    is_test: bool = True,
) -> IntakeOutcome:
    """The single consumer. Maps a valid, tenant-attributed, sender-verified
    intake event into the existing pipeline seam and fires the sourcing run
    exactly as an in-app request does — or replies NEEDS_CLARIFICATION / a
    confirm step / a safe rejection. NEVER creates a run from an unverified
    stranger, an ambiguous message, or an unattributable tenant.

    Order of gates (each NO-run outcome is terminal):
      FLAG_OFF          — surface dormant.
      REJECTED_MALFORMED— malformed event (validate()).
      TENANT_UNKNOWN    — to-address tenant resolves to no known tenant.
      UNKNOWN_SENDER_.. — sender not recognized for the tenant → held + confirm.
      NEEDS_CLARIFICATION — parser not sufficient, or a family-variant block.
      RUN_CREATED       — parser sufficient + family-OK → fire_sourcing_run → ack.

    AUTO-ORDER IS OUT: this consumer calls only fire_sourcing_run (run creation
    + the confirm-intake→sourcing transition) and reply_sink. It never places,
    approves, or advances an order — the no-order property test pins this.
    """
    sink = reply_sink or _default_reply_sink

    if not _intake_enabled():
        return IntakeOutcome(status=IntakeOutcomeStatus.FLAG_OFF, reason="INTAKE_CHANNELS_V1 off")

    # Gate 1 — malformed inbound is safely rejected (nothing enters the pipeline).
    reason = event.validate()
    if reason is not None:
        return IntakeOutcome(status=IntakeOutcomeStatus.REJECTED_MALFORMED, reason=reason)

    # Gate 2 — tenant attribution. An unattributable address never births a run.
    if tenant_lookup(event.tenant_key) is None:
        return IntakeOutcome(status=IntakeOutcomeStatus.TENANT_UNKNOWN,
                             reason=f"unknown tenant_key={event.tenant_key!r}")

    # Gate 3 — unknown-sender defence. A stranger is held + sent a confirm step;
    # no sourcing run is created from an unverified sender.
    if not sender_known(event.tenant_key, event.sender):
        token = hold_event(event, is_test=is_test)
        if not token:
            # Store failure (fail-soft): refuse to create a run rather than
            # silently letting an unverified stranger through. Safe default.
            return IntakeOutcome(status=IntakeOutcomeStatus.REJECTED_MALFORMED,
                                 reason="unable to hold unknown-sender event")
        sink(build_confirm_reply(event.sender, token))
        return IntakeOutcome(status=IntakeOutcomeStatus.UNKNOWN_SENDER_CONFIRM_SENT,
                             confirm_token=token)

    # Gate 4 — propose-don't-invent. Run the existing IntakeAgent over the
    # message (+ photos). An ambiguous message is NEEDS_CLARIFICATION, never a
    # confidently-wrong request entering sourcing.
    parsed = parse_event_to_specs(event, intake_agent=intake_agent,
                                  anthropic_api_key=anthropic_api_key)
    if parsed is None:
        # Extractor failed (fail-soft): ask for details, never invent specs.
        reply = build_clarify_reply(event.sender, "")
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="extractor unavailable", reply=reply)

    if not parsed.get("sufficient"):
        question = parsed.get("follow_up_question") or ""
        reply = build_clarify_reply(event.sender, question)
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="insufficient specs", reply=reply)

    specs = parsed.get("asset_specs") or {}

    # Gate 5 — family-variant binding (the existing confirm_intake guard, fed AS
    # IT IS). A family-level request for a variant-selecting class with an
    # unanswered variant attr is NEEDS_CLARIFICATION (named attrs), never a run
    # on an open family / a hallucinated rating.
    from utils.procurement_agent.agents.intake_agent import family_disambig_block
    block = family_disambig_block(specs)
    if block is not None:
        reply = build_clarify_reply(event.sender, "", missing_labels=block.get("missing_labels"))
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="family_variant_unconfirmed",
                             clarify_attrs=block.get("missing_attrs"),
                             reply=reply)

    # Gate 6 — fire the sourcing run through the existing seam (injected). The
    # firer creates the run + advances to SOURCING + schedules the background
    # sourcing task — exactly as an in-app confirm-intake does. Same flags, same
    # gates. NEVER orders/approves.
    run_id = fire_sourcing_run(specs, event.tenant_key)
    if not run_id:
        # The firer refused (e.g. a guard the consumer didn't pre-check). Treat
        # as NEEDS_CLARIFICATION rather than fake success — surface honestly.
        reply = build_clarify_reply(event.sender, "")
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="sourcing run not fired", reply=reply)

    reply = build_ack_reply(event.sender)
    sink(reply)
    return IntakeOutcome(status=IntakeOutcomeStatus.RUN_CREATED, run_id=run_id, reply=reply)


def consume_confirmed_event(
    held_payload: Dict[str, Any],
    *,
    fire_sourcing_run: FireSourcingRun,
    reply_sink: Optional[ReplySink] = None,
    intake_agent: Optional[Any] = None,
    anthropic_api_key: Optional[str] = None,
    is_test: bool = True,
) -> IntakeOutcome:
    """Replay a held event (advanced via the confirm step) as a CONFIRMED sender
    and run the consumer. The sender is now verified, so the unknown-sender gate
    is skipped; the parser + family + firing gates run as normal. Builds the
    IntakeEvent from the held payload (attachments are NOT replayed — held events
    serialize attachment metadata only, not bytes; a real confirm would re-fetch
    the original message. For the build, the confirm path re-parses text-only)."""
    try:
        event = IntakeEvent(
            tenant_key=held_payload["tenant_key"],
            channel=IntakeChannel(held_payload["channel"]),
            sender=held_payload["sender"],
            text_body=held_payload.get("text_body") or "",
            attachments=[],  # bytes not held; see docstring.
            channel_metadata=held_payload.get("channel_metadata") or {},
            received_at=held_payload.get("received_at") or datetime.now(timezone.utc).isoformat(),
        )
    except (KeyError, ValueError) as exc:
        return IntakeOutcome(status=IntakeOutcomeStatus.REJECTED_MALFORMED,
                             reason=f"held payload malformed: {exc}")

    if not _intake_enabled():
        return IntakeOutcome(status=IntakeOutcomeStatus.FLAG_OFF, reason="INTAKE_CHANNELS_V1 off")
    if event.validate() is not None:
        return IntakeOutcome(status=IntakeOutcomeStatus.REJECTED_MALFORMED,
                             reason=event.validate())
    if tenant_lookup(event.tenant_key) is None:
        return IntakeOutcome(status=IntakeOutcomeStatus.TENANT_UNKNOWN,
                             reason=f"unknown tenant_key={event.tenant_key!r}")

    # The sender is CONFIRMED — skip the unknown-sender gate, run parse → fire.
    parsed = parse_event_to_specs(event, intake_agent=intake_agent,
                                  anthropic_api_key=anthropic_api_key)
    sink = reply_sink or _default_reply_sink
    if parsed is None or not parsed.get("sufficient"):
        reply = build_clarify_reply(event.sender, (parsed or {}).get("follow_up_question") or "")
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="insufficient specs", reply=reply)
    specs = parsed.get("asset_specs") or {}
    from utils.procurement_agent.agents.intake_agent import family_disambig_block
    block = family_disambig_block(specs)
    if block is not None:
        reply = build_clarify_reply(event.sender, "", missing_labels=block.get("missing_labels"))
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="family_variant_unconfirmed",
                             clarify_attrs=block.get("missing_attrs"), reply=reply)
    run_id = fire_sourcing_run(specs, event.tenant_key)
    if not run_id:
        reply = build_clarify_reply(event.sender, "")
        sink(reply)
        return IntakeOutcome(status=IntakeOutcomeStatus.NEEDS_CLARIFICATION,
                             reason="sourcing run not fired", reply=reply)
    reply = build_ack_reply(event.sender)
    sink(reply)
    return IntakeOutcome(status=IntakeOutcomeStatus.RUN_CREATED, run_id=run_id, reply=reply)
