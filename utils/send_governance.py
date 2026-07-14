"""
utils/send_governance.py — outbound-send governance (SEND_GOVERNANCE_V1).

The Phase-1 controls from RFQ_LIVE_WIRING_SPEC.md §3, enforced at the LAST seam
before delivery (GmailSender.send consults this module before anything else, ahead
of the EMAIL_SEND_ENABLED check), so no caller of the send layer can bypass them:

    suppression  →  allowlist  →  caps  →  (release, structural)  →  delivery gate

This module NEVER enables delivery. EMAIL_SEND_ENABLED (utils/email_sender.py)
remains the only delivery gate and is untouched by governance: a message that
passes every governance stage still stubs while that gate is off. SEND_GOVERNANCE_V1
gates the governance features only.

FAIL-CLOSED BY CONTRACT — deliberately the opposite of the integration-pattern
fail-soft used for external providers (CLAUDE.md §9): an external-provider error
degrades to a heuristic because the pipeline must survive; a GOVERNANCE store error
must BLOCK the send, because "couldn't verify it's permitted" means "not permitted".
Concretely:
  - empty allowlist        ⇒ every send blocked ("not_allowlisted")
  - missing/unreadable DB  ⇒ blocked at the stage that failed (reason carries the error)
  - a blocked verdict is a RECORDED outcome (the caller stamps it on sent_messages),
    never a silent drop.

Flag OFF ⇒ this module is never consulted on the send path (evaluate() is behind
send_governance_active() in the caller) and behavior is byte-identical to pre-
governance code — parity-tested.

Store: own sqlite (data/send_governance.sqlite), mirroring intake_channels
(own file, is_test marking on rows tests create, module-level _DB_PATH monkeypatch
seam). Domains are normalized with the same rule the supplier registry uses, so
"https://www.DXPE.com/x" and "dxpe.com" are one domain.

Admin mutations (add/remove) are audit-logged via utils.audit_log (who/when/what).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
_DB_PATH = os.path.join(_DATA_DIR, "send_governance.sqlite")

_ALLOWLIST_DDL = """
CREATE TABLE IF NOT EXISTS send_allowlist (
    domain     TEXT PRIMARY KEY,
    added_by   TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    note       TEXT,
    is_test    INTEGER NOT NULL DEFAULT 0
);
"""


def _env_truthy(value: Optional[str]) -> bool:
    """Strict opt-in parse (house rule): only 1/true/yes/on enable; anything else —
    None, "", "0", junk — fails safe to False."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def send_governance_active() -> bool:
    """True iff env SEND_GOVERNANCE_V1 is truthy. Read at call time (tests set/unset
    per case — same convention as ranking_bands / intake flags)."""
    return _env_truthy(os.environ.get("SEND_GOVERNANCE_V1"))


def _normalize_domain(raw: str) -> str:
    """One domain rule for the whole send path — delegate to the supplier registry's
    normalizer (lazy import; registry is already a send-path dependency)."""
    from utils.supplier_registry import _normalize_domain as _nd
    return _nd(raw or "")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_conn() -> sqlite3.Connection:
    """Open the governance store, creating schema on first touch. RAISES on failure —
    callers on the enforcement path convert an exception into a BLOCKED verdict
    (fail-closed), never into an allow."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute(_ALLOWLIST_DDL)
    return conn


# ---------------------------------------------------------------------------
# Allowlist store (admin-managed; audit-logged in the API layer AND here)
# ---------------------------------------------------------------------------

def allowlist_add(domain: str, *, added_by: str, note: Optional[str] = None,
                  is_test: bool = False) -> dict:
    """Add (or refresh) one domain on the send allowlist. Idempotent upsert.
    Raises on store failure (admin surface reports it; nothing on the enforcement
    path calls this). Audit-logged: who allowed what, when."""
    dom = _normalize_domain(domain)
    if not dom:
        raise ValueError(f"not a usable domain: {domain!r}")
    with closing(_get_conn()) as conn:
        conn.execute(
            "INSERT INTO send_allowlist (domain, added_by, added_at, note, is_test) "
            "VALUES (?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET "
            "added_by=excluded.added_by, added_at=excluded.added_at, "
            "note=excluded.note, is_test=excluded.is_test",
            (dom, added_by, _now_iso(), note, 1 if is_test else 0),
        )
        conn.commit()
    _audit("send_allowlist_add", actor=added_by, domain=dom, note=note)
    print(f"[SendGovernance] allowlist ADD {dom} by {added_by}")
    return {"domain": dom, "added_by": added_by}


def allowlist_remove(domain: str, *, removed_by: str) -> bool:
    """Remove one domain from the allowlist. Returns True if a row was removed.
    Audit-logged."""
    dom = _normalize_domain(domain)
    with closing(_get_conn()) as conn:
        cur = conn.execute("DELETE FROM send_allowlist WHERE domain = ?", (dom,))
        conn.commit()
        removed = cur.rowcount > 0
    if removed:
        _audit("send_allowlist_remove", actor=removed_by, domain=dom)
        print(f"[SendGovernance] allowlist REMOVE {dom} by {removed_by}")
    return removed


def allowlist_list() -> list[dict]:
    """All allowlisted domains (admin read). Raises on store failure."""
    with closing(_get_conn()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT domain, added_by, added_at, note, is_test FROM send_allowlist "
            "ORDER BY domain").fetchall()
        return [dict(r) for r in rows]


def _domain_allowlisted(dom: str) -> bool:
    """Enforcement-path membership check. RAISES on store failure — the caller
    (evaluate) converts that into a blocked verdict, fail-closed."""
    with closing(_get_conn()) as conn:
        row = conn.execute(
            "SELECT 1 FROM send_allowlist WHERE domain = ?", (dom,)).fetchone()
        return row is not None


def _audit(mode: str, *, actor: str, domain: str, note: Optional[str] = None) -> None:
    """Best-effort audit event for an admin mutation. Failure to audit must not
    hide the mutation result (it already committed) — logged and swallowed."""
    try:
        from utils.audit_log import write_audit_log
        write_audit_log({
            "sourcing_run_id": None,
            "input_summary": f"{mode}: {domain}" + (f" ({note})" if note else ""),
            "workflow_mode": mode,
            "user_selection": actor,
            "final_recommendation": domain,
            "agent_version": "send-governance-v1",
        })
    except Exception as exc:
        print(f"[SendGovernance] audit write failed for {mode} {domain}: {exc}")


# ---------------------------------------------------------------------------
# Enforcement verdict (consulted by GmailSender.send, ahead of the delivery gate)
# ---------------------------------------------------------------------------

@dataclass
class GovernanceVerdict:
    """Outcome of the governance stack for one outbound message.

    allowed=False carries the ledger `status` for the blocked outcome
    ("suppressed" | "not_allowlisted" | "cap_blocked") and a human-readable
    reason. allowed=True means every governance stage passed — the message may
    proceed to the DELIVERY gate (EMAIL_SEND_ENABLED), which this module never
    touches."""
    allowed: bool
    status: str = "ok"
    reason: str = ""


def _recipient_domains(message) -> list[str]:
    """The set of domains a message would actually deliver to (to + cc) —
    governance judges real recipients, never metadata claims."""
    domains: list[str] = []
    for addr in message.all_recipients:
        dom = _normalize_domain(addr.rsplit("@", 1)[-1]) if "@" in addr else ""
        if dom and dom not in domains:
            domains.append(dom)
    return domains


def evaluate(message) -> GovernanceVerdict:
    """Run the governance stack over one outbound message. Order is load-bearing
    (suppression beats allowlist beats caps — precedence is property-tested):

      1. suppression (T3 — lands ahead of allowlist when built)
      2. allowlist: EVERY recipient domain must be explicitly allowlisted.
         Empty/missing/unreadable allowlist ⇒ blocked (fail-closed).
      3. caps (T2)

    Never raises: any internal failure returns a BLOCKED verdict for the stage
    that failed. Never allows on error."""
    try:
        domains = _recipient_domains(message)
        if not domains:
            return GovernanceVerdict(False, "not_allowlisted",
                                     "no recipient domain to check")
        for dom in domains:
            if not _domain_allowlisted(dom):
                return GovernanceVerdict(False, "not_allowlisted",
                                         f"domain not on send allowlist: {dom}")
        return GovernanceVerdict(True)
    except Exception as exc:
        # Fail-CLOSED: a governance store problem blocks the send.
        return GovernanceVerdict(False, "not_allowlisted",
                                 f"allowlist check failed (fail-closed): {exc}")
