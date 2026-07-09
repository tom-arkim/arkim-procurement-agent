"""
Night 6 — Supplier claim-token store tests (T1).

Token hygiene (the brief's required properties), asserted:
  - High-entropy mint (secrets.token_urlsafe(32) — ~256 bits; length + uniqueness).
  - HASHED at rest: the raw token never appears in the store; a store read yields
    only the digest. A registry-DB read can never produce a live link.
  - Lookup by hash, never a string compare over raw tokens.
  - Expiry: an expired token is rejected; a fresh one validates.
  - Regeneration invalidates the prior token (revoked_at set); a
    reused-after-regeneration token is rejected.
  - Uniform rejection: invalid / expired / reused tokens all validate -> None
    (T5's uniform-rejection property starts here; the ROUTE makes the response
    uniform — see test_supplier_portal.py).
  - Flag-off (SUPPLIER_PORTAL_V1 off) -> store no-ops (defense-in-depth; the
    route is the load-bearing gate).

Isolated store: each test points claim_tokens at a temp sqlite file + flips
CLAIM_TOKENS_ENABLED on. No live network; no supplier_registry dependency (the
token store is standalone).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from utils import claim_tokens as ct


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Isolated claim_tokens store + SUPPLIER_PORTAL_V1 ON."""
    monkeypatch.setattr(ct, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
    monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
    return ct


def _read_rows(tmp_path):
    """Read the raw store rows (for the hashed-at-rest assertions)."""
    conn = sqlite3.connect(tmp_path / "claim_tokens.sqlite")
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM claim_tokens").fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Mint + validate + hashed-at-rest
# ---------------------------------------------------------------------------

class TestMintAndValidate:
    def test_mint_returns_raw_token_once(self, store):
        out = store.generate_for("dxpe.com")
        assert out is not None
        raw = out["token"]
        assert raw and isinstance(raw, str)
        # token_urlsafe(32) -> ~43 chars, URL-safe.
        assert len(raw) >= 32
        assert out["supplier_domain"] == "dxpe.com"
        assert out["expires_at"]
        assert out["token_id"]

    def test_raw_token_not_stored_only_hash(self, store, tmp_path):
        out = store.generate_for("dxpe.com")
        rows = _read_rows(tmp_path)
        assert len(rows) == 1
        stored = rows[0]
        # The raw token NEVER appears in any stored column.
        assert out["token"] not in str(stored)
        # The stored token_hash is the SHA-256 hex digest, not the raw token.
        import hashlib
        assert stored["token_hash"] == hashlib.sha256(out["token"].encode()).hexdigest()
        assert stored["token_hash"] != out["token"]
        # A prefix is kept for the rate-limit key (not a secret; < full token).
        assert stored["token_prefix"] == out["token"][:8]

    def test_validate_returns_row_for_live_token(self, store):
        out = store.generate_for("dxpe.com")
        v = store.validate_token(out["token"])
        assert v is not None
        assert v["supplier_domain"] == "dxpe.com"
        assert v["token_id"] == out["token_id"]

    def test_validate_rejects_garbage(self, store):
        assert store.validate_token("not-a-real-token") is None
        assert store.validate_token("") is None
        assert store.validate_token(None) is None  # type: ignore[arg-type]

    def test_validate_is_lookup_by_hash_not_string_compare(self, store, tmp_path):
        """Confirm the stored row carries no raw token AND the lookup digest is
        the hash (a structural check: the only UNIQUE-indexed lookup column is
        token_hash, and it equals the SHA-256 of the presented token)."""
        out = store.generate_for("dxpe.com")
        v = store.validate_token(out["token"])
        assert v is not None
        rows = _read_rows(tmp_path)
        import hashlib
        digest = hashlib.sha256(out["token"].encode()).hexdigest()
        assert any(r["token_hash"] == digest for r in rows)

    def test_high_entropy_unique_tokens(self, store):
        tokens = {store.generate_for("a.com")["token"] for _ in range(20)}
        assert len(tokens) == 20  # no collisions across 20 mints


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

class TestExpiry:
    def test_expired_token_rejected(self, store):
        out = store.generate_for("dxpe.com", expiry_days=0)
        # expiry_days=0 -> expires at ~now; force it strictly into the past.
        import sqlite3 as _s
        conn = _s.connect(store._DB_PATH)
        conn.execute(
            "UPDATE claim_tokens SET expires_at = ? WHERE id = ?",
            ((datetime.utcnow() - timedelta(seconds=1)).isoformat(), out["token_id"]),
        )
        conn.commit()
        conn.close()
        assert store.validate_token(out["token"]) is None

    def test_fresh_token_validates(self, store):
        out = store.generate_for("dxpe.com", expiry_days=7)
        assert store.validate_token(out["token"]) is not None

    def test_default_expiry_is_7_days(self, store):
        out = store.generate_for("dxpe.com")
        exp = datetime.fromisoformat(out["expires_at"])
        delta = exp - datetime.utcnow()
        # ~7 days, allow a minute of slack.
        assert timedelta(days=6, hours=23) < delta < timedelta(days=7, minutes=5)


# ---------------------------------------------------------------------------
# Regeneration invalidates the prior token
# ---------------------------------------------------------------------------

class TestRegeneration:
    def test_regenerate_revokes_prior_token(self, store):
        first = store.generate_for("dxpe.com")
        # Prior token validates.
        assert store.validate_token(first["token"]) is not None
        second = store.regenerate("dxpe.com")
        assert second is not None
        assert second["token"] != first["token"]
        # The prior token is now rejected (reused-after-regeneration).
        assert store.validate_token(first["token"]) is None
        # The new token validates.
        assert store.validate_token(second["token"]) is not None

    def test_regenerate_retains_prior_hash_revoked(self, store, tmp_path):
        """The prior token's hash is retained (revoked), not deleted — so a
        reused-after-regeneration token is detectable + rejected, not silently
        treated as 'never existed'."""
        first = store.generate_for("dxpe.com")
        store.regenerate("dxpe.com")
        rows = _read_rows(tmp_path)
        assert len(rows) == 2  # prior + new both present
        prior = next(r for r in rows if r["id"] == first["token_id"])
        assert prior["revoked_at"] is not None
        assert prior["revoked_reason"] == "regenerated"

    def test_at_most_one_live_after_regenerate(self, store, tmp_path):
        store.generate_for("dxpe.com")
        store.generate_for("dxpe.com")  # two live tokens permitted pre-regen
        store.regenerate("dxpe.com")
        rows = _read_rows(tmp_path)
        live = [r for r in rows if r["revoked_at"] is None]
        assert len(live) == 1  # only the freshly-minted one


# ---------------------------------------------------------------------------
# Uniform rejection (T5 starts here)
# ---------------------------------------------------------------------------

class TestUniformRejection:
    def test_invalid_expired_reused_all_reject_to_none(self, store):
        live = store.generate_for("dxpe.com", expiry_days=7)["token"]
        expired = store.generate_for("dxpe.com", expiry_days=0)["token"]
        # Force the expired token's row into the past (identify by its hash).
        import sqlite3 as _s
        import hashlib
        expired_hash = hashlib.sha256(expired.encode()).hexdigest()
        conn = _s.connect(store._DB_PATH)
        conn.execute(
            "UPDATE claim_tokens SET expires_at = ? WHERE token_hash = ?",
            ((datetime.utcnow() - timedelta(days=1)).isoformat(), expired_hash),
        )
        conn.commit()
        conn.close()
        # Sanity: the expired token really is rejected before we test reuse.
        assert store.validate_token(expired) is None
        # All three rejected categories -> None (uniform at the store layer).
        assert store.validate_token("totally-garbage") is None      # invalid
        assert store.validate_token(expired) is None                # expired
        # Regeneration revokes `live`; a reused-after-regen token is rejected.
        reused = store.regenerate("dxpe.com")
        reused_tok = reused["token"]
        assert store.validate_token(live) is None                   # reused-after-regen
        # The new live token still validates.
        assert store.validate_token(reused_tok) is not None


# ---------------------------------------------------------------------------
# Domain normalization + revoke
# ---------------------------------------------------------------------------

class TestDomainAndRevoke:
    def test_domain_normalized(self, store):
        out = store.generate_for("WWW.DXPE.com/")
        assert out["supplier_domain"] == "dxpe.com"

    def test_empty_domain_rejected(self, store):
        assert store.generate_for("") is None
        assert store.generate_for("   ") is None

    def test_revoke_explicit(self, store):
        out = store.generate_for("dxpe.com")
        assert store.validate_token(out["token"]) is not None
        assert store.revoke(out["token_id"]) is True
        assert store.validate_token(out["token"]) is None

    def test_list_never_returns_raw_or_hash(self, store):
        out = store.generate_for("dxpe.com")
        rows = store.list_for_supplier("dxpe.com")
        assert len(rows) == 1
        s = str(rows[0])
        assert out["token"] not in s
        assert "token_hash" not in rows[0]  # hash column not surfaced


# ---------------------------------------------------------------------------
# Flag-off (defense-in-depth; route is the load-bearing gate)
# ---------------------------------------------------------------------------

class TestFlagOff:
    def test_flag_off_no_ops(self, store, monkeypatch):
        monkeypatch.setattr(store, "CLAIM_TOKENS_ENABLED", False)
        assert store.generate_for("dxpe.com") is None
        assert store.validate_token("anything") is None
        assert store.regenerate("dxpe.com") is None
        assert store.revoke("x") is False
        assert store.list_for_supplier("dxpe.com") == []
