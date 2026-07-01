"""
Phase B0 — run-state foundation for document-sourcing interleaving (STATE ONLY).

Asserts the additions are purely additive with defaults matching today's behavior:
match_basis="direct" (per-candidate), document_status="none" (per-run), single wave;
the results structure can carry wave/basis labels and accept an appended second wave
without losing the first (the append BEHAVIOR is B3 — here it's just list concat). No
aftermarket logic, no document-sourcing wiring, no external actions.
"""

import pytest
from sqlalchemy import text

from utils.models import (
    SourcingRun, SourcingOption, tag_match_wave,
    MATCH_BASIS_DIRECT, MATCH_BASIS_SPEC_MATCHED, MATCH_WAVE_DIRECT,
    DOCUMENT_STATUS_NONE, DOCUMENT_STATUS_INGESTED,
)
from utils.procurement_agent.state import persistence


def _option(name="V"):
    return SourcingOption(vendor_name=name, base_price=10.0, lead_time_days=3,
                          reliability_score=70.0, merchant_type="Enterprise")


# ---------------------------------------------------------------------------
# Defaults preserve today's behavior
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_run_defaults_document_status_none(self):
        assert SourcingRun().document_status == DOCUMENT_STATUS_NONE == "none"

    def test_candidate_defaults_direct_wave_one(self):
        opt = _option()
        assert opt.match_basis == MATCH_BASIS_DIRECT == "direct"
        assert opt.match_wave == MATCH_WAVE_DIRECT == 1

    def test_existing_option_construction_unaffected(self):
        # The new fields are additive with defaults — existing positional construction
        # still works and nothing else changes.
        opt = _option("Grainger")
        assert opt.vendor_name == "Grainger" and opt.base_price == 10.0


# ---------------------------------------------------------------------------
# Appendable-wave structure (labels only; append BEHAVIOR is B3)
# ---------------------------------------------------------------------------

class TestWaveStructure:
    def test_tag_match_wave_stamps_labels(self):
        rows = [{"vendor_name": "A"}, {"vendor_name": "B"}]
        tagged = tag_match_wave(rows, match_basis=MATCH_BASIS_SPEC_MATCHED, wave=2)
        assert tagged is rows  # in place, returns same list
        for r in rows:
            assert r["match_basis"] == "spec_matched" and r["match_wave"] == 2

    def test_tag_defaults_to_direct_wave_one(self):
        rows = [{"vendor_name": "A"}]
        tag_match_wave(rows)
        assert rows[0]["match_basis"] == "direct" and rows[0]["match_wave"] == 1

    def test_tag_does_not_overwrite_existing_labels(self):
        rows = [{"vendor_name": "A", "match_basis": "spec_matched", "match_wave": 2}]
        tag_match_wave(rows, match_basis="direct", wave=1)  # setdefault -> no clobber
        assert rows[0]["match_basis"] == "spec_matched" and rows[0]["match_wave"] == 2

    def test_second_wave_appends_without_losing_first(self):
        wave1 = tag_match_wave([{"vendor_name": "Grainger"}], match_basis=MATCH_BASIS_DIRECT, wave=1)
        wave2 = tag_match_wave([{"vendor_name": "AftermarketCo"}],
                               match_basis=MATCH_BASIS_SPEC_MATCHED, wave=2)
        # The results structure (a per-tier list) holds both waves; append = concat.
        block = {"results": list(wave1), "count": len(wave1), "status": "ok"}
        block["results"].extend(wave2)            # B3 will do this mid-run; B0 just holds it
        bases = {r["match_basis"] for r in block["results"]}
        assert len(block["results"]) == 2
        assert bases == {"direct", "spec_matched"}
        assert block["results"][0]["vendor_name"] == "Grainger"  # wave 1 preserved

    def test_consumer_tolerates_mixed_waves(self):
        # A consumer iterating a tier's results is unaffected by mixed-wave labels.
        results = [{"vendor_name": "A", "match_wave": 1}, {"vendor_name": "B", "match_wave": 2}]
        assert [r["vendor_name"] for r in results] == ["A", "B"]


# ---------------------------------------------------------------------------
# Persistence — additive column, round-trip, idempotent migration
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_round_trip_defaults_none(self, db_url):
        run = persistence.create_run(db_url=db_url, asset_specs={"manufacturer": "Baldor"})
        assert run["document_status"] == "none"
        fetched = persistence.get_run(run["id"], db_url=db_url)
        assert fetched["document_status"] == "none"

    def test_update_document_status_persists(self, db_url):
        run = persistence.create_run(db_url=db_url)
        updated = persistence.update_run(run["id"], {"document_status": DOCUMENT_STATUS_INGESTED},
                                         db_url=db_url)
        assert updated["document_status"] == "ingested"
        assert persistence.get_run(run["id"], db_url=db_url)["document_status"] == "ingested"

    def test_idempotent_migration_preserves_existing_runs(self, tmp_path):
        # Simulate a pre-B0 DB: a sourcing_runs table WITHOUT document_status + a legacy row.
        eng = persistence._make_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
        with eng.connect() as c:
            c.execute(text("CREATE TABLE sourcing_runs (id TEXT PRIMARY KEY, current_phase TEXT)"))
            c.execute(text("INSERT INTO sourcing_runs (id, current_phase) VALUES ('r1', 'intake')"))
            c.commit()

        persistence.migrate_run_state(eng)        # additive
        persistence.migrate_run_state(eng)        # again -> idempotent no-op (no error)

        with eng.connect() as c:
            cols = {row[1] for row in c.execute(text("PRAGMA table_info(sourcing_runs)"))}
            assert "document_status" in cols
            val = c.execute(text("SELECT document_status FROM sourcing_runs WHERE id='r1'")).scalar()
            assert val == "none"                  # existing run reads today's default
