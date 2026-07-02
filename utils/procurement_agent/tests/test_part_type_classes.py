"""
Tests for utils/sourcing_archieved/part_type_classes.py — MRO noun-class
dictionary + classifier (data-only module feeding the SCORING_V2 TypeGate).
"""

import pytest

from utils.sourcing_archieved.part_type_classes import (
    classify_noun_class,
    classify_noun_class_from_url,
    classify_result_noun_class,
    get_noun_class,
    _classes,
)


# ---------------------------------------------------------------------------
# Dictionary loads + structure
# ---------------------------------------------------------------------------

class TestDictionary:
    def test_dictionary_loads(self):
        classes = _classes()
        assert len(classes) >= 10  # SEAL PUMP BEARING GASKET VALVE MOTOR + extras

    def test_every_class_has_canonical_and_synonyms(self):
        for nc in _classes():
            assert nc.canonical, "missing canonical"
            assert nc.synonyms, f"{nc.canonical} missing synonyms"
            # canonical always self-references as a (trailing) synonym so the bare
            # class word is detectable
            assert any(nc.canonical.lower() in syn for syn in nc.synonyms), (
                f"{nc.canonical} has no bare-word synonym"
            )

    @pytest.mark.parametrize("label", [
        "SEAL", "PUMP", "BEARING", "GASKET", "VALVE", "MOTOR",
    ])
    def test_required_classes_present(self, label):
        assert get_noun_class(label) is not None, f"{label} class missing"

    def test_get_noun_class_case_insensitive(self):
        assert get_noun_class("seal").canonical == "SEAL"
        assert get_noun_class("Pump").canonical == "PUMP"

    def test_get_noun_class_unknown_returns_none(self):
        assert get_noun_class("WIDGET") is None
        assert get_noun_class("") is None
        assert get_noun_class(None) is None

    def test_slug_tokens_present_for_each_class(self):
        # URL-slug matching is the highest-leverage signal — every class must
        # carry at least one slug token.
        for nc in _classes():
            assert nc.slug_tokens, f"{nc.canonical} missing slug tokens"


# ---------------------------------------------------------------------------
# classify_noun_class — free-text classification
# ---------------------------------------------------------------------------

class TestClassifyNounClass:
    def test_mechanical_seal_classifies_seal(self):
        assert classify_noun_class("mechanical seal") == "SEAL"

    def test_centrifugal_pump_classifies_pump(self):
        assert classify_noun_class("centrifugal pump") == "PUMP"

    def test_ball_bearing_classifies_bearing(self):
        assert classify_noun_class("ball bearing") == "BEARING"

    def test_ball_valve_classifies_valve(self):
        assert classify_noun_class("ball valve") == "VALVE"

    def test_vfd_classifies_drive(self):
        # VFD is a DRIVE synonym — the bare acronym must classify.
        assert classify_noun_class("VFD") == "DRIVE"

    def test_gearmotor_classifies_motor(self):
        assert classify_noun_class("gearmotor") == "MOTOR"

    def test_longest_synonym_wins_over_substring(self):
        # "mechanical seal" must resolve to SEAL, not get shadowed by a shorter
        # synonym inside another phrase.
        assert classify_noun_class("Goulds 3196 mechanical seal ST-1.375") == "SEAL"

    def test_multi_word_takes_priority(self):
        # "variable frequency drive" is a DRIVE synonym; "drive" alone is too,
        # but the longer phrase is the more specific verdict and must win.
        assert classify_noun_class("variable frequency drive") == "DRIVE"

    def test_case_insensitive(self):
        assert classify_noun_class("MECHANICAL SEAL") == "SEAL"
        assert classify_noun_class("Centrifugal Pump") == "PUMP"

    def test_unknown_text_returns_none(self):
        assert classify_noun_class("industrial maintenance platform") is None
        assert classify_noun_class("catalog") is None

    def test_empty_returns_none(self):
        assert classify_noun_class("") is None
        assert classify_noun_class(None) is None
        assert classify_noun_class("   ") is None

    def test_synonym_inside_longer_text_still_classifies(self):
        # Free text like a result title / detected_type typically wraps the noun
        # in surrounding marketing copy.
        assert classify_noun_class("Goulds 3196 ST mechanical seal replacement") == "SEAL"
        assert classify_noun_class("Self-priming centrifugal pump 5HP") == "PUMP"


# ---------------------------------------------------------------------------
# classify_noun_class_from_url — URL-slug classification
# ---------------------------------------------------------------------------

class TestClassifyFromUrl:
    def test_seal_slug_classifies_seal(self):
        assert classify_noun_class_from_url(
            "https://platinumperformanceproducts.com/mechanical-seals/goulds/3196-st"
        ) == "SEAL"

    def test_pump_slug_classifies_pump(self):
        assert classify_noun_class_from_url(
            "https://zoro.com/pump/centrifugal/goulds-3196/i/"
        ) == "PUMP"

    def test_bearing_slug_classifies_bearing(self):
        assert classify_noun_class_from_url(
            "https://mrosupply.com/bearings/skf-6205-2rs/"
        ) == "BEARING"

    def test_valve_slug_classifies_valve(self):
        assert classify_noun_class_from_url(
            "https://valvesupply.com/valves/ball-valves/2-inch/"
        ) == "VALVE"

    def test_domain_does_not_classify_the_page(self):
        # pumpcatalog.com contains 'pump' but the PATH has no pump segment —
        # the domain must not classify a seal page as a pump (mirror of the
        # _is_collection_url path-only discipline in scoring.py).
        assert classify_noun_class_from_url(
            "https://pumpcatalog.com/mechanical-seals/goulds-type-21"
        ) == "SEAL"

    def test_no_category_segment_returns_none(self):
        assert classify_noun_class_from_url(
            "https://example.com/products/sku-12345"
        ) is None

    def test_empty_url_returns_none(self):
        assert classify_noun_class_from_url("") is None
        assert classify_noun_class_from_url(None) is None

    def test_query_only_url_returns_none(self):
        assert classify_noun_class_from_url(
            "https://example.com/search?q=goulds+3196"
        ) is None


# ---------------------------------------------------------------------------
# classify_result_noun_class — combined title + url verdict
# ---------------------------------------------------------------------------

class TestClassifyResultNounClass:
    def test_url_wins_over_title_when_disagree(self):
        # The vendor's own category breadcrumb is more authoritative than the
        # marketing title. URL says PUMP, title says seal -> PUMP.
        url = "https://zoro.com/pump/centrifugal/goulds-3196/i/"
        title = "Goulds 3196 replacement seal kit"
        assert classify_result_noun_class(title, url) == "PUMP"

    def test_title_used_when_url_undetectable(self):
        url = "https://vendor.com/products/sku-999"
        title = "Goulds 3196 mechanical seal, Type 1, 1.375 inch"
        assert classify_result_noun_class(title, url) == "SEAL"

    def test_both_undetectable_returns_none(self):
        url = "https://vendor.com/products/sku-999"
        title = "Goulds 3196 replacement part"
        assert classify_result_noun_class(title, url) is None

    def test_both_agree_returns_class(self):
        url = "https://vendor.com/mechanical-seals/goulds/3196"
        title = "Goulds 3196 mechanical seal"
        assert classify_result_noun_class(title, url) == "SEAL"
