"""Tests for utils/run_labels.py — Night 2 Run Labels store.

Covers the brief's overnight-testing obligations for the label module itself:
- write_label appends; read_labels reads back oldest-first; current_label is the latest
- both scopes (run / candidate) round-trip with their payload shape
- fail-soft: a forced write failure doesn't raise AND increments the counter
- flag-off inertness at the module level (zero writes, reads return empty/None)
- append-only: a second write for the same (run, scope, ref) preserves history;
  current_label returns the latest
- pure data / no network on import
- store isolation: writes ONLY to data/run_labels.sqlite (never other stores)
"""

import sqlite3

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def labels(monkeypatch, tmp_path):
    """Isolated run_labels module: flag ON, DB in tmp_path, failures reset."""
    import utils.run_labels as rl
    monkeypatch.setattr(rl, "RUN_CAPTURE", True)
    monkeypatch.setattr(rl, "_DB_PATH", str(tmp_path / "run_labels.sqlite"))
    monkeypatch.setattr(rl, "_DATA_DIR", str(tmp_path))
    rl.reset_failures()
    return rl


@pytest.fixture
def labels_off(monkeypatch, tmp_path):
    """Flag-OFF isolated run_labels (inertness checks)."""
    import utils.run_labels as rl
    monkeypatch.setattr(rl, "RUN_CAPTURE", False)
    monkeypatch.setattr(rl, "_DB_PATH", str(tmp_path / "run_labels_off.sqlite"))
    monkeypatch.setattr(rl, "_DATA_DIR", str(tmp_path))
    rl.reset_failures()
    return rl


# ---------------------------------------------------------------------------
# Schema + pure-data
# ---------------------------------------------------------------------------

class TestSchema:
    def test_import_is_pure_no_network(self):
        """Importing run_labels makes no network/LLM calls (registry purity)."""
        import utils.run_labels as rl
        assert isinstance(rl.RUN_CAPTURE, bool)
        assert rl._DB_PATH.endswith("run_labels.sqlite")

    def test_table_created_on_first_write(self, labels):
        labels.write_label(
            "run-1", labels.SCOPE_RUN,
            {"intake_correct": True, "expected_part_type": "mechanical_seal",
             "expected_component_of": "Goulds 3196", "expected_regime": "ANCHORED",
             "corrections": None, "note": "ok"},
            labeled_by="tom",
        )
        conn = sqlite3.connect(labels._DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "run_labels" in tables
        cols = {r[1] for r in conn.execute("PRAGMA table_info(run_labels)").fetchall()}
        assert {"label_id", "run_id", "scope", "candidate_ref", "label_json",
                "labeled_by", "ts"} <= cols
        conn.close()

    def test_indexes_exist(self, labels):
        labels.write_label("run-1", labels.SCOPE_RUN, {"note": "x"})
        conn = sqlite3.connect(labels._DB_PATH)
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'").fetchall()}
        assert "ix_run_labels_run_id" in idx
        assert "ix_run_labels_run_scope" in idx
        conn.close()


# ---------------------------------------------------------------------------
# Round-trip — both scopes, read-back shapes
# ---------------------------------------------------------------------------

class TestRoundTrip:
    def test_run_scope_label_round_trips(self, labels):
        payload = {
            "intake_correct": False,
            "expected_part_type": "mechanical_seal",
            "expected_component_of": "Goulds 3196",
            "expected_regime": "ANCHORED",
            "corrections": "intake said pump, it's a seal",
            "note": "fix",
        }
        labels.write_label("run-1", labels.SCOPE_RUN, payload, labeled_by="tom")
        out = labels.read_labels("run-1")
        assert len(out) == 1
        assert out[0]["scope"] == labels.SCOPE_RUN
        assert out[0]["candidate_ref"] is None
        assert out[0]["labeled_by"] == "tom"
        assert out[0]["label"]["expected_part_type"] == "mechanical_seal"
        assert out[0]["label"]["intake_correct"] is False
        assert out[0]["label"]["corrections"] == "intake said pump, it's a seal"

    def test_candidate_scope_label_round_trips(self, labels):
        payload = {"right_part_type": "PUMP", "should_pass_floor": False,
                   "note": "pump on a seal request"}
        labels.write_label("run-1", labels.SCOPE_CANDIDATE, payload,
                           candidate_ref="Pumpman-t3-0", labeled_by="tom")
        out = labels.read_labels("run-1")
        assert out[0]["scope"] == labels.SCOPE_CANDIDATE
        assert out[0]["candidate_ref"] == "Pumpman-t3-0"
        assert out[0]["label"]["right_part_type"] == "PUMP"
        assert out[0]["label"]["should_pass_floor"] is False

    def test_read_returns_oldest_first(self, labels):
        labels.write_label("r", labels.SCOPE_RUN, {"note": "first"})
        labels.write_label("r", labels.SCOPE_RUN, {"note": "second"})
        out = labels.read_labels("r")
        assert [o["label"]["note"] for o in out] == ["first", "second"]


# ---------------------------------------------------------------------------
# Append-only + current_label semantics
# ---------------------------------------------------------------------------

class TestAppendOnly:
    def test_relabel_preserves_history(self, labels):
        """A second write for the same (run, scope, ref) appends — history kept."""
        labels.write_label("r", labels.SCOPE_RUN,
                           {"intake_correct": False, "note": "first verdict"})
        labels.write_label("r", labels.SCOPE_RUN,
                           {"intake_correct": True, "note": "revised"})
        out = labels.read_labels("r")
        assert len(out) == 2  # append-only — both rows present

    def test_current_label_is_latest(self, labels):
        labels.write_label("r", labels.SCOPE_RUN, {"note": "first"})
        labels.write_label("r", labels.SCOPE_RUN, {"note": "second"})
        cur = labels.current_label("r", labels.SCOPE_RUN)
        assert cur["label"]["note"] == "second"

    def test_current_label_scoped_by_candidate_ref(self, labels):
        labels.write_label("r", labels.SCOPE_CANDIDATE, {"note": "a"},
                           candidate_ref="A-t2-0")
        labels.write_label("r", labels.SCOPE_CANDIDATE, {"note": "b"},
                           candidate_ref="B-t2-1")
        a = labels.current_label("r", labels.SCOPE_CANDIDATE, candidate_ref="A-t2-0")
        b = labels.current_label("r", labels.SCOPE_CANDIDATE, candidate_ref="B-t2-1")
        assert a["label"]["note"] == "a"
        assert b["label"]["note"] == "b"
        assert labels.current_label("r", labels.SCOPE_CANDIDATE,
                                    candidate_ref="C-none") is None


# ---------------------------------------------------------------------------
# Fail-soft + input validation
# ---------------------------------------------------------------------------

class TestFailSoft:
    def test_bad_scope_records_failure_not_raise(self, labels):
        labels.write_label("r", "bogus", {"note": "x"})
        assert labels.label_failures() == 1
        assert labels.read_labels("r") == []

    def test_candidate_scope_without_ref_records_failure(self, labels):
        labels.write_label("r", labels.SCOPE_CANDIDATE, {"note": "x"})
        assert labels.label_failures() == 1

    def test_forced_write_failure_increments_counter(self, labels, monkeypatch):
        """A forced DB failure doesn't raise AND increments the counter."""
        def _boom(*a, **k):
            raise sqlite3.OperationalError("disk full")
        monkeypatch.setattr(labels, "_get_conn", _boom)
        labels.write_label("r", labels.SCOPE_RUN, {"note": "x"})
        assert labels.label_failures() == 1

    def test_read_failure_is_fail_soft(self, labels, monkeypatch):
        monkeypatch.setattr(labels, "_get_conn",
                            lambda: (_ for _ in ()).throw(sqlite3.OperationalError("io")))
        assert labels.read_labels("r") == []
        assert labels.current_label("r", labels.SCOPE_RUN) is None
        assert labels.labeled_run_ids() == []
        assert labels.read_all_labels() == []
        assert labels.label_failures() == 4


# ---------------------------------------------------------------------------
# Flag-off inertness
# ---------------------------------------------------------------------------

class TestInertness:
    def test_flag_off_write_is_noop(self, labels_off):
        labels_off.write_label("r", labels_off.SCOPE_RUN, {"note": "x"})
        assert labels_off.read_labels("r") == []
        # no DB file created
        import os
        assert not os.path.exists(labels_off._DB_PATH)

    def test_flag_off_reads_return_empty(self, labels_off):
        assert labels_off.read_labels("r") == []
        assert labels_off.current_label("r", labels_off.SCOPE_RUN) is None
        assert labels_off.labeled_run_ids() == []
        assert labels_off.read_all_labels() == []


# ---------------------------------------------------------------------------
# Store isolation — writes ONLY to run_labels.sqlite
# ---------------------------------------------------------------------------

class TestIsolation:
    def test_writes_only_to_run_labels_db(self, labels, tmp_path):
        labels.write_label("r", labels.SCOPE_RUN, {"note": "x"})
        # the only sqlite file in the data dir is run_labels.sqlite
        sqlite_files = [p.name for p in tmp_path.glob("*.sqlite")]
        assert sqlite_files == ["run_labels.sqlite"]


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------

class TestAggregation:
    def test_labeled_run_ids_distinct(self, labels):
        labels.write_label("a", labels.SCOPE_RUN, {"note": "x"})
        labels.write_label("a", labels.SCOPE_RUN, {"note": "y"})
        labels.write_label("b", labels.SCOPE_RUN, {"note": "z"})
        assert labels.labeled_run_ids() == ["a", "b"]

    def test_read_all_labels_full_history(self, labels):
        labels.write_label("a", labels.SCOPE_RUN, {"note": "1"})
        labels.write_label("b", labels.SCOPE_CANDIDATE, {"note": "2"},
                           candidate_ref="V-t2-0")
        all_rows = labels.read_all_labels()
        assert len(all_rows) == 2
        assert {r["run_id"] for r in all_rows} == {"a", "b"}
