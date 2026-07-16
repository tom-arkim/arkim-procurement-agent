"""
utils/quote_tokens.py
Night 11 — Per-RFQ quote-submission token store (QUOTE_SUBMIT_V1, T2).

The path-A credential (QUOTE_SUBMISSION_SPEC.md §3): a tokenized link minted per
RFQ send and embedded in the RFQ email (`/quote/{token}`), letting the supplier
— claimed or unclaimed — submit a structured quote with NO account.

Deliberately a SEPARATE namespace from utils/claim_tokens.py (brief I2): its own
module, its own sqlite file, its own table. A claim token must never open a
quote form and vice versa — isolation is structural (different stores; a
presented token is hashed and looked up only in THIS table), and cross-token
tests pin it.

Token hygiene — the claim-token pattern re-applied verbatim:
  - Entropy: ``secrets.token_urlsafe(32)`` (~256 bits).
  - Hashed at rest: only the SHA-256 hex digest is stored; the raw token exists
    once, in the mint return value (embedded into the outbound RFQ).
  - Lookup by hash, never a string compare over raw tokens.
  - Unknown token → None (the route renders the uniform 404 — no oracle).

Where quote tokens DIFFER from claim tokens (spec §3):
  - Scope: single-RFQ. The row carries the request identity (run_id, rfq_id,
    part_key, supplier_domain) plus the display context the token page needs
    (part identity, qty, need-by, the supplier's own name). The token can only
    quote THAT request.
  - NOT single-use: the supplier may revise their quote — each submission
    supersedes (quote_store handles supersede). Validation never consumes.
  - Expiry = the RFQ's validity window (default 14 days; configurable).
  - Dead-RFQ honesty: a KNOWN token whose RFQ has closed (expired window or
    revoked/withdrawn) validates to state="closed" — the route renders an
    honest "this request has closed", never an error page and never a live
    form writing to a dead request. Only an UNKNOWN token is None/404: knowing
    a real token proves receipt of the RFQ email, so the closed state leaks
    nothing to an enumerator.

Flag gating: the ROUTE gate (api_server, QUOTE_SUBMIT_V1) is load-bearing;
store functions no-op when the flag is off (defense-in-depth, same layering as
claim_tokens). Rows carry ``is_test`` provenance like every Night-11 store.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Optional


def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _active() -> bool:
    """Live flag read (mirrors quote_store.quote_submit_active)."""
    return _env_truthy(os.environ.get("QUOTE_SUBMIT_V1"))


def _dormant() -> bool:
    return not _active()


_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "quote_tokens.sqlite")

_TOKEN_BYTES = 32
_PREFIX_LEN = 8  # rate-limit key material only — not enough to forge a token
DEFAULT_EXPIRY_DAYS = 14  # the RFQ validity window (spec §3)

STATE_LIVE = "live"
STATE_CLOSED = "closed"


_DDL = """
CREATE TABLE IF NOT EXISTS quote_tokens (
    id              TEXT PRIMARY KEY,
    run_id          TEXT,
    rfq_id          TEXT,
    part_key        TEXT,
    supplier_domain TEXT NOT NULL,
    vendor_name     TEXT,
    manufacturer    TEXT,
    part_number     TEXT,
    quantity        REAL,
    need_by         TEXT,
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT,
    revoked_reason  TEXT,
    is_test         INTEGER NOT NULL DEFAULT 0
);
"""

_INDEX_HASH = ("CREATE INDEX IF NOT EXISTS ix_quote_tokens_hash "
               "ON quote_tokens (token_hash);")
_INDEX_RUN = ("CREATE INDEX IF NOT EXISTS ix_quote_tokens_run "
              "ON quote_tokens (run_id);")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.execute(_INDEX_HASH)
    conn.execute(_INDEX_RUN)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_domain(raw: str) -> str:
    from utils.supplier_registry import _normalize_domain as _nd
    return _nd(raw or "")


def _is_expired(expires_at: Optional[str]) -> bool:
    """True when past expiry (tolerant ISO parse; naive treated as UTC —
    mirrors claim_tokens._is_expired)."""
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt <= datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mint_for_rfq(
    *,
    supplier_domain: str,
    run_id: Optional[str] = None,
    rfq_id: Optional[str] = None,
    part_key: Optional[str] = None,
    vendor_name: Optional[str] = None,
    manufacturer: Optional[str] = None,
    part_number: Optional[str] = None,
    quantity: Optional[float] = None,
    need_by: Optional[str] = None,
    expiry_days: int = DEFAULT_EXPIRY_DAYS,
    is_test: bool = True,
) -> Optional[dict]:
    """Mint one quote token scoped to one RFQ send. Returns ``{token, token_id,
    supplier_domain, run_id, rfq_id, expires_at}`` — the RAW token is returned
    ONCE (only the hash is stored); the caller embeds it in the RFQ email.
    Fail-soft: None on flag-off / empty domain / store failure. Minting is
    admin-gated (or mechanical at send time) at the CALLER — this is the store.
    """
    if _dormant():
        return None
    dom = _normalize_domain(supplier_domain)
    if not dom:
        return None
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    token_id = str(uuid.uuid4())
    expires = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                """INSERT INTO quote_tokens
                   (id, run_id, rfq_id, part_key, supplier_domain, vendor_name,
                    manufacturer, part_number, quantity, need_by, token_hash,
                    token_prefix, expires_at, created_at, revoked_at,
                    revoked_reason, is_test)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (token_id, run_id, rfq_id, part_key, dom, vendor_name,
                 manufacturer, part_number, quantity, need_by, _hash_token(raw),
                 raw[:_PREFIX_LEN], expires, _now(), None, None,
                 1 if is_test else 0),
            )
            conn.commit()
        return {
            "token": raw,
            "token_id": token_id,
            "supplier_domain": dom,
            "run_id": run_id,
            "rfq_id": rfq_id,
            "expires_at": expires,
        }
    except Exception as exc:
        print(f"[QuoteTokens] mint_for_rfq failed for {dom!r}: {exc}")
        return None


def validate_token(raw: str) -> Optional[dict]:
    """Validate a presented raw token (hash lookup, THIS table only — a claim
    token can never match here).

    Returns None for an UNKNOWN token (route → uniform 404, no oracle).
    Returns the token context with ``state``:
      - "live"   — unexpired, unrevoked: the form may render and submissions
                   may write. NOT consumed — revisions are allowed (spec §5).
      - "closed" — the RFQ has closed (expired window or revoked/withdrawn):
                   the route renders the honest closed state, never a form.
    The returned context is what the token page may show (spec §3): the request
    identity + the supplier's own name/domain. Nothing else lives in the row.
    Fail-soft: None on any store error / flag-off."""
    if _dormant():
        return None
    if not raw or not isinstance(raw, str):
        return None
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM quote_tokens WHERE token_hash = ?",
                (_hash_token(raw),),
            ).fetchone()
    except Exception as exc:
        print(f"[QuoteTokens] validate_token failed: {exc}")
        return None
    if not row:
        return None
    d = dict(row)
    closed = bool(d.get("revoked_at")) or _is_expired(d.get("expires_at"))
    return {
        "state": STATE_CLOSED if closed else STATE_LIVE,
        "token_id": d.get("id"),
        "run_id": d.get("run_id"),
        "rfq_id": d.get("rfq_id"),
        "part_key": d.get("part_key"),
        "supplier_domain": d.get("supplier_domain"),
        "vendor_name": d.get("vendor_name"),
        "manufacturer": d.get("manufacturer"),
        "part_number": d.get("part_number"),
        "quantity": d.get("quantity"),
        "need_by": d.get("need_by"),
        "expires_at": d.get("expires_at"),
        "token_prefix": d.get("token_prefix"),
    }


def revoke(token_id: str, *, reason: str = "explicit") -> bool:
    """Revoke one token (admin action / RFQ withdrawn → the token page renders
    the closed state from then on). True on a write."""
    if _dormant():
        return False
    if not token_id:
        return False
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "UPDATE quote_tokens SET revoked_at = ?, revoked_reason = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (_now(), reason, token_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as exc:
        print(f"[QuoteTokens] revoke failed for {token_id!r}: {exc}")
        return False


def revoke_for_rfq(rfq_id: str, *, reason: str = "rfq_withdrawn") -> int:
    """Revoke every live token for one RFQ (the withdraw-an-RFQ admin action).
    Returns the number revoked (0 on flag-off / none / error)."""
    if _dormant():
        return 0
    if not rfq_id:
        return 0
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "UPDATE quote_tokens SET revoked_at = ?, revoked_reason = ? "
                "WHERE rfq_id = ? AND revoked_at IS NULL",
                (_now(), reason, rfq_id),
            )
            conn.commit()
            return cur.rowcount
    except Exception as exc:
        print(f"[QuoteTokens] revoke_for_rfq failed for {rfq_id!r}: {exc}")
        return 0


def list_for_run(run_id: str) -> list[dict]:
    """Token metadata for a run (admin/diagnostic). NEVER returns the raw token
    or the hash — id, prefix, scope, dates only. Empty on flag-off / error."""
    if _dormant():
        return []
    if not run_id:
        return []
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, run_id, rfq_id, part_key, supplier_domain, "
                "vendor_name, token_prefix, expires_at, created_at, "
                "revoked_at, revoked_reason, is_test "
                "FROM quote_tokens WHERE run_id = ? ORDER BY created_at DESC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        print(f"[QuoteTokens] list_for_run failed for {run_id!r}: {exc}")
        return []
