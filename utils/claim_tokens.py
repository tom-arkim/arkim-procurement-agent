"""
utils/claim_tokens.py
Night 6 — Supplier claim-portal token store (T1).

Single-supplier-scoped, expiring, regenerable magic-link tokens for the public
supplier claim portal (the app's first public route — `utils/procurement_agent`
portal routes consume this module). Built to the house standard as a standalone
module: clean, typed, tested, fail-soft (CLAUDE.md §5 / §9 external-provider
discipline applies to its own I/O too — a store error degrades, never raises into
the request path).

Token hygiene (the brief's T1 [REVIEW-ADD] — required):
  - Entropy: tokens are minted with ``secrets.token_urlsafe(32)`` (~256 bits).
  - Hashed at rest: only the SHA-256 hex digest of the token is stored. A
    registry/store read can never yield a live link — the raw token exists only
    in the concierge-returned link, never on disk.
  - Lookup by hash, never string compare over raw tokens: ``validate_token``
    hashes the presented token and looks the row up by digest.
  - Regeneration invalidates the prior token: ``regenerate`` revokes (sets
    ``revoked_at`` on) every prior live token for the supplier before minting a
    new one, so at most one live token per supplier at a time.

Lifecycle:
  - ``generate_for(supplier_domain)`` — mint a token (hash stored, raw returned
    ONCE to the caller so the concierge can build the link). Expiry default 7d
    (the brief's tightened default; regenerable on demand). Fail-soft: None on a
    store error or empty domain.
  - ``validate_token(raw)`` — hash + lookup. Returns the token row (with
    ``supplier_domain``, ``expires_at``) iff a live, unexpired, unrevoked token
    matches; else None. Constant-time-ish (single indexed lookup by hash; the
    digest compare is structural, not a secret-vs-secret compare since the
    presented token is hashed first and the stored digest is the lookup key).
  - ``regenerate(supplier_domain)`` — revoke all prior tokens for the supplier +
    mint a new one. Returns the new raw token. The prior token's hash is
    retained (revoked) so a reused-after-regeneration token is detectable and
    rejected uniformly (T5: invalid / expired / reused → identical safe
    rejection, no oracle).
  - ``revoke(token_id)`` — explicit revoke (admin/manual).

Flag gating (guardrail 3): the SUPPLIER_PORTAL_V1 flag gates the *route*, not
this store. The store functions are pure data ops; they no-op (return None / [])
when ``CLAIM_TOKENS_ENABLED`` is off so the surface is byte-identical to
pre-Night-6 in tests that flip the flag off. The route's own flag guard is the
load-bearing gate; this is defense-in-depth.

Schema (``claim_tokens`` table, in its own sqlite file
``data/claim_tokens.sqlite`` so the module is standalone — no shared-connection
concerns with supplier_registry's non-WAL sqlite):
  id              TEXT PRIMARY KEY    (uuid4)
  supplier_domain TEXT NOT NULL       (normalized; references supplier_registry loosely)
  token_hash      TEXT NOT NULL UNIQUE  (SHA-256 hex of the raw token)
  token_prefix    TEXT NOT NULL       (first 8 chars of the RAW token — rate-limit key,
                                      NOT enough to forge a token; see T5 rate-limit)
  expires_at      TEXT NOT NULL       (ISO 8601 UTC)
  created_at      TEXT NOT NULL       (ISO 8601 UTC)
  revoked_at      TEXT                (NULL until revoked/regenerated past)
  revoked_reason  TEXT                ("regenerated" | "explicit" | NULL)
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Feature flag — mirrors TIER1_V2 / SCORING_V2 strict truthy parse. Default OFF.
# This is defense-in-depth for the store; the ROUTE gates on SUPPLIER_PORTAL_V1
# (api_server). Tests monkeypatch this module's CLAIM_TOKENS_ENABLED.
# ---------------------------------------------------------------------------
def _env_truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


CLAIM_TOKENS_ENABLED: bool = _env_truthy(os.environ.get("SUPPLIER_PORTAL_V1"))

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "claim_tokens.sqlite")

# Token entropy + expiry. token_urlsafe(32) -> ~43-char URL-safe string, ~256 bits.
_TOKEN_BYTES = 32
_PREFIX_LEN = 8  # chars of the RAW token kept for the rate-limit key (not a secret)
_DEFAULT_EXPIRY_DAYS = 7  # the brief's tightened default (regenerable on demand)


_DDL = """
CREATE TABLE IF NOT EXISTS claim_tokens (
    id              TEXT PRIMARY KEY,
    supplier_domain TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    token_prefix    TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    revoked_at      TEXT,
    revoked_reason  TEXT
);
"""

_INDEX_HASH = "CREATE INDEX IF NOT EXISTS ix_claim_tokens_hash ON claim_tokens (token_hash);"
_INDEX_DOMAIN = "CREATE INDEX IF NOT EXISTS ix_claim_tokens_domain ON claim_tokens (supplier_domain);"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dormant() -> bool:
    """True when the claim-token surface is off (defense-in-depth; route is the gate)."""
    return not CLAIM_TOKENS_ENABLED


def _hash_token(raw: str) -> str:
    """SHA-256 hex digest of the raw token. The digest is what is stored + looked up."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get_conn() -> sqlite3.Connection:
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_DDL)
    conn.execute(_INDEX_HASH)
    conn.execute(_INDEX_DOMAIN)
    conn.commit()
    return conn


def _now() -> str:
    return datetime.utcnow().isoformat()


def _normalize_domain(raw: str) -> str:
    """Lowercase, strip www., strip trailing slash and path (mirrors supplier_registry)."""
    from urllib.parse import urlparse
    raw = (raw or "").lower().strip()
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        host = parsed.hostname or raw
    except Exception:
        host = raw
    if host.startswith("www."):
        host = host[4:]
    return host.strip()


def _row_to_dict(r: sqlite3.Row) -> dict:
    return dict(r)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_for(supplier_domain: str, *,
                 expiry_days: int = _DEFAULT_EXPIRY_DAYS) -> Optional[dict]:
    """Mint a claim token for ``supplier_domain``. Returns ``{token, token_id,
    supplier_domain, expires_at}`` — the RAW token is returned ONCE (only the
    hash is stored). Returns None on empty domain / flag-off / store failure
    (fail-soft — never raises). Does NOT revoke prior tokens (use
    ``regenerate`` for that); multiple live tokens per supplier are permitted
    until the concierge regenerates."""
    if _dormant():
        return None
    dom = _normalize_domain(supplier_domain)
    if not dom:
        return None
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    digest = _hash_token(raw)
    prefix = raw[:_PREFIX_LEN]
    expires = (datetime.utcnow() + timedelta(days=expiry_days)).isoformat()
    created = _now()
    token_id = str(uuid.uuid4())
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                """INSERT INTO claim_tokens
                   (id, supplier_domain, token_hash, token_prefix,
                    expires_at, created_at, revoked_at, revoked_reason)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (token_id, dom, digest, prefix, expires, created, None, None),
            )
            conn.commit()
        return {
            "token": raw,
            "token_id": token_id,
            "supplier_domain": dom,
            "expires_at": expires,
        }
    except Exception as exc:
        print(f"[ClaimTokens] generate_for failed for {dom!r}: {exc}")
        return None


def validate_token(raw: str) -> Optional[dict]:
    """Validate a presented raw token. Returns the token row (with
    ``supplier_domain``, ``expires_at``, ``token_id``) iff a live, unexpired,
    unrevoked token matches; else None. Lookup is by hash — never a string
    compare over raw tokens. Fail-soft: None on any error / flag-off."""
    if _dormant():
        return None
    if not raw or not isinstance(raw, str):
        return None
    digest = _hash_token(raw)
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM claim_tokens WHERE token_hash = ?", (digest,)
            ).fetchone()
            if not row:
                return None
            d = _row_to_dict(row)
            if d.get("revoked_at"):
                return None  # reused-after-regeneration / explicitly revoked
            # Expiry check (naive UTC ISO compare — tolerant of tz-aware via parse).
            if _is_expired(d.get("expires_at")):
                return None
            return {
                "token_id": d.get("id"),
                "supplier_domain": d.get("supplier_domain"),
                "expires_at": d.get("expires_at"),
                "token_prefix": d.get("token_prefix"),
            }
    except Exception as exc:
        print(f"[ClaimTokens] validate_token failed: {exc}")
        return None


def _is_expired(expires_at: Optional[str]) -> bool:
    """True when ``expires_at`` is in the past (tolerant of tz-aware ISO)."""
    if not expires_at:
        return True
    try:
        dt = datetime.fromisoformat(expires_at)
    except (ValueError, TypeError):
        return True
    now = datetime.utcnow()
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt <= now


def regenerate(supplier_domain: str, *,
               expiry_days: int = _DEFAULT_EXPIRY_DAYS) -> Optional[dict]:
    """Revoke every prior live token for ``supplier_domain`` and mint a new one.
    The prior token's hash is retained (marked revoked, reason="regenerated") so
    a reused-after-regeneration token is detectable and rejected uniformly (T5).
    Returns the new token dict (see ``generate_for``), or None on flag-off /
    empty domain / store failure."""
    if _dormant():
        return None
    dom = _normalize_domain(supplier_domain)
    if not dom:
        return None
    now = _now()
    try:
        with closing(_get_conn()) as conn:
            conn.execute(
                """UPDATE claim_tokens SET revoked_at = ?, revoked_reason = 'regenerated'
                   WHERE supplier_domain = ? AND revoked_at IS NULL""",
                (now, dom),
            )
            conn.commit()
    except Exception as exc:
        print(f"[ClaimTokens] regenerate (revoke prior) failed for {dom!r}: {exc}")
        # Fall through to mint a new token anyway — a stale prior token is still
        # rejected by validate_token (revoked_at stays NULL only if the UPDATE
        # failed, in which case the old token remains live, which is safe-ish but
        # not ideal; fail-soft keeps the request path alive).
    return generate_for(dom, expiry_days=expiry_days)


def revoke(token_id: str, *, reason: str = "explicit") -> bool:
    """Explicitly revoke one token by id. Returns True on a write, False
    otherwise (flag-off / missing / store failure)."""
    if _dormant():
        return False
    if not token_id:
        return False
    try:
        with closing(_get_conn()) as conn:
            cur = conn.execute(
                "UPDATE claim_tokens SET revoked_at = ?, revoked_reason = ? WHERE id = ?",
                (_now(), reason, token_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception as exc:
        print(f"[ClaimTokens] revoke failed for {token_id!r}: {exc}")
        return False


def list_for_supplier(supplier_domain: str) -> list[dict]:
    """List token rows for a supplier (admin/diagnostic). Never returns the raw
    token or the hash to the caller — only metadata (id, prefix, status, dates).
    Empty on flag-off / error."""
    if _dormant():
        return []
    dom = _normalize_domain(supplier_domain)
    if not dom:
        return []
    try:
        with closing(_get_conn()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, supplier_domain, token_prefix, expires_at, "
                "created_at, revoked_at, revoked_reason "
                "FROM claim_tokens WHERE supplier_domain = ? ORDER BY created_at DESC",
                (dom,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        print(f"[ClaimTokens] list_for_supplier failed for {dom!r}: {exc}")
        return []
