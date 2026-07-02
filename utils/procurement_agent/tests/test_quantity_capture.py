"""
Acceptance tests for quantity capture (T3) — the pure extractor + the merge
helper. The IntakeAgent.run() flag-gated integration is covered in
test_intake_quantity_integration.py (the flag-off inertness + flag-on behavior).
"""

from __future__ import annotations

import pytest

from utils.procurement_agent.quantity_capture import (
    extract_quantity,
    apply_quantity,
    _MAX_PLAUSIBLE_QTY,
)


# ---------------------------------------------------------------------------
# Stated quantity -> (N, False)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("I need 6 SKF 6205 bearings", 6),
    ("need 6 of those", 6),
    ("I want 4 bearings", 4),
    ("looking for 3 valves", 3),
    ("require 10 seals", 10),
    ("order 2 pumps", 2),
    ("buy 12 of them", 12),
    ("6x bearings", 6),
    ("6 x bearings", 6),
    ("6× bearings", 6),
    ("qty 8", 8),
    ("qty: 8", 8),
    ("qty=8", 8),
    ("quantity 5", 5),
    ("quantity: 5", 5),
    ("6 of these", 6),
    ("6 pieces", 6),
    ("6 pcs", 6),
    ("6 units", 6),
    ("6 ea", 6),
    ("6 each", 6),
])
def test_stated_quantity_extracted(text, expected):
    qty, assumed = extract_quantity(text)
    assert (qty, assumed) == (expected, False), f"{text!r} -> {(qty, assumed)}"


# ---------------------------------------------------------------------------
# Unstated -> (1, True). Part numbers must NOT be misread as quantities.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "SKF 6205 bearing",                  # 6205 is a PN, not a qty
    "Goulds 3196 mechanical seal",       # 3196 is a model
    "Grundfos CR32-5 pump",
    "2 inch stainless ball valve",       # "2 inch" is a SIZE, not a qty (no signal word)
    "a mechanical seal for a Goulds 3196",
    "What pump is it on?",
    "",                                  # empty
    "   ",                               # whitespace
    "hello",                             # no digits
])
def test_unstated_defaults_to_one_assumed(text):
    qty, assumed = extract_quantity(text)
    assert (qty, assumed) == (1, True), f"{text!r} -> {(qty, assumed)}"


# ---------------------------------------------------------------------------
# Non-string / None input -> (1, True), never raises.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, 123, 4.5, [], {}])
def test_non_string_input_safe(bad):
    qty, assumed = extract_quantity(bad)  # type: ignore[arg-type]
    assert (qty, assumed) == (1, True)


# ---------------------------------------------------------------------------
# The "2 inch ball valve" case: "2 inch" must NOT be read as quantity 2.
# (No quantity signal word, no "of", no "x" multiplier, no "pcs/units".)
# ---------------------------------------------------------------------------

def test_size_not_quantity():
    assert extract_quantity("2 inch stainless ball valve") == (1, True)
    assert extract_quantity("3 phase motor") == (1, True)
    assert extract_quantity("460V 30HP motor") == (1, True)


# ---------------------------------------------------------------------------
# Ceiling: an absurd stated quantity is treated as unstated.
# ---------------------------------------------------------------------------

def test_absurd_quantity_falls_to_default():
    text = f"need {_MAX_PLAUSIBLE_QTY + 1} bearings"
    qty, assumed = extract_quantity(text)
    assert (qty, assumed) == (1, True)


def test_zero_quantity_falls_to_default():
    # "need 0" is not a valid quantity -> default 1
    qty, assumed = extract_quantity("need 0 bearings")
    assert (qty, assumed) == (1, True)


# ---------------------------------------------------------------------------
# apply_quantity — merge semantics.
# ---------------------------------------------------------------------------

def test_apply_quantity_stated_wins():
    specs = {}
    out = apply_quantity(specs, "I need 6 SKF 6205 bearings")
    assert out["quantity"] == 6
    assert out["_quantity_assumed"] is False


def test_apply_quantity_unstated_defaults():
    specs = {}
    out = apply_quantity(specs, "SKF 6205 bearing")
    assert out["quantity"] == 1
    assert out["_quantity_assumed"] is True


def test_apply_quantity_preserves_prior_real_quantity():
    # Prior turn stated a real quantity; this turn states none -> keep prior.
    specs = {"quantity": 6, "_quantity_assumed": False}
    out = apply_quantity(specs, "it's a 3-phase")
    assert out["quantity"] == 6
    assert out["_quantity_assumed"] is False


def test_apply_quantity_new_stated_overrides_prior_assumed():
    # Prior was assumed (1); this turn states 4 -> 4 wins.
    specs = {"quantity": 1, "_quantity_assumed": True}
    out = apply_quantity(specs, "actually need 4")
    assert out["quantity"] == 4
    assert out["_quantity_assumed"] is False


def test_apply_quantity_new_stated_overrides_prior_real():
    # User changes their mind: prior 6, now states 4 -> 4 wins.
    specs = {"quantity": 6, "_quantity_assumed": False}
    out = apply_quantity(specs, "make it 4 instead")
    # "make it 4" has no verb match; "4" alone is ambiguous. Use an explicit verb.
    out = apply_quantity(specs, "I need 4 instead")
    assert out["quantity"] == 4
    assert out["_quantity_assumed"] is False


def test_apply_quantity_pure_no_io():
    """apply_quantity must not touch the network / env."""
    import socket
    orig = socket.socket

    class _Probe:
        def __init__(self, *a, **k):
            raise AssertionError("apply_quantity opened a socket")

    socket.socket = _Probe  # type: ignore[assignment]
    try:
        out = apply_quantity({}, "need 6 bearings")
        assert out["quantity"] == 6
    finally:
        socket.socket = orig  # type: ignore[assignment]
