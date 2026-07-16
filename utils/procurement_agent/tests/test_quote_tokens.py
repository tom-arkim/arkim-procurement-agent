"""
Night 11 T2 — utils/quote_tokens.py: per-RFQ quote-submission tokens
(QUOTE_SUBMIT_V1).

Covers (QUOTE_SUBMISSION_SPEC.md §3, brief T2):
  - hashed at rest (no raw token / no recoverable secret on disk),
  - single-RFQ scope (the validated context is exactly the minted request),
  - NOT single-use (revision-friendly: validation never consumes),
  - RFQ-window expiry → the honest CLOSED state (not an error, not a 404),
  - revoke (single + per-RFQ) → closed state,
  - unknown token → None (the route's uniform-404 input; no oracle),
  - CROSS-TOKEN ISOLATION vs utils/claim_tokens.py: a claim token never
    validates as a quote token and vice versa (separate stores, structurally),
  - flag-off dormancy (defense-in-depth; the route gate is the boundary).

Isolated stores: each test points quote_tokens (and claim_tokens where used)
at temp sqlite files and opts in to the flags explicitly.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from utils import quote_tokens as qt


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """Isolated quote_tokens store + QUOTE_SUBMIT_V1 ON."""
    monkeypatch.setenv("QUOTE_SUBMIT_V1", "1")
    monkeypatch.setattr(qt, "_DB_PATH", str(tmp_path / "quote_tokens.sqlite"))
    return qt


@pytest.fixture()
def claim_store(monkeypatch, tmp_path):
    """Isolated claim_tokens store + SUPPLIER_PORTAL_V1 ON (for the
    cross-token isolation tests)."""
    from utils import claim_tokens as ct
    monkeypatch.setenv("SUPPLIER_PORTAL_V1", "1")
    monkeypatch.setattr(ct, "CLAIM_TOKENS_ENABLED", True)
    monkeypatch.setattr(ct, "_DB_PATH", str(tmp_path / "claim_tokens.sqlite"))
    return ct


def _mint(store, **overrides):
    kwargs = dict(
        supplier_domain="dxpe.com",
        vendor_name="DXP Enterprises",
        run_id="run-1",
        rfq_id="rfq-9",
        part_key="gusher pumps|84004-28-C238CBC",
        manufacturer="Gusher Pumps",
        part_number="84004-28-C238CBC",
        quantity=2,
        need_by="2026-08-01",
    )
    kwargs.update(overrides)
    return store.mint_for_rfq(**kwargs)


def _rows(store):
    conn = sqlite3.connect(store._DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM quote_tokens").fetchall()]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Mint + hash-at-rest
# ---------------------------------------------------------------------------

class TestMint:
    def test_mint_returns_raw_token_once(self, store):
        out = _mint(store)
        assert out and out["token"] and out["token_id"]
        assert out["supplier_domain"] == "dxpe.com"
        assert out["run_id"] == "run-1" and out["rfq_id"] == "rfq-9"

    def test_hashed_at_rest_no_raw_token_on_disk(self, store):
        out = _mint(store)
        (row,) = _rows(store)
        assert out["token"] not in str(row.values())
        assert row["token_hash"] != out["token"]
        assert len(row["token_hash"]) == 64  # SHA-256 hex
        # Only a non-forgeable prefix is kept in the clear.
        assert row["token_prefix"] == out["token"][:8]

    def test_empty_domain_rejected(self, store):
        assert _mint(store, supplier_domain="") is None

    def test_default_expiry_is_the_14_day_rfq_window(self, store):
        out = _mint(store)
        until = datetime.fromisoformat(out["expires_at"])
        delta = until - datetime.now(timezone.utc)
        assert timedelta(days=13) < delta <= timedelta(days=14)

    def test_expiry_configurable(self, store):
        out = _mint(store, expiry_days=3)
        until = datetime.fromisoformat(out["expires_at"])
        assert until - datetime.now(timezone.utc) <= timedelta(days=3)

    def test_is_test_provenance_recorded(self, store):
        _mint(store)  # default True in tests
        (row,) = _rows(store)
        assert row["is_test"] == 1


# ---------------------------------------------------------------------------
# Validate — live / closed / unknown, and NOT single-use
# ---------------------------------------------------------------------------

class TestValidate:
    def test_live_token_returns_the_minted_request_context(self, store):
        out = _mint(store)
        row = store.validate_token(out["token"])
        assert row["state"] == "live"
        assert row["run_id"] == "run-1"
        assert row["rfq_id"] == "rfq-9"
        assert row["part_key"] == "gusher pumps|84004-28-C238CBC"
        assert row["supplier_domain"] == "dxpe.com"
        assert row["vendor_name"] == "DXP Enterprises"
        assert row["manufacturer"] == "Gusher Pumps"
        assert row["part_number"] == "84004-28-C238CBC"
        assert row["quantity"] == 2
        assert row["need_by"] == "2026-08-01"

    def test_not_single_use_revisions_allowed(self, store):
        out = _mint(store)
        assert store.validate_token(out["token"])["state"] == "live"
        assert store.validate_token(out["token"])["state"] == "live"  # again

    def test_unknown_token_is_none(self, store):
        _mint(store)
        assert store.validate_token("not-a-real-token") is None
        assert store.validate_token("") is None
        assert store.validate_token(None) is None

    def test_expired_rfq_window_reads_closed_not_none(self, store):
        out = _mint(store)
        conn = sqlite3.connect(store._DB_PATH)
        conn.execute(
            "UPDATE quote_tokens SET expires_at = ? WHERE id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
             out["token_id"]))
        conn.commit()
        conn.close()
        row = store.validate_token(out["token"])
        assert row is not None            # a KNOWN token is never a 404
        assert row["state"] == "closed"   # honest closed state, no live form

    def test_revoked_token_reads_closed(self, store):
        out = _mint(store)
        assert store.revoke(out["token_id"]) is True
        assert store.validate_token(out["token"])["state"] == "closed"

    def test_revoke_is_idempotent_guarded(self, store):
        out = _mint(store)
        assert store.revoke(out["token_id"]) is True
        assert store.revoke(out["token_id"]) is False  # already revoked
        assert store.revoke("no-such-id") is False

    def test_revoke_for_rfq_closes_every_token_of_that_rfq_only(self, store):
        a1 = _mint(store, rfq_id="rfq-9")
        a2 = _mint(store, rfq_id="rfq-9", supplier_domain="sealit.example")
        b = _mint(store, rfq_id="rfq-10")
        assert store.revoke_for_rfq("rfq-9") == 2
        assert store.validate_token(a1["token"])["state"] == "closed"
        assert store.validate_token(a2["token"])["state"] == "closed"
        assert store.validate_token(b["token"])["state"] == "live"


# ---------------------------------------------------------------------------
# Cross-token isolation (brief T2 — required)
# ---------------------------------------------------------------------------

class TestCrossTokenIsolation:
    def test_claim_token_never_opens_the_quote_namespace(self, store, claim_store):
        claim = claim_store.generate_for("dxpe.com")
        assert claim and claim["token"]
        assert store.validate_token(claim["token"]) is None

    def test_quote_token_never_opens_the_claim_namespace(self, store, claim_store):
        quote = _mint(store)
        assert claim_store.validate_token(quote["token"]) is None

    def test_stores_are_separate_files(self, store, claim_store):
        _mint(store)
        claim_store.generate_for("dxpe.com")
        assert store._DB_PATH != claim_store._DB_PATH
        # The quote DB has no claim_tokens table and vice versa.
        conn = sqlite3.connect(store._DB_PATH)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "quote_tokens" in names and "claim_tokens" not in names
        conn = sqlite3.connect(claim_store._DB_PATH)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        conn.close()
        assert "claim_tokens" in names and "quote_tokens" not in names


# ---------------------------------------------------------------------------
# Flag gating + admin listing hygiene
# ---------------------------------------------------------------------------

class TestFlagGatingAndListing:
    def test_flag_off_everything_noops(self, monkeypatch, tmp_path):
        monkeypatch.setattr(qt, "_DB_PATH", str(tmp_path / "quote_tokens.sqlite"))
        # conftest autouse pin keeps QUOTE_SUBMIT_V1 off.
        assert _mint(qt) is None
        assert qt.validate_token("anything") is None
        assert qt.revoke("any-id") is False
        assert qt.revoke_for_rfq("rfq-9") == 0
        assert qt.list_for_run("run-1") == []

    def test_flag_off_validate_rejects_previously_minted(self, store, monkeypatch):
        out = _mint(store)
        monkeypatch.setenv("QUOTE_SUBMIT_V1", "")
        assert store.validate_token(out["token"]) is None

    def test_list_for_run_returns_metadata_never_secrets(self, store):
        out = _mint(store)
        (row,) = store.list_for_run("run-1")
        assert row["id"] == out["token_id"]
        assert row["supplier_domain"] == "dxpe.com"
        assert "token_hash" not in row
        assert out["token"] not in str(row.values())
        assert row["token_prefix"] == out["token"][:8]  # prefix only
