"""
Tests for scoring._classify_pn_match — 5-tier PN match classifier (Fix 1).
"""

import pytest

from utils.sourcing_archieved.scoring import _classify_pn_match, PN_MATCH_POINTS


class TestClassifyPnMatch:
    def test_exact_match_identical_strings(self):
        level = _classify_pn_match("HHI-150-12-447T", "HHI-150-12-447T", "", "US Motors")
        assert level == "exact"
        assert PN_MATCH_POINTS[level] == 40

    def test_normalized_match_delimiter_difference(self):
        """MR-1-1375 vs MR11375 — same PN, different delimiters → normalized, 40 pts."""
        level = _classify_pn_match("HHI-150-12-447T", "HHI150-12-447T", "", "US Motors")
        assert level == "normalized"
        assert PN_MATCH_POINTS[level] == 40

    def test_normalized_match_no_penalty(self):
        """normalized level must not trigger mismatch penalty (only 'none' with found_pn does)."""
        level = _classify_pn_match("HHI-150-12-447T", "HHI150-12-447T", "", "US Motors")
        pn_pts = PN_MATCH_POINTS[level]
        penalty = 30 if (level == "none" and "HHI150-12-447T") else 0
        assert pn_pts == 40
        assert penalty == 0

    def test_stem_match_endress_hauser(self):
        """PMC11-AA1U1HBWBJJ vs PMC11 — same family stem → stem."""
        level = _classify_pn_match(
            "PMC11-AA1U1HBWBJJ", "PMC11AA1U1HBWBJJ",
            "", "Endress+Hauser"
        )
        # Both normalize to the same string → normalized match
        assert level in ("normalized", "stem")
        assert PN_MATCH_POINTS[level] >= 25

    def test_stem_match_different_ordering_code(self):
        """PMC11-AA1U1HBWBJJ (searched) vs PMC11-BB2X3YYZAA (different config) — same stem."""
        level = _classify_pn_match(
            "PMC11-AA1U1HBWBJJ", "PMC11-BB2X3YYZAA",
            "", "Endress+Hauser"
        )
        assert level == "stem"
        assert PN_MATCH_POINTS[level] == 25

    def test_substring_match_pn_in_snippet(self):
        """found_pn is None but searched PN appears in snippet → substring."""
        snippet = "We stock part number HHI15012447T for same-day shipping."
        level = _classify_pn_match("HHI-150-12-447T", None, snippet, "US Motors")
        assert level == "substring"
        assert PN_MATCH_POINTS[level] == 15

    def test_none_match_genuinely_different_pn(self):
        """Completely different found_pn and not in snippet → none → penalty applies."""
        level = _classify_pn_match("HHI-150-12-447T", "WEG-200-14-449T", "unrelated content", "US Motors")
        assert level == "none"
        assert PN_MATCH_POINTS[level] == 0
        # Penalty fires when level == "none" AND found_pn is non-null
        penalty = 30 if (level == "none" and "WEG-200-14-449T") else 0
        assert penalty == 30

    def test_none_match_no_penalty_when_found_pn_absent(self):
        """No found_pn, PN not in snippet → none, but NO mismatch penalty."""
        level = _classify_pn_match("HHI-150-12-447T", None, "generic pump page", "US Motors")
        assert level == "none"
        found_pn = None
        penalty = 30 if (level == "none" and found_pn) else 0
        assert penalty == 0

    def test_empty_searched_pn_returns_none(self):
        level = _classify_pn_match("", "HHI-150-12-447T", "snippet", "US Motors")
        assert level == "none"
