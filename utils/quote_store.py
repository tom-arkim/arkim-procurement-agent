"""
utils/quote_store.py
Night 11 — Supplier structured-quote store + lifecycle (QUOTE_SUBMIT_V1, T1).

The single store all three quote entry paths write (QUOTE_SUBMISSION_SPEC.md §2):
path A (RFQ quote link), path B (claimed-supplier portal), path C (concierge
entry). One record shape, ``submitted_via`` ∈ {rfq_link, portal, concierge}.

Built to the house standard as a standalone module (CLAUDE.md §5): clean, typed,
tested, fail-soft — a store error degrades (None/[]/False), never raises into a
request path. Mirrors the claim_tokens/intake_channels idioms: own sqlite file,
live flag read, ``is_test`` provenance on every row.

Lifecycle (spec §5):
  active     — drives promotion (the T4 confirmation record, see
               ``as_confirmation_record``).
  review     — flagged by the submission sanity checks (spec §6: pn_differs /
               price wildly off the price_db band / qty far from requested).
               NOT active; a concierge approve activates it, a reject withdraws.
               Flag-not-block: review is the exception path, default is instant
               activation.
  superseded — a newer submission from the same supplier for the same request
               (run_id + supplier_domain) wins; history kept.
  expired    — past valid_until. Evaluated AT READ TIME (no cron): the stored
               status stays as written; ``effective_status`` / the active reads
               compute expiry per read, so an expired quote stops promoting the
               moment it lapses and the card reverts honestly (spec §6).
  withdrawn  — supplier or admin withdrawal, or a review reject (reason kept).

Buyer acceptance is NOT a quote status — it's an order event in the existing
approval flow, referencing the quote id (spec §5).

The I1 promotion contract: ``as_confirmation_record`` adapts an active quote
into EXACTLY the record shape the existing T4 promotion reader consumes — the
``_index_quotes`` item (api_server.py): ``{status: "confirmed",
supplier_domain, thread_id, confidence, payload: {unit_price, currency,
lead_time, ...}}``. Real quotes therefore feed the EXISTING promotion path
(_resolve_quote → _quote_overlay → the read-time Band-C promotion loop); no
second promotion mechanism exists.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Feature flag — read LIVE at call time (mirrors api_server._portal_enabled) so
# the conftest env pin + per-test monkeypatch.setenv are always honored.
# Default OFF.
# ---------------------------------------------------------------------------

def _env_truthy(value: Optional[str]) -> bool:
    """Strict truthy parse (mirrors ranking_bands / claim_tokens): only
    1/true/yes/on enable; everything else fails safe to False."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def quote_submit_active() -> bool:
    """True iff env QUOTE_SUBMIT_V1 is truthy (read at call time)."""
    return _env_truthy(os.environ.get("QUOTE_SUBMIT_V1"))


def _dormant() -> bool:
    """Defense-in-depth store gate (the ROUTE gate in api_server is the
    load-bearing one, same layering as claim_tokens)."""
    return not quote_submit_active()


_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "quotes.sqlite")

# Statuses (spec §5). "review" is the spec's active=false,review=true state,
# expressed as a status so the lifecycle is a single column.
STATUS_ACTIVE = "active"
STATUS_REVIEW = "review"
STATUS_SUPERSEDED = "superseded"
STATUS_EXPIRED = "expired"          # computed at read time, never stored by submit
STATUS_WITHDRAWN = "withdrawn"

VALID_VIAS = ("rfq_link", "portal", "concierge")

# Sanity-check thresholds (spec §6). Price: >3x / <0.2x the price_db median
# where a band exists (band-absent SKIPS — absence of data must not flag every
# quote). Quantity: wildly off the requested qty; the spec doesn't pin the
# multiplier — 10x/0.1x chosen (enumerated in the morning report). Partial
# quotes are real (spec §4), so only a WILD divergence flags.
PRICE_HIGH_MULT = 3.0
PRICE_LOW_MULT = 0.2
QTY_HIGH_MULT = 10.0
QTY_LOW_MULT = 0.1

# Default quote validity window (spec §3: the RFQ's validity window, default 14
# days; configurable per submission via valid_until).
DEFAULT_VALIDITY_DAYS = 14


_DDL = """
CREATE TABLE IF NOT EXISTS quotes (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT,
    rfq_id                TEXT,
    part_key              TEXT,
    supplier_domain       TEXT NOT NULL,
    vendor_name           TEXT,
    manufacturer          TEXT,
    requested_part_number TEXT,
    quoted_part_number    TEXT,
    pn_confirmed          INTEGER NOT NULL DEFAULT 1,
    pn_differs            INTEGER NOT NULL DEFAULT 0,
    quote_number          TEXT,
    unit_price            REAL NOT NULL,
    currency              TEXT NOT NULL DEFAULT 'USD',
    quantity              REAL,
    lead_time             TEXT,
    freight               TEXT,
    valid_until           TEXT,
    notes                 TEXT,
    submitted_via         TEXT NOT NULL,
    submitted_by          TEXT,
    submitted_at          TEXT NOT NULL,
    status                TEXT NOT NULL,
    review_reasons_json   TEXT,
    resolved_at           TEXT,
    resolved_by           TEXT,
    is_test               INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);
"""

_INDEX_RUN = "CREATE INDEX IF NOT EXISTS ix_quotes_run ON quotes (run_id);"
_INDEX_DOMAIN = "CREATE INDEX IF NOT EXISTS ix_quotes_domain ON quotes (supplier_domain);"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.execute(_INDEX_RUN)
    conn.execute(_INDEX_DOMAIN)
    conn.commit()
    return conn


def _now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _now() -> str:
    return _now_dt().isoformat()


def _normalize_domain(raw: str) -> str:
    """One normalization for supplier identity — delegate to the registry's
    (the same key the promotion join uses)."""
    from utils.supplier_registry import _normalize_domain as _nd
    return _nd(raw or "")


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO parse → aware UTC (naive treated as UTC — mirrors
    claim_tokens._is_expired). None on blank/unparseable."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_pn(pn: Optional[str]) -> str:
    from utils.procurement_agent.agents.sourcing_agent import normalize_part_number
    return normalize_part_number(pn or "")


def _row_to_quote(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["review_reasons"] = json.loads(d.get("review_reasons_json") or "[]")
    except (ValueError, TypeError):
        d["review_reasons"] = []
    d["pn_confirmed"] = bool(d.get("pn_confirmed"))
    d["pn_differs"] = bool(d.get("pn_differs"))
    d["is_test"] = bool(d.get("is_test"))
    d["effective_status"] = effective_status(d)
    return d


# ---------------------------------------------------------------------------
# Sanity checks (spec §6) — FLAG, never block
# ---------------------------------------------------------------------------

def compute_review_reasons(
    unit_price: float,
    *,
    pn_differs: bool,
    band_median: Optional[float] = None,
    quantity: Optional[float] = None,
    requested_quantity: Optional[float] = None,
) -> list[str]:
    """The flag-not-block sanity checks. Returns the reasons a submission lands
    in review; empty list ⇒ instant activation (the default path).

      pn_differs           — the wrong-part gate at the quote boundary (spec §4):
                             an edited PN never auto-promotes.
      price_out_of_band    — >3x or <0.2x the price_db median. band_median None
                             (no band for this part) SKIPS the check entirely.
      qty_out_of_band      — wildly off the requested qty (>10x / <0.1x). Either
                             side missing/zero SKIPS (partial quotes are real).

    Pure — the caller resolves the band median (price_band_median) and the
    requested qty; this only applies the thresholds.
    """
    reasons: list[str] = []
    if pn_differs:
        reasons.append("pn_differs")
    if band_median is not None and band_median > 0 and unit_price is not None:
        if unit_price > band_median * PRICE_HIGH_MULT or \
                unit_price < band_median * PRICE_LOW_MULT:
            reasons.append("price_out_of_band")
    if quantity and requested_quantity:
        try:
            q, rq = float(quantity), float(requested_quantity)
            if rq > 0 and (q > rq * QTY_HIGH_MULT or q < rq * QTY_LOW_MULT):
                reasons.append("qty_out_of_band")
        except (TypeError, ValueError):
            pass
    return reasons


def price_band_median(manufacturer: Optional[str],
                      part_number: Optional[str]) -> Optional[float]:
    """The price_db band for the sanity check (I5): the median of the cached
    vendor prices for this (manufacturer, part_number). None when no band
    exists — the check SKIPS on None (absence of data must not flag every
    quote). Fail-soft: None on any store error."""
    if not manufacturer or not part_number:
        return None
    try:
        from utils import price_db
        entries = price_db.get_cached_prices(manufacturer, part_number)
        prices = sorted(
            float(e["price"]) for e in entries.values()
            if e.get("price") is not None and float(e["price"]) > 0
        )
        if not prices:
            return None
        n = len(prices)
        mid = n // 2
        return prices[mid] if n % 2 else (prices[mid - 1] + prices[mid]) / 2.0
    except Exception as exc:
        print(f"[QuoteStore] price_band_median failed for "
              f"{manufacturer!r}/{part_number!r}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Lifecycle reads — expiry is a READ-TIME computation (spec §5, no cron)
# ---------------------------------------------------------------------------

def effective_status(quote: dict, now: Optional[datetime] = None) -> str:
    """The quote's status as of ``now``: a stored-active quote past its
    valid_until is EXPIRED (promotion ceases, no zombie confirmations — spec
    §6). Every other stored status stands as written. A blank/unparseable
    valid_until never expires (tolerant, mirrors the date discipline
    elsewhere)."""
    status = quote.get("status") or STATUS_ACTIVE
    if status != STATUS_ACTIVE:
        return status
    until = _parse_dt(quote.get("valid_until"))
    if until is not None and until <= (now or _now_dt()):
        return STATUS_EXPIRED
    return status


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def submit_quote(
    *,
    supplier_domain: str,
    unit_price: float,
    submitted_via: str,
    run_id: Optional[str] = None,
    rfq_id: Optional[str] = None,
    part_key: Optional[str] = None,
    vendor_name: Optional[str] = None,
    manufacturer: Optional[str] = None,
    requested_part_number: Optional[str] = None,
    quoted_part_number: Optional[str] = None,
    quote_number: Optional[str] = None,
    currency: str = "USD",
    quantity: Optional[float] = None,
    requested_quantity: Optional[float] = None,
    lead_time: Optional[str] = None,
    freight: Optional[str] = None,
    valid_until: Optional[str] = None,
    notes: Optional[str] = None,
    submitted_by: Optional[str] = None,
    band_median: Optional[float] = None,
    is_test: bool = True,
) -> Optional[dict]:
    """Record one structured quote (all three entry paths land here).

    - Supersede-on-resubmit: every prior active/review quote from the same
      supplier for the same request (run_id + supplier_domain) is marked
      superseded — the new submission wins, history kept (spec §5).
    - The wrong-part gate: ``quoted_part_number`` differing from
      ``requested_part_number`` (normalized compare) ⇒ pn_differs, review.
      A blank quoted PN keeps the requested PN (the form prefills it).
    - Sanity checks flag → status "review" with reasons; otherwise "active"
      immediately (spec §6 — review is the exception path).
    - valid_until defaults to now + 14 days (the RFQ validity window).
    - ``band_median`` is the caller-resolved price band (see
      ``price_band_median``); None skips the price check.

    Returns the stored quote dict, or None on flag-off / invalid input / store
    failure (fail-soft — never raises into a request path)."""
    if _dormant():
        return None
    dom = _normalize_domain(supplier_domain)
    if not dom:
        return None
    if submitted_via not in VALID_VIAS:
        return None
    try:
        price = float(unit_price)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None

    req_pn = (requested_part_number or "").strip()
    quoted_pn = (quoted_part_number or "").strip() or req_pn
    pn_differs = bool(req_pn and quoted_pn and
                      _normalize_pn(quoted_pn) != _normalize_pn(req_pn))

    reasons = compute_review_reasons(
        price, pn_differs=pn_differs, band_median=band_median,
        quantity=quantity, requested_quantity=requested_quantity)
    status = STATUS_REVIEW if reasons else STATUS_ACTIVE

    now = _now()
    until = valid_until or (
        _now_dt() + timedelta(days=DEFAULT_VALIDITY_DAYS)).isoformat()
    quote_id = str(uuid.uuid4())
    try:
        with closing(_get_conn()) as conn:
            # Supersede prior live submissions from this supplier for this
            # request — the new one wins; superseded rows keep their content.
            if run_id:
                conn.execute(
                    """UPDATE quotes SET status = ?, resolved_at = ?
                       WHERE run_id = ? AND supplier_domain = ?
                         AND status IN (?, ?)""",
                    (STATUS_SUPERSEDED, now, run_id, dom,
                     STATUS_ACTIVE, STATUS_REVIEW),
                )
            conn.execute(
                """INSERT INTO quotes
                   (id, run_id, rfq_id, part_key, supplier_domain, vendor_name,
                    manufacturer, requested_part_number, quoted_part_number,
                    pn_confirmed, pn_differs, quote_number, unit_price, currency,
                    quantity, lead_time, freight, valid_until, notes,
                    submitted_via, submitted_by, submitted_at, status,
                    review_reasons_json, resolved_at, resolved_by, is_test,
                    created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (quote_id, run_id, rfq_id, part_key, dom, vendor_name,
                 manufacturer, req_pn or None, quoted_pn or None,
                 0 if pn_differs else 1, 1 if pn_differs else 0, quote_number,
                 price, currency or "USD", quantity, lead_time, freight, until,
                 notes, submitted_via, submitted_by, now, status,
                 json.dumps(reasons), None, None, 1 if is_test else 0, now),
            )
            conn.commit()
        print(f"[QuoteStore] Quote recorded: {vendor_name or dom} run={run_id} "
              f"status={status} reasons={reasons or '-'} via={submitted_via}")
        return get_quote(quote_id)
    except Exception as exc:
        print(f"[QuoteStore] submit_quote failed for {dom!r}: {exc}")
        return None


def _transition(quote_id: str, new_status: str, *, from_statuses: tuple,
                resolved_by: Optional[str] = None) -> Optional[dict]:
    """Guarded status transition; returns the updated quote or None (unknown id,
    wrong current status, flag-off, store failure)."""
    if _dormant():
        return None
    if not quote_id:
        return None
    marks = ",".join("?" for _ in from_statuses)
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                f"""UPDATE quotes SET status = ?, resolved_at = ?, resolved_by = ?
                    WHERE id = ? AND status IN ({marks})""",
                (new_status, _now(), resolved_by, quote_id, *from_statuses),
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
        return get_quote(quote_id)
    except Exception as exc:
        print(f"[QuoteStore] transition {quote_id!r} -> {new_status} failed: {exc}")
        return None


def approve_review(quote_id: str, *, resolved_by: Optional[str] = None) -> Optional[dict]:
    """Concierge-approve a review-flagged quote → active (it now promotes). A
    pn_differs quote stays labelled as the QUOTED part number — approval never
    silently relabels it as the requested PN (spec §6 / criterion 4)."""
    return _transition(quote_id, STATUS_ACTIVE,
                       from_statuses=(STATUS_REVIEW,), resolved_by=resolved_by)


def reject_review(quote_id: str, *, resolved_by: Optional[str] = None) -> Optional[dict]:
    """Concierge-reject a review-flagged quote → withdrawn (never promotes)."""
    return _transition(quote_id, STATUS_WITHDRAWN,
                       from_statuses=(STATUS_REVIEW,), resolved_by=resolved_by)


def withdraw(quote_id: str, *, resolved_by: Optional[str] = None) -> Optional[dict]:
    """Withdraw an active or review quote (supplier or admin)."""
    return _transition(quote_id, STATUS_WITHDRAWN,
                       from_statuses=(STATUS_ACTIVE, STATUS_REVIEW),
                       resolved_by=resolved_by)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_quote(quote_id: str) -> Optional[dict]:
    """One quote by id (review_reasons decoded, effective_status computed), or
    None. Not flag-gated — reads of existing rows are harmless and the admin
    surface needs them; the route gate is the boundary."""
    if not quote_id:
        return None
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            r = conn.execute("SELECT * FROM quotes WHERE id = ?",
                             (quote_id,)).fetchone()
            return _row_to_quote(r) if r else None
    except Exception as exc:
        print(f"[QuoteStore] get_quote failed for {quote_id!r}: {exc}")
        return None


def get_quotes(
    run_id: Optional[str] = None,
    supplier_domain: Optional[str] = None,
    status: Optional[str] = None,
) -> list[dict]:
    """Quote rows (newest first), optionally filtered. ``status`` filters on the
    EFFECTIVE status (read-time expiry applied), so status="active" never
    returns a lapsed quote and status="expired" finds them. Fail-soft: []."""
    clauses: list[str] = []
    params: list = []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if supplier_domain is not None:
        clauses.append("supplier_domain = ?")
        params.append(_normalize_domain(supplier_domain))
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM quotes{where} ORDER BY created_at DESC", params
            ).fetchall()
            out = [_row_to_quote(r) for r in rows]
            if status is not None:
                out = [q for q in out if q["effective_status"] == status]
            return out
    except Exception as exc:
        print(f"[QuoteStore] get_quotes failed: {exc}")
        return []


def get_active_quotes(run_id: str) -> list[dict]:
    """The quotes that DRIVE PROMOTION for a run: effectively active only —
    review/superseded/withdrawn excluded by status, expired excluded at read
    time (spec §6: promotion ceases at expiry, honestly)."""
    if not run_id:
        return []
    return get_quotes(run_id=run_id, status=STATUS_ACTIVE)


# ---------------------------------------------------------------------------
# The I1 promotion adapter — the ONLY bridge to the existing T4 seam
# ---------------------------------------------------------------------------

def as_confirmation_record(quote: dict) -> dict:
    """Adapt one active quote into the confirmation-record shape the EXISTING
    promotion reader consumes (api_server._index_quotes items): status
    "confirmed", supplier_domain, thread_id (None — structured quotes join by
    domain), confidence None (supplier-authored, not an extraction — the
    overlay's "no signal" case, so quoteUnverified never flags it), and the
    payload keys _quote_overlay reads (unit_price, currency, lead_time).

    Extra payload keys (quote_id, quoted_part_number, pn_differs, quantity,
    freight, valid_until, quote_number, submitted_at) ride along for the
    buyer-side card (T6) — the legacy overlay ignores what it doesn't read."""
    return {
        "status": "confirmed",
        "supplier_domain": quote.get("supplier_domain"),
        "vendor_name": quote.get("vendor_name"),
        "thread_id": None,
        "confidence": None,
        "payload": {
            "unit_price": quote.get("unit_price"),
            "currency": quote.get("currency") or "USD",
            "lead_time": quote.get("lead_time"),
            "quote_id": quote.get("id"),
            "quote_number": quote.get("quote_number"),
            "quoted_part_number": quote.get("quoted_part_number"),
            "pn_differs": bool(quote.get("pn_differs")),
            "quantity": quote.get("quantity"),
            "freight": quote.get("freight"),
            "valid_until": quote.get("valid_until"),
            "submitted_at": quote.get("submitted_at"),
            "submitted_via": quote.get("submitted_via"),
        },
    }
