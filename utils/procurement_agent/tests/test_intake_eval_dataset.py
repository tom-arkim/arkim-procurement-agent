"""
T8 acceptance tests — schema validation over the labeled eval dataset fixture.
No live calls in the test itself.
"""

from __future__ import annotations

import json
import os

import pytest

from utils.procurement_agent.part_type_registry import KNOWN_PART_TYPES


_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "intake_eval_dataset.json"
)


def _load():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


_VALID_TYPES = set(KNOWN_PART_TYPES) | {"unknown"}
_VALID_SPLITS = {"dev", "holdout"}
_VALID_REGIMES = {"DIRECT", "ANCHORED"}


# ---------------------------------------------------------------------------
# Schema: every example has the required fields with valid values.
# ---------------------------------------------------------------------------

def test_fixture_loads_and_has_examples():
    data = _load()
    examples = data["examples"]
    assert isinstance(examples, list) and len(examples) >= 24, (
        "expected ~24-30 labeled examples"
    )


@pytest.mark.parametrize("idx", range(100))  # bounded; only exercises existing rows
def test_each_example_schema_valid(idx):
    data = _load()
    examples = data["examples"]
    if idx >= len(examples):
        pytest.skip("beyond dataset size")
    ex = examples[idx]
    for key in ("input", "expected_part_type", "expected_component_of",
                "expected_regime", "split"):
        assert key in ex, f"example {idx} missing {key}"
    assert isinstance(ex["input"], str) and ex["input"].strip(), f"example {idx} empty input"
    assert ex["expected_part_type"] in _VALID_TYPES, (
        f"example {idx} bad part_type {ex['expected_part_type']!r}"
    )
    assert ex["split"] in _VALID_SPLITS, f"example {idx} bad split {ex['split']!r}"
    assert ex["expected_regime"] in _VALID_REGIMES, f"example {idx} bad regime"
    # component_of is nullable, but must be a string when present.
    co = ex["expected_component_of"]
    assert co is None or (isinstance(co, str) and co.strip()), (
        f"example {idx} bad component_of {co!r}"
    )


# ---------------------------------------------------------------------------
# Splits non-empty; ~2/3 dev, 1/3 holdout.
# ---------------------------------------------------------------------------

def test_splits_non_empty_and_ratio():
    data = _load()
    examples = data["examples"]
    dev = [e for e in examples if e["split"] == "dev"]
    hold = [e for e in examples if e["split"] == "holdout"]
    assert len(dev) >= 10, "dev split too small"
    assert len(hold) >= 5, "holdout split too small"
    # roughly 2/3 dev, 1/3 holdout — allow some slack.
    ratio = len(dev) / len(examples)
    assert 0.55 <= ratio <= 0.80, f"dev ratio {ratio:.2f} outside the ~2/3 band"


# ---------------------------------------------------------------------------
# Every registry type + unknown represented in BOTH splits where possible.
# ---------------------------------------------------------------------------

def test_all_types_present_overall():
    data = _load()
    types = {e["expected_part_type"] for e in data["examples"]}
    assert _VALID_TYPES.issubset(types), (
        f"missing types: {_VALID_TYPES - types}"
    )


def test_unknown_represented_in_both_splits():
    data = _load()
    for split in ("dev", "holdout"):
        types = {e["expected_part_type"] for e in data["examples"] if e["split"] == split}
        assert "unknown" in types, f"split {split} has no unknown/off-registry example"


# ---------------------------------------------------------------------------
# Sanity: component_of is non-null only for ANCHORED regime (mechanical_seal).
# ---------------------------------------------------------------------------

def test_component_of_only_for_anchored():
    data = _load()
    for ex in data["examples"]:
        if ex["expected_component_of"] is not None:
            assert ex["expected_regime"] == "ANCHORED", (
                f"example {ex['input']!r} has component_of but regime != ANCHORED"
            )
            assert ex["expected_part_type"] == "mechanical_seal", (
                f"example {ex['input']!r} has component_of but type != mechanical_seal"
            )


# ---------------------------------------------------------------------------
# No live calls in this test — prove it by socket probe.
# ---------------------------------------------------------------------------

def test_dataset_test_makes_no_network_call():
    import socket
    orig = socket.socket

    class _Probe:
        def __init__(self, *a, **k):
            raise AssertionError("dataset test opened a socket")

    socket.socket = _Probe  # type: ignore[assignment]
    try:
        _load()  # load + schema check happen via the tests above; this just re-loads
    finally:
        socket.socket = orig  # type: ignore[assignment]
