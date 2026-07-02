"""
T7 — schema validation for the labeled scoring eval dataset.

Validates the JSON structure of scoring_eval_dataset.json so T8 can rely on it.
Does NOT run the scorer (that's T8). Pure structural + label sanity checks:
  - the file loads and parses
  - every case has the required fields with the right types
  - ids are unique
  - split is dev | holdout, and BOTH splits are present (T8 reports per-split)
  - the anchor cases exist (Goulds seal should_pass=true, Goulds pump
    should_pass=false, SKF bearing should_pass=true)
  - request.category is Part | Equipment
  - the floor is a positive number
"""

import json
import os
from pathlib import Path

import pytest

_FIXTURE = Path(__file__).parent / "fixtures" / "scoring_eval_dataset.json"


def _load() -> dict:
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def dataset() -> dict:
    return _load()


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

class TestDatasetStructure:
    def test_file_exists(self):
        assert _FIXTURE.exists(), f"eval dataset missing at {_FIXTURE}"

    def test_loads_and_has_meta_and_cases(self, dataset):
        assert "_meta" in dataset, "missing _meta"
        assert "cases" in dataset, "missing cases"
        cases = dataset["cases"]
        assert isinstance(cases, list)
        # Brief asks for ~20-30 labeled pairs.
        assert 20 <= len(cases) <= 30, f"expected 20-30 cases, got {len(cases)}"

    def test_floor_is_positive(self, dataset):
        floor = dataset["_meta"]["schema"]
        # the floor lives at _meta.floor
        assert dataset["_meta"]["floor"] > 0.0


# ---------------------------------------------------------------------------
# Per-case schema
# ---------------------------------------------------------------------------

class TestCaseSchema:
    REQUIRED_REQUEST = {"manufacturer", "model", "part_number", "voltage",
                        "category", "detected_type"}
    REQUIRED_RESULT = {"snippet", "url", "title", "found_pn"}
    REQUIRED_EXPECTED = {"should_pass_floor", "rationale"}

    def test_every_case_has_required_fields(self, dataset):
        for c in dataset["cases"]:
            assert "id" in c and isinstance(c["id"], str), f"case missing id: {c}"
            assert "split" in c, f"{c.get('id')}: missing split"
            assert "request" in c, f"{c['id']}: missing request"
            assert "result" in c, f"{c['id']}: missing result"
            assert "expected" in c, f"{c['id']}: missing expected"
            assert set(c["request"].keys()) >= self.REQUIRED_REQUEST, (
                f"{c['id']}: request missing fields {self.REQUIRED_REQUEST - set(c['request'].keys())}"
            )
            assert set(c["result"].keys()) >= self.REQUIRED_RESULT, (
                f"{c['id']}: result missing fields"
            )
            assert set(c["expected"].keys()) >= self.REQUIRED_EXPECTED, (
                f"{c['id']}: expected missing fields"
            )

    def test_ids_unique(self, dataset):
        ids = [c["id"] for c in dataset["cases"]]
        assert len(ids) == len(set(ids)), f"duplicate ids: {ids}"

    def test_split_values_and_both_present(self, dataset):
        for c in dataset["cases"]:
            assert c["split"] in ("dev", "holdout"), f"{c['id']}: bad split {c['split']!r}"
        splits = {c["split"] for c in dataset["cases"]}
        assert "dev" in splits, "no dev split (T8 reports dev results)"
        assert "holdout" in splits, "no holdout split (T8 reports holdout results)"

    def test_category_values(self, dataset):
        for c in dataset["cases"]:
            assert c["request"]["category"] in ("Part", "Equipment"), (
                f"{c['id']}: bad category {c['request']['category']!r}"
            )

    def test_should_pass_is_bool(self, dataset):
        for c in dataset["cases"]:
            assert isinstance(c["expected"]["should_pass_floor"], bool), (
                f"{c['id']}: should_pass_floor must be bool"
            )

    def test_rationale_nonempty(self, dataset):
        for c in dataset["cases"]:
            assert c["expected"]["rationale"].strip(), f"{c['id']}: empty rationale"


# ---------------------------------------------------------------------------
# Anchor presence (the cases T8 must get right)
# ---------------------------------------------------------------------------

class TestAnchorsPresent:
    def _by_id(self, dataset, cid):
        for c in dataset["cases"]:
            if c["id"] == cid:
                return c
        return None

    def test_goulds_seal_anchor_present_and_should_pass(self, dataset):
        c = self._by_id(dataset, "goulds_seal_platinum")
        assert c is not None, "missing anchor: goulds_seal_platinum"
        assert c["expected"]["should_pass_floor"] is True

    def test_goulds_pump_anchor_present_and_should_fail(self, dataset):
        c = self._by_id(dataset, "goulds_pump_zoro")
        assert c is not None, "missing anchor: goulds_pump_zoro"
        assert c["expected"]["should_pass_floor"] is False

    def test_skf_bearing_clean_anchor_present_and_should_pass(self, dataset):
        c = self._by_id(dataset, "skf_bearing_clean")
        assert c is not None, "missing no-regression anchor: skf_bearing_clean"
        assert c["expected"]["should_pass_floor"] is True
