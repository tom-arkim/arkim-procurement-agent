"""
Tests for the RFQ-draft persistence + enforced lifecycle (RFQ wiring A0).

The integrity spine under test: a draft can NEVER reach 'sent' without first passing through
'approved'. Pure persistence — no endpoints, no send, no Apollo, no email.
"""

import pytest

from utils.procurement_agent.state.persistence import (
    create_run,
    create_draft,
    get_draft,
    list_drafts,
    transition_draft,
    can_transition_draft,
    DraftTransitionError,
)


def _draft(db_url, **over):
    """Create a run + one drafted RFQ draft; return (run_id, draft_dict)."""
    run_id = create_run(db_url=db_url)["id"]
    snap = over.get("candidate_snapshot", {"vendor_name": "Acme Pumps", "source_url": "https://acme.com", "base_price": 200.0})
    draft = create_draft(
        run_id=run_id,
        candidate_id=over.get("candidate_id", "Acme Pumps-t3-0"),
        candidate_snapshot=snap,
        draft_body=over.get("draft_body", "Subject: Quote Request — pump seal\n\nHello Acme,"),
        db_url=db_url,
    )
    return run_id, draft


class TestCreateDraft:
    def test_create_draft_is_drafted_with_frozen_snapshot(self, db_url):
        snap = {"vendor_name": "Acme Pumps", "source_url": "https://acme.com", "base_price": 200.0}
        run_id, d = _draft(db_url, candidate_snapshot=snap)
        assert d["status"] == "drafted"
        assert d["run_id"] == run_id
        assert d["candidate_id"] == "Acme Pumps-t3-0"
        assert d["draft_body"].startswith("Subject: Quote Request")
        assert d["candidate_snapshot"] == snap          # frozen exactly as sourced
        # No approval/rejection/send recorded while drafted — nothing preset.
        assert d["approved_by"] is None and d["approved_at"] is None
        assert d["rejected_by"] is None and d["rejected_at"] is None
        assert d["sent_message_id"] is None

    def test_get_and_list_drafts(self, db_url):
        run_id, d = _draft(db_url)
        assert get_draft(d["id"], db_url=db_url)["id"] == d["id"]
        assert get_draft("nope", db_url=db_url) is None
        listed = list_drafts(run_id, db_url=db_url)
        assert [x["id"] for x in listed] == [d["id"]]


class TestLegalLifecycle:
    def test_drafted_to_approved_to_sent(self, db_url):
        _, d = _draft(db_url)
        approved = transition_draft(d["id"], "approved", approved_by="tom@arkim.ai", db_url=db_url)
        assert approved["status"] == "approved"
        assert approved["approved_by"] == "tom@arkim.ai"
        assert approved["approved_at"] is not None        # stamped by the transition, not preset
        assert approved["sent_message_id"] is None         # still null until sent

        sent = transition_draft(d["id"], "sent", sent_message_id="sm-123", db_url=db_url)
        assert sent["status"] == "sent"
        assert sent["sent_message_id"] == "sm-123"

    def test_drafted_to_rejected(self, db_url):
        _, d = _draft(db_url)
        rejected = transition_draft(d["id"], "rejected", rejected_by="tom@arkim.ai", db_url=db_url)
        assert rejected["status"] == "rejected"
        assert rejected["rejected_by"] == "tom@arkim.ai"
        assert rejected["rejected_at"] is not None

    def test_can_transition_pure(self, db_url):
        assert can_transition_draft("drafted", "approved")
        assert can_transition_draft("drafted", "rejected")
        assert can_transition_draft("approved", "sent")
        assert not can_transition_draft("drafted", "sent")
        assert not can_transition_draft("rejected", "approved")
        assert not can_transition_draft("sent", "approved")


class TestIllegalTransitionsRaise:
    def test_drafted_to_sent_skips_approval(self, db_url):
        _, d = _draft(db_url)
        with pytest.raises(DraftTransitionError):
            transition_draft(d["id"], "sent", sent_message_id="x", db_url=db_url)
        # The draft did NOT move, and no send id leaked onto it.
        after = get_draft(d["id"], db_url=db_url)
        assert after["status"] == "drafted" and after["sent_message_id"] is None

    def test_approved_to_drafted_backward(self, db_url):
        _, d = _draft(db_url)
        transition_draft(d["id"], "approved", approved_by="a", db_url=db_url)
        with pytest.raises(DraftTransitionError):
            transition_draft(d["id"], "drafted", db_url=db_url)

    def test_reapprove_blocked(self, db_url):
        _, d = _draft(db_url)
        transition_draft(d["id"], "approved", approved_by="a", db_url=db_url)
        with pytest.raises(DraftTransitionError):
            transition_draft(d["id"], "approved", approved_by="b", db_url=db_url)

    def test_rejected_to_approved_resurrect(self, db_url):
        _, d = _draft(db_url)
        transition_draft(d["id"], "rejected", rejected_by="a", db_url=db_url)
        with pytest.raises(DraftTransitionError):
            transition_draft(d["id"], "approved", approved_by="b", db_url=db_url)

    def test_sent_is_terminal(self, db_url):
        _, d = _draft(db_url)
        transition_draft(d["id"], "approved", approved_by="a", db_url=db_url)
        transition_draft(d["id"], "sent", sent_message_id="sm", db_url=db_url)
        for target in ("approved", "rejected", "drafted", "sent"):
            with pytest.raises(DraftTransitionError):
                transition_draft(d["id"], target, db_url=db_url)

    def test_unknown_draft_raises(self, db_url):
        with pytest.raises(DraftTransitionError):
            transition_draft("does-not-exist", "approved", approved_by="a", db_url=db_url)


class TestApprovalIsRealNotPreset:
    def test_sent_message_id_null_until_sent(self, db_url):
        _, d = _draft(db_url)
        assert get_draft(d["id"], db_url=db_url)["sent_message_id"] is None
        transition_draft(d["id"], "approved", approved_by="a", db_url=db_url)
        assert get_draft(d["id"], db_url=db_url)["sent_message_id"] is None  # still null when approved
        transition_draft(d["id"], "sent", sent_message_id="sm-9", db_url=db_url)
        assert get_draft(d["id"], db_url=db_url)["sent_message_id"] == "sm-9"
