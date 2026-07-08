"""
Tests for IntakeAgent — multimodal extraction, sufficiency assessment, clarification.

All LLM calls (requests.post) are mocked so tests run without API keys.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from utils.models import SourcingRun
from utils.procurement_agent.agents.intake_agent import (
    IntakeAgent,
    CATEGORY_REQUIRED_FIELDS,
    SUFFICIENCY_THRESHOLD,
    _detect_media_type,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_run(specs: dict | None = None) -> SourcingRun:
    return SourcingRun(asset_specs_json=specs)


def _mock_anthropic_response(payload: dict) -> MagicMock:
    """Create a mock requests.Response returning the given JSON payload as Claude output."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "content": [{"text": json.dumps(payload)}]
    }
    return mock_resp


def _extracted(overrides: dict | None = None) -> dict:
    """Return a complete extraction payload with high confidence (sufficient by default)."""
    base = {
        "manufacturer":            "Grundfos",
        "model":                   "CR32-5",
        "part_number":             "96516888",
        "voltage":                 "460V",
        "category":                "Equipment",
        "hp":                      "15",
        "serial_number":           None,
        "description":             "Vertical multistage centrifugal pump",
        "gpm":                     "32",
        "psi":                     None,
        "frame":                   None,
        "phase":                   "3-phase",
        "detected_type":           "centrifugal pump",
        "rpm":                     None,
        "shaft_size":              None,
        "bore_diameter":           None,
        "seal_face_size":          None,
        "connection_size":         None,
        "material_spec":           None,
        "use_case":                None,
        "manufacturer_confidence": 92,
        "part_id_confidence":      85,
        "confidence_reasoning":    "Manufacturer name and model explicitly stated",
    }
    if overrides:
        base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Extraction — text
# ---------------------------------------------------------------------------

class TestTextExtraction:
    def test_extracts_manufacturer_and_model(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(_make_run(), {"text": "Need a Grundfos CR32-5 pump", "images": [], "force_proceed": False})

        assert result["asset_specs"]["manufacturer"] == "Grundfos"
        assert result["asset_specs"]["model"] == "CR32-5"

    def test_sufficient_when_both_confidences_high(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(_make_run(), {"text": "Grundfos CR32-5", "images": [], "force_proceed": False})

        assert result["sufficient"] is True
        assert result["follow_up_question"] is None

    def test_sends_prior_specs_as_context(self):
        agent   = IntakeAgent(anthropic_api_key="test-key")
        prior   = {"manufacturer": "Grundfos", "model": "CR32-5", "voltage": "460V",
                   "manufacturer_confidence": 92, "part_id_confidence": 70}
        run     = _make_run(specs=prior)

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted({"hp": "15"}))
            result = agent.run(run, {"text": "it's 15 HP", "images": [], "force_proceed": False})

        # Verify prior specs present in the prompt
        call_body = mock_post.call_args[1]["json"]
        user_content = call_body["messages"][0]["content"]
        assert "Grundfos" in user_content

        # Merged specs should include hp from this turn
        assert result["asset_specs"]["hp"] == "15"

    def test_merges_prior_specs_new_values_win(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"manufacturer": "Unknown", "model": "OLD", "manufacturer_confidence": 20, "part_id_confidence": 20}
        run   = _make_run(specs=prior)

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(run, {"text": "Actually it's Grundfos", "images": [], "force_proceed": False})

        assert result["asset_specs"]["manufacturer"] == "Grundfos"
        assert result["asset_specs"]["model"] == "CR32-5"


# ---------------------------------------------------------------------------
# Extraction — multimodal
# ---------------------------------------------------------------------------

class TestMultimodalExtraction:
    def test_sends_image_content_when_images_provided(self):
        agent      = IntakeAgent(anthropic_api_key="test-key")
        fake_image = b"\xff\xd8\xff\xe0" + b"\x00" * 64  # minimal fake JPEG

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "see nameplate", "images": [fake_image], "force_proceed": False})

        call_body = mock_post.call_args[1]["json"]
        content   = call_body["messages"][0]["content"]
        assert isinstance(content, list), "Multimodal call must use list content"
        types = [c["type"] for c in content]
        assert "image" in types
        assert "text"  in types

    def test_image_type_is_base64(self):
        agent      = IntakeAgent(anthropic_api_key="test-key")
        fake_image = b"\xff\xd8\xff\xe0" + b"\x00" * 64

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "", "images": [fake_image], "force_proceed": False})

        call_body = mock_post.call_args[1]["json"]
        img_block = next(c for c in call_body["messages"][0]["content"] if c["type"] == "image")
        assert img_block["source"]["type"] == "base64"

    def test_jpeg_media_type_sent_for_jpeg_bytes(self):
        agent      = IntakeAgent(anthropic_api_key="test-key")
        fake_jpeg  = b"\xff\xd8\xff\xe0" + b"\x00" * 64

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "", "images": [fake_jpeg], "force_proceed": False})

        img_block = next(
            c for c in mock_post.call_args[1]["json"]["messages"][0]["content"]
            if c["type"] == "image"
        )
        assert img_block["source"]["media_type"] == "image/jpeg"

    def test_png_media_type_sent_for_png_bytes(self):
        agent    = IntakeAgent(anthropic_api_key="test-key")
        fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "", "images": [fake_png], "force_proceed": False})

        img_block = next(
            c for c in mock_post.call_args[1]["json"]["messages"][0]["content"]
            if c["type"] == "image"
        )
        assert img_block["source"]["media_type"] == "image/png"

    def test_webp_media_type_sent_for_webp_bytes(self):
        agent     = IntakeAgent(anthropic_api_key="test-key")
        fake_webp = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "", "images": [fake_webp], "force_proceed": False})

        img_block = next(
            c for c in mock_post.call_args[1]["json"]["messages"][0]["content"]
            if c["type"] == "image"
        )
        assert img_block["source"]["media_type"] == "image/webp"

    def test_caps_at_four_images(self):
        agent  = IntakeAgent(anthropic_api_key="test-key")
        images = [b"\xff\xd8\xff" + b"\x00" * 10] * 6  # 6 images

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(_make_run(), {"text": "", "images": images, "force_proceed": False})

        call_body  = mock_post.call_args[1]["json"]
        img_blocks = [c for c in call_body["messages"][0]["content"] if c["type"] == "image"]
        assert len(img_blocks) == 4


# ---------------------------------------------------------------------------
# Sufficiency assessment
# ---------------------------------------------------------------------------

class TestSufficiency:
    def test_proceeds_with_caveat_when_only_manufacturer_confidence_low(self):
        """Fix 3: mfg<70 but pid≥80 → proceed with caveat, not blocked."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer_confidence": 40, "manufacturer": None})
            )
            result = agent.run(_make_run(), {"text": "unknown device", "images": [], "force_proceed": False})

        assert result["sufficient"] is True
        assert result["manufacturer_caveat"] is not None

    def test_fails_when_both_confidences_low(self):
        """Both mfg<70 and pid<80 → blocked."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer_confidence": 40, "part_id_confidence": 45, "manufacturer": None})
            )
            result = agent.run(_make_run(), {"text": "unknown device", "images": [], "force_proceed": False})

        assert result["sufficient"] is False
        assert result["follow_up_question"] is not None

    def test_fails_when_part_id_confidence_low(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"part_id_confidence": 55})
            )
            result = agent.run(_make_run(), {"text": "some part", "images": [], "force_proceed": False})

        assert result["sufficient"] is False

    def test_fails_motor_missing_hp(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        motor_specs = _extracted({
            "manufacturer":     None,   # no identity -> genuine spec-described path
            "model":            None,   # (the family-level model-no-PN case is covered by
                                        #  the T4 variant-disambiguation tests; this test
                                        #  guards the no-identity spec-described path the
                                        #  variant gate must leave UNAFFECTED)
            "detected_type":    "induction motor",
            "category":         "Equipment",
            "part_number":      None,   # spec-based (no PN) -> the dimension requirement applies
            "hp":               None,
            "frame":            "326T",
            "rpm":              "1800",
            "manufacturer_confidence": 92,
            "part_id_confidence":      88,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(motor_specs)
            result = agent.run(_make_run(), {"text": "US Motors motor", "images": [], "force_proceed": False})

        assert result["sufficient"] is False
        # Missing field should be hp
        assert result["confidence_summary"]["missing_field"] == "hp"

    def test_fails_motor_missing_frame(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        motor_specs = _extracted({
            "detected_type":    "induction motor",
            "category":         "Equipment",
            "part_number":      None,   # spec-based (no PN) -> the dimension requirement applies
            "hp":               "30",
            "frame":            None,
            "rpm":              "1800",
            "voltage":          "460V",
            "manufacturer_confidence": 92,
            "part_id_confidence":      88,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(motor_specs)
            result = agent.run(_make_run(), {"text": "30HP motor", "images": [], "force_proceed": False})

        assert result["sufficient"] is False

    def test_fails_mechanical_seal_missing_shaft_size(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        seal_specs = _extracted({
            "detected_type":    "mechanical seal",
            "category":         "Part",
            "part_number":      None,   # spec-based (no PN) -> the dimension requirement applies
            "shaft_size":       None,
            "material_spec":    "Carbon/Silicon Carbide",
            "manufacturer_confidence": 85,
            "part_id_confidence":      80,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(seal_specs)
            result = agent.run(_make_run(), {"text": "mechanical seal", "images": [], "force_proceed": False})

        assert result["sufficient"] is False

    def test_passes_when_all_required_fields_present(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        motor_specs = _extracted({
            "detected_type":    "induction motor",
            "category":         "Equipment",
            "hp":               "30",
            "voltage":          "460V",
            "frame":            "326T",
            "rpm":              "1800",
            "manufacturer_confidence": 92,
            "part_id_confidence":      88,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(motor_specs)
            result = agent.run(_make_run(), {"text": "30HP US Motors motor 326T frame", "images": [], "force_proceed": False})

        assert result["sufficient"] is True


# ---------------------------------------------------------------------------
# Force proceed
# ---------------------------------------------------------------------------

class TestForceProceed:
    def test_bypasses_sufficiency_gate(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        low_conf = _extracted({"manufacturer_confidence": 15, "part_id_confidence": 10})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(low_conf)
            result = agent.run(_make_run(), {"text": "I need a part", "images": [], "force_proceed": True})

        assert result["sufficient"] is True
        assert result["confidence_summary"].get("forced") is True

    def test_still_extracts_specs_on_force_proceed(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted({"manufacturer_confidence": 10}))
            result = agent.run(_make_run(), {"text": "Grundfos pump", "images": [], "force_proceed": True})

        assert result["asset_specs"]["manufacturer"] == "Grundfos"


# ---------------------------------------------------------------------------
# No API key (fallback)
# ---------------------------------------------------------------------------

class TestNoApiKey:
    def test_returns_zero_confidence_without_api_key(self):
        agent  = IntakeAgent(anthropic_api_key=None)
        result = agent.run(_make_run(), {"text": "Allen Bradley VFD", "images": [], "force_proceed": False})

        assert result["manufacturer_confidence"] == 0
        assert result["part_id_confidence"] == 0
        assert result["sufficient"] is False

    def test_follow_up_falls_back_to_default_question(self):
        agent  = IntakeAgent(anthropic_api_key=None)
        result = agent.run(_make_run(), {"text": "some equipment", "images": [], "force_proceed": False})

        # With no API key, a default question should still be returned
        assert result["follow_up_question"] is not None
        assert len(result["follow_up_question"]) > 10

    def test_does_not_call_requests_without_api_key(self):
        agent = IntakeAgent(anthropic_api_key=None)
        with patch("requests.post") as mock_post:
            agent.run(_make_run(), {"text": "anything", "images": [], "force_proceed": False})
        mock_post.assert_not_called()


# ---------------------------------------------------------------------------
# Clarification generation
# ---------------------------------------------------------------------------

class TestClarification:
    def test_generates_question_when_not_sufficient(self):
        """Both confidences low → blocked_need_either → clarification question generated."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        haiku_response = MagicMock()
        haiku_response.raise_for_status = MagicMock()
        haiku_response.json.return_value = {"content": [{"text": "What is the HP rating?"}]}

        # mfg=30, pid=55 → blocked_need_either (both < threshold and pid < 80)
        low_conf = _extracted({"manufacturer_confidence": 30, "part_id_confidence": 55})
        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_anthropic_response(low_conf),  # extraction call
                haiku_response,                        # clarification call
            ]
            result = agent.run(_make_run(), {"text": "unknown part", "images": [], "force_proceed": False})

        assert result["follow_up_question"] == "What is the HP rating?"

    def test_fallback_question_on_clarification_error(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        # mfg=30, pid=55 → blocked, triggers clarification which then errors
        low_conf = _extracted({"manufacturer_confidence": 30, "part_id_confidence": 55})
        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_anthropic_response(low_conf),
                Exception("network error"),
            ]
            result = agent.run(_make_run(), {"text": "vague request", "images": [], "force_proceed": False})

        # Should fall back to default question, not raise
        assert result["follow_up_question"] is not None


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------

class TestOutputContract:
    def test_all_required_keys_present(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(_make_run(), {"text": "pump", "images": [], "force_proceed": False})

        required = {"asset_specs", "manufacturer_confidence", "part_id_confidence",
                    "sufficient", "follow_up_question", "confidence_summary"}
        assert required.issubset(result.keys())

    def test_confidence_summary_contains_reasoning(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(_make_run(), {"text": "pump", "images": [], "force_proceed": False})

        assert "reasoning" in result["confidence_summary"]


# ---------------------------------------------------------------------------
# Fix 1 — Units-based classification override
# ---------------------------------------------------------------------------

class TestUnitsClassification:
    def test_motor_units_hp_frame_override_pump(self):
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "centrifugal pump",
            "category":      "Equipment",
            "manufacturer":  "Crown",
            "hp":            "5",
            "frame":         "184T",
            "rpm":           None,
            "voltage":       "460V",
        }
        new_type, new_cat, override = classify_by_units(specs)
        assert override is True
        assert "motor" in new_type.lower()
        assert new_cat == "Equipment"

    def test_motor_units_hp_rpm_override_non_motor(self):
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "industrial device",
            "hp":            "10",
            "rpm":           "1800",
        }
        new_type, new_cat, override = classify_by_units(specs)
        assert override is True
        assert "motor" in new_type.lower()

    def test_pump_units_gpm_psi_fire(self):
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": None,
            "gpm":           "32",
            "psi":           "150",
            "manufacturer":  "Grundfos",
        }
        new_type, new_cat, override = classify_by_units(specs)
        assert override is True
        assert "pump" in new_type.lower()
        assert new_cat == "Equipment"

    def test_no_units_no_override(self):
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {"detected_type": "centrifugal pump", "manufacturer": "Grundfos"}
        _, _, override = classify_by_units(specs)
        assert override is False

    def test_motor_already_classified_no_override(self):
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "induction motor",
            "hp":            "5",
            "frame":         "184T",
        }
        _, _, override = classify_by_units(specs)
        assert override is False

    def test_classify_by_units_wired_into_run(self):
        """When VLM misclassifies as pump but motor units present, run() overrides."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        pump_with_motor_units = _extracted({
            "detected_type": "centrifugal pump",
            "category":      "Equipment",
            "hp":            "5",
            "frame":         "184T",
            "rpm":           "1800",
            "manufacturer_confidence": 85,
            "part_id_confidence":      80,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(pump_with_motor_units)
            result = agent.run(_make_run(), {"text": "Crown Triton 5HP motor", "images": [], "force_proceed": False})

        assert "motor" in result["asset_specs"].get("detected_type", "").lower()

    # Fix 3 — loosened motor classification (hp+frame sufficient; 3-field → more specific)

    def test_motor_hp_frame_no_rpm_classifies_as_electric_motor(self):
        """hp + frame alone (no RPM) → 'Electric Motor'."""
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "industrial equipment",
            "hp":            "150",
            "frame":         "447T",
            "rpm":           None,
        }
        new_type, new_cat, override = classify_by_units(specs)
        assert override is True
        assert new_type == "Electric Motor"
        assert new_cat == "Equipment"

    def test_motor_hp_rpm_frame_classifies_as_three_phase(self):
        """hp + rpm + frame → '3-Phase Electric Motor' (more specific than 2-field rule)."""
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "industrial equipment",
            "hp":            "150",
            "rpm":           "1185",
            "frame":         "447T",
        }
        new_type, new_cat, override = classify_by_units(specs)
        assert override is True
        assert new_type == "3-Phase Electric Motor"

    def test_three_phase_rule_beats_two_field_rule(self):
        """When all three motor signals present, 3-Phase label wins over Electric Motor."""
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "unknown device",
            "hp":            "30",
            "rpm":           "1800",
            "frame":         "326T",
        }
        new_type, _, override = classify_by_units(specs)
        assert override is True
        assert "3-phase" in new_type.lower()

    def test_motor_classifies_regardless_of_brand(self):
        """hp + frame triggers motor classification independent of manufacturer name."""
        from utils.procurement_agent.agents.intake_agent import classify_by_units
        specs = {
            "detected_type": "industrial device",
            "manufacturer":  "Hyundai",
            "hp":            "30",
            "frame":         "256T",
        }
        new_type, _, override = classify_by_units(specs)
        assert override is True
        assert "motor" in new_type.lower()


# ---------------------------------------------------------------------------
# Fix 3 — Asymmetric stop condition
# ---------------------------------------------------------------------------

class TestAsymmetricStopCondition:
    def test_proceed_full_confidence_both_high(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "centrifugal pump", "manufacturer": "Grundfos"}
        state, missing, caveat = assess_proceed_state(specs, 92, 88)
        assert state == "proceed_full_confidence"
        assert caveat is None
        assert missing is None

    def test_proceed_with_manufacturer_caveat_pmc11_case(self):
        """Endress+Hauser PMC11: mfg ~45, pid ~85 — should proceed with caveat."""
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "pressure sensor", "manufacturer": "Endress+Hauser", "model": "PMC11"}
        state, missing, caveat = assess_proceed_state(specs, 45, 85)
        assert state == "proceed_with_manufacturer_caveat"
        assert caveat is not None
        assert "manufacturer" in caveat.lower()

    def test_proceed_with_manufacturer_caveat_pid_at_80(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "bearing"}
        state, missing, caveat = assess_proceed_state(specs, 60, 80)
        assert state == "proceed_with_manufacturer_caveat"

    def test_blocked_need_part_id(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "unknown device"}
        state, missing, caveat = assess_proceed_state(specs, 90, 55)
        assert state == "blocked_need_part_id"
        assert missing == "part_type"

    def test_blocked_need_either_both_low(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {}
        state, missing, caveat = assess_proceed_state(specs, 30, 40)
        assert state == "blocked_need_either"
        assert missing == "manufacturer"

    def test_blocked_when_mfg_low_pid_between_70_and_79(self):
        """pid=75 is >= threshold but < 80 — should still block."""
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "pump"}
        state, missing, caveat = assess_proceed_state(specs, 50, 75)
        assert state == "blocked_need_either"

    def test_needs_clarification_missing_motor_hp(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {
            "detected_type": "induction motor",
            "hp":            None,
            "voltage":       "460V",
            "frame":         None,
            "rpm":           None,
        }
        state, missing, caveat = assess_proceed_state(specs, 90, 88)
        assert state == "needs_clarification"
        assert missing is not None

    def test_sufficient_true_when_caveat_state(self):
        """proceed_with_manufacturer_caveat → sufficient=True in the full run() output."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        low_mfg_high_pid = _extracted({
            "manufacturer_confidence": 45,
            "part_id_confidence":      85,
            "detected_type":           "pressure sensor",
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(low_mfg_high_pid)
            result = agent.run(_make_run(), {"text": "XY99-Z sensor", "images": [], "force_proceed": False})

        assert result["sufficient"] is True
        assert result["manufacturer_caveat"] is not None

    def test_manufacturer_caveat_none_when_full_confidence(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            result = agent.run(_make_run(), {"text": "Grundfos CR32-5", "images": [], "force_proceed": False})

        assert result["manufacturer_caveat"] is None


# ---------------------------------------------------------------------------
# Clarification loop bug fix — monotonic confidence + prior question passthrough
# ---------------------------------------------------------------------------

class TestClarificationLoopFix:
    def test_monotonic_confidence_company_name_answer_proceeds(self):
        """PMC11 → asks manufacturer → 'Endress Hauser' → must not re-ask same question.

        Simulates turn 2: prior turn established part_id_confidence=72 (PMC11 recognised).
        LLM sees "Endress Hauser" and scores mfg=90, part=30 (company name ≠ part info).
        Without Fix A: part_conf=30 < 70 → blocked_need_part_id.
        With Fix A: part_conf=max(72, 30)=72 ≥ 70, mfg_conf=max(0, 90)=90 ≥ 70 → proceeds.
        """
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {
            "detected_type":           "pressure sensor",
            "model":                   "PMC11",
            "psi":                     "150",
            "manufacturer_confidence": 0,
            "part_id_confidence":      72,
        }
        run = _make_run(specs=prior)
        turn2_response = _extracted({
            "manufacturer":            "Endress+Hauser",
            "detected_type":           "pressure sensor",
            "model":                   "PMC11",
            "psi":                     "150",
            "manufacturer_confidence": 90,
            "part_id_confidence":      30,  # company name gives no part info
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn2_response)
            result = agent.run(run, {
                "text":           "Endress Hauser",
                "images":         [],
                "force_proceed":  False,
                "prior_question": "Who is the manufacturer of this equipment?",
            })

        # Fix A: part_conf = max(72, 30) = 72 (floor preserved from prior turn).
        # mfg_conf = 92: per-object inference recognizes model "PMC11" -> Endress+Hauser at the
        # prefix confidence (92), which overrides the LLM's 90 (split-first change; the old
        # whole-input hint scanned only the turn-2 text "Endress Hauser" and missed the model PN).
        # Both >= 70 either way -> proceed_full_confidence; the intent (proceeds, no re-ask) holds.
        assert result["manufacturer_confidence"] == 92
        assert result["part_id_confidence"] == 72
        assert result["sufficient"] is True
        assert result["follow_up_question"] is None

    def test_confidence_floor_prevents_regression(self):
        """Prior part_id_confidence=65; clarification answer gives 30 — floor holds at 65."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"manufacturer_confidence": 80, "part_id_confidence": 65}
        run   = _make_run(specs=prior)
        turn2_response = _extracted({
            "manufacturer_confidence": 85,
            "part_id_confidence":      30,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn2_response)
            result = agent.run(run, {
                "text":          "Endress Hauser",
                "images":        [],
                "force_proceed": False,
            })

        assert result["part_id_confidence"] == 65
        assert result["manufacturer_confidence"] == 85

    def test_prior_question_included_in_llm_prompt(self):
        """When prior_question is set, LLM prompt contains 'Agent asked:' prefix."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"manufacturer_confidence": 0, "part_id_confidence": 65, "model": "PMC11"}
        run   = _make_run(specs=prior)

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(run, {
                "text":           "Endress Hauser",
                "images":         [],
                "force_proceed":  False,
                "prior_question": "Who is the manufacturer?",
            })

        call_body    = mock_post.call_args[1]["json"]
        user_content = call_body["messages"][0]["content"]
        assert 'Agent asked: "Who is the manufacturer?"' in user_content
        assert "User replied: " in user_content

    def test_prior_question_absent_on_initial_turn(self):
        """On first turn (no prior_question), prompt uses plain 'User input:' framing."""
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"manufacturer_confidence": 80, "part_id_confidence": 70, "model": "PMC11"}
        run   = _make_run(specs=prior)

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())
            agent.run(run, {
                "text":          "PMC11-AA1V1HFVXJA",
                "images":        [],
                "force_proceed": False,
            })

        call_body    = mock_post.call_args[1]["json"]
        user_content = call_body["messages"][0]["content"]
        assert "Agent asked:" not in user_content

    def test_two_turn_loop_resolves_without_second_clarification(self):
        """Integration: turn1 blocks → turn2 with answer + prior_question → proceeds."""
        agent = IntakeAgent(anthropic_api_key="test-key")

        # Turn 1: "PMC11-AA1V1HFVXJA" — mfg unknown, part recognized
        turn1_response = _extracted({
            "manufacturer":            None,
            "model":                   "PMC11-AA1V1HFVXJA",
            "part_number":             None,   # model-only (no PN) -> psi still required (spec-based)
            "detected_type":           "pressure sensor",
            "manufacturer_confidence": 0,
            "part_id_confidence":      72,
        })
        run    = _make_run()
        haiku1 = MagicMock()
        haiku1.raise_for_status = MagicMock()
        haiku1.json.return_value = {"content": [{"text": "Who is the manufacturer of this equipment?"}]}

        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_anthropic_response(turn1_response),
                haiku1,
            ]
            result1 = agent.run(run, {"text": "PMC11-AA1V1HFVXJA", "images": [], "force_proceed": False})

        assert result1["sufficient"] is False
        assert result1["follow_up_question"] is not None
        clarification_q = result1["follow_up_question"]

        # Turn 2: user answers the clarification — prior specs carry mfg_conf=0, part_conf=72
        prior_after_turn1 = result1["asset_specs"]
        run2 = _make_run(specs=prior_after_turn1)
        turn2_response = _extracted({
            "manufacturer":            "Endress+Hauser",
            "model":                   "PMC11-AA1V1HFVXJA",
            "part_number":             None,   # still model-only; psi (now supplied) satisfies the category
            "detected_type":           "pressure sensor",
            "psi":                     "150",  # required field for pressure sensor category
            "manufacturer_confidence": 92,
            "part_id_confidence":      35,
        })

        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn2_response)
            result2 = agent.run(run2, {
                "text":           "Endress Hauser",
                "images":         [],
                "force_proceed":  False,
                "prior_question": clarification_q,
            })

        # Fix A: part_conf = max(72, 35) = 72; mfg_conf = max(0, 92) = 92
        # assess_proceed_state(merged, 92, 72) → proceed_full_confidence
        assert result2["sufficient"] is True
        assert result2["follow_up_question"] is None
        assert result2["manufacturer_confidence"] == 92
        assert result2["part_id_confidence"] == 72


# ---------------------------------------------------------------------------
# Media type detection
# ---------------------------------------------------------------------------

class TestDetectMediaType:
    def test_jpeg_magic_bytes(self):
        assert _detect_media_type(b"\xff\xd8\xff\xe0" + b"\x00" * 64) == "image/jpeg"

    def test_png_magic_bytes(self):
        assert _detect_media_type(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64) == "image/png"

    def test_webp_magic_bytes(self):
        assert _detect_media_type(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64) == "image/webp"

    def test_unknown_bytes_default_to_jpeg(self):
        assert _detect_media_type(b"\x00\x01\x02\x03" + b"\x00" * 64) == "image/jpeg"

    def test_riff_without_webp_marker_defaults_to_jpeg(self):
        assert _detect_media_type(b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 64) == "image/jpeg"


# ---------------------------------------------------------------------------
# Multi-part list extraction — the LLM returns a JSON ARRAY when the user describes
# several parts. run() must NOT crash (the old `extracted.items()` AttributeError) — it
# detects the list and responds honestly ("one part at a time"), unwraps a list-of-1, and
# degrades an empty/malformed list to a clarification.
# ---------------------------------------------------------------------------

class TestMultiPartListHandling:
    def test_list_of_two_returns_honest_one_at_a_time(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"manufacturer_confidence": 0, "part_id_confidence": 0}
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                [_extracted({"manufacturer": "SKF"}), _extracted({"manufacturer": "Gates"})])
            result = agent.run(_make_run(prior),
                               {"text": "a bearing and a belt", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert "several parts (2 detected)" in result["follow_up_question"]
        assert result["confidence_summary"]["proceed_state"] == "multi_part_detected"
        # No silent data loss / no fabricated specs: nothing merged from the list.
        assert "manufacturer" not in result["asset_specs"]

    def test_list_wrapped_single_is_unwrapped(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([_extracted()])  # list of exactly 1
            result = agent.run(_make_run(),
                               {"text": "Grundfos CR32-5", "images": [], "force_proceed": False})
        # Unwrapped to the single-part path — processed normally, NOT the multi-part message.
        assert result["asset_specs"]["manufacturer"] == "Grundfos"
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"

    def test_empty_list_degrades_to_clarification_no_crash(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([])  # empty / malformed
            result = agent.run(_make_run(),
                               {"text": "???", "images": [], "force_proceed": False})
        assert result["sufficient"] is False  # asks for more — no crash, no multi-part message
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"

    def test_single_dict_path_unchanged(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted())  # normal dict
            result = agent.run(_make_run(),
                               {"text": "Grundfos CR32-5", "images": [], "force_proceed": False})
        assert result["sufficient"] is True          # byte-for-byte the existing success path
        assert result["follow_up_question"] is None


class TestMultiPartArrayExtraction:
    """The _EXTRACTION_SYSTEM array instruction makes the model emit a JSON array for 2+ DISTINCT
    parts (so run()'s list-gate fires) while keeping a single part with comma-separated ATTRIBUTES
    as one object. Offline, we mock the LLM to return the shapes the prompt is designed to elicit
    and assert run()'s contract on each — the headline being that a 2-part input no longer reports
    sufficient=True with one part silently dropped."""

    def test_two_distinct_parts_array_not_dropped_into_success(self):
        # THE DATA-LOSS HEADLINE: "SKF 6205-2RS1, FLOWSIC610" is a bearing AND a gas-flow analyzer.
        # The prompt elicits a 2-element array -> run() -> _multi_part_response: sufficient=False,
        # nothing merged. FLOWSIC610 is no longer silently dropped while the run reports success.
        agent = IntakeAgent(anthropic_api_key="test-key")
        skf = _extracted({"manufacturer": "SKF", "model": "6205-2RS1", "part_number": "6205-2RS1",
                          "detected_type": "deep groove ball bearing", "category": "Part"})
        flowsic = _extracted({"manufacturer": "SICK", "model": "FLOWSIC610", "part_number": "FLOWSIC610",
                              "detected_type": "gas flow analyzer", "category": "Equipment"})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([skf, flowsic])
            result = agent.run(_make_run({"manufacturer_confidence": 0, "part_id_confidence": 0}),
                               {"text": "SKF 6205-2RS1, FLOWSIC610", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert "several parts (2 detected)" in result["follow_up_question"]
        assert result["confidence_summary"]["proceed_state"] == "multi_part_detected"
        # No merge -> neither part is dropped into a partial "success"; both held for re-entry.
        assert "manufacturer" not in result["asset_specs"]

    def test_single_part_with_comma_attributes_is_one_object_not_split(self):
        # FALSE-POSITIVE GUARD: "1/2 inch ball valve, NPT threaded" is ONE valve (the comma
        # separates an attribute, not a second part). The prompt elicits ONE object -> single-part
        # path, NOT the multi-part message.
        agent = IntakeAgent(anthropic_api_key="test-key")
        valve = _extracted({"manufacturer": "Apollo", "detected_type": "ball valve", "category": "Part",
                            "connection_size": "1/2 inch", "model": None, "part_number": None})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(valve)
            result = agent.run(_make_run(),
                               {"text": "1/2 inch ball valve, NPT threaded", "images": [], "force_proceed": False})
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"
        assert result["asset_specs"]["detected_type"] == "ball valve"   # processed as ONE part

    def test_single_bearing_with_bore_attribute_is_one_object_not_split(self):
        # FALSE-POSITIVE GUARD #2: "deep groove ball bearing, 25mm bore" -> one bearing (bore is a
        # dimension, not a second part).
        agent = IntakeAgent(anthropic_api_key="test-key")
        bearing = _extracted({"manufacturer": "SKF", "detected_type": "deep groove ball bearing",
                              "category": "Part", "bore_diameter": "25mm", "model": None, "part_number": None})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(bearing)
            result = agent.run(_make_run(),
                               {"text": "deep groove ball bearing, 25mm bore", "images": [], "force_proceed": False})
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"
        assert result["asset_specs"]["detected_type"] == "deep groove ball bearing"

    def test_single_part_with_pn_still_sufficient(self):
        # A lone part is unaffected: "SKF 6205-2RS1" -> one object -> sufficient (over-ask fix
        # intact), never a false multi-part detection.
        agent = IntakeAgent(anthropic_api_key="test-key")
        skf = _extracted({"manufacturer": "SKF", "model": "6205-2RS1", "part_number": "6205-2RS1",
                          "detected_type": "deep groove ball bearing", "category": "Part"})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(skf)
            result = agent.run(_make_run(),
                               {"text": "SKF 6205-2RS1", "images": [], "force_proceed": False})
        assert result["sufficient"] is True
        assert result["follow_up_question"] is None
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"

    def test_array_wrapped_single_part_is_unwrapped(self):
        # If the model over-wraps a lone part as [{...}], run()'s list-of-1 unwrap catches it ->
        # single-part path, not a spurious multi-part message.
        agent = IntakeAgent(anthropic_api_key="test-key")
        skf = _extracted({"manufacturer": "SKF", "model": "6205-2RS1", "part_number": "6205-2RS1",
                          "detected_type": "deep groove ball bearing", "category": "Part"})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([skf])  # array of exactly 1
            result = agent.run(_make_run(),
                               {"text": "SKF 6205-2RS1", "images": [], "force_proceed": False})
        assert result["asset_specs"]["manufacturer"] == "SKF"
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"

    def test_multi_part_returns_parsed_parts_for_fanout(self):
        # The parsed parts ride along as multi_part_specs so a caller can fan them into N cards;
        # still sufficient=False, proceed_state=multi_part_detected, nothing merged into THIS run.
        agent = IntakeAgent(anthropic_api_key="test-key")
        skf = _extracted({"manufacturer": "SKF", "model": "6205-2RS1", "part_number": "6205-2RS1",
                          "detected_type": "deep groove ball bearing", "category": "Part"})
        flowsic = _extracted({"manufacturer": "SICK", "model": "FLOWSIC610", "part_number": "FLOWSIC610",
                              "detected_type": "gas flow analyzer", "category": "Equipment"})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([skf, flowsic])
            result = agent.run(_make_run({"manufacturer_confidence": 0, "part_id_confidence": 0}),
                               {"text": "SKF 6205-2RS1, FLOWSIC610", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert result["confidence_summary"]["proceed_state"] == "multi_part_detected"
        parts = result["multi_part_specs"]
        assert len(parts) == 2
        assert parts[0]["part_number"] == "6205-2RS1"
        assert parts[1]["model"] == "FLOWSIC610"
        assert "manufacturer" not in result["asset_specs"]   # nothing merged into THIS run

    def test_multipart_text_WITH_image_also_detects_array(self):
        # The #2 tie-in: a multi-part description sent WITH an image goes through the multimodal
        # extractor (images present) but hits the SAME run() list-gate -> _multi_part_response.
        # So plumbing the text into the image path extends #1's detection to image+text uploads.
        agent = IntakeAgent(anthropic_api_key="test-key")
        skf = _extracted({"manufacturer": "SKF", "model": "6205-2RS1", "part_number": "6205-2RS1",
                          "detected_type": "deep groove ball bearing", "category": "Part"})
        flowsic = _extracted({"manufacturer": "SICK", "model": "FLOWSIC610", "part_number": "FLOWSIC610",
                              "detected_type": "gas flow analyzer", "category": "Equipment"})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([skf, flowsic])
            result = agent.run(_make_run({"manufacturer_confidence": 0, "part_id_confidence": 0}),
                               {"text": "SKF 6205-2RS1, FLOWSIC610", "images": [b"\xff\xd8img"],
                                "force_proceed": False})
        assert result["sufficient"] is False
        assert "several parts (2 detected)" in result["follow_up_question"]
        assert result["confidence_summary"]["proceed_state"] == "multi_part_detected"


class TestPnHintMultiPartScoping:
    """The pn-prefix hint used to be a SINGULAR prompt note that biased the LLM to one part and
    collapsed multi-part input. Split-first (Approach 2) REMOVES the prompt note — the LLM's
    array/attribute rule decides part-count unbiased — and infers the manufacturer PER-PART after
    extraction (_apply_pn_manufacturer). These assert the note is gone from the prompt AND that
    per-object inference sets the manufacturer for single, multi, boundary, and prior-specs cases.
    (PMC21 -> Endress+Hauser, 22C -> Allen-Bradley resolve offline via the static prefix map.)

    Note: d0b4c30's note-assertion tests were REPLACED — the prompt note no longer exists; these
    assert the per-object inference that took its place (flagged in the commit)."""

    def test_no_pn_hint_note_in_prompt_text_path(self):
        # The singular manufacturer note is GONE from the extraction prompt — the LLM decides
        # part-count UNBIASED (no "manufactured by" / "SYSTEM NOTE" steering it to one part).
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted({"model": "PMC21"}))
            agent.run(_make_run(), {"text": "Cerabar PMC21", "images": [], "force_proceed": False})
        prompt = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        assert "manufactured by" not in prompt
        assert "SYSTEM NOTE" not in prompt

    def test_no_pn_hint_note_in_prompt_image_path(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(_extracted({"model": "PMC21"}))
            agent.run(_make_run(), {"text": "Cerabar PMC21", "images": [b"\xff\xd8img"], "force_proceed": False})
        content = mock_post.call_args_list[0].kwargs["json"]["messages"][0]["content"]
        textblock = next(c["text"] for c in content if c.get("type") == "text")
        assert "manufactured by" not in textblock
        assert "SYSTEM NOTE" not in textblock

    def test_single_part_manufacturer_inferred_per_object(self):
        # BENEFIT KEPT (now per-object): the LLM leaves manufacturer null; the PMC21 prefix on the
        # part's OWN part_number infers Endress+Hauser after extraction.
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer": None, "model": "PMC21", "part_number": "PMC21",
                            "detected_type": "pressure transmitter", "manufacturer_confidence": 0}))
            result = agent.run(_make_run(), {"text": "Cerabar PMC21", "images": [], "force_proceed": False})
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"
        assert result["asset_specs"]["manufacturer"] == "Endress+Hauser"

    def test_multipart_collapse_fixed_and_each_part_inferred(self):
        # THE COLLAPSE FIX + the per-element improvement: "Cerabar PMC21, ... 22C VFD" -> 2-array ->
        # multi_part_detected; EACH part's manufacturer is inferred from its OWN PN (E+H, Allen-Bradley),
        # even though the LLM left both null. (The old list-skip guard inferred neither.)
        agent = IntakeAgent(anthropic_api_key="test-key")
        eh = _extracted({"manufacturer": None, "model": "PMC21", "part_number": "PMC21",
                         "detected_type": "pressure transmitter", "category": "Part", "manufacturer_confidence": 0})
        ab = _extracted({"manufacturer": None, "model": "22C-D010N104", "part_number": "22C-D010N104",
                         "detected_type": "vfd", "category": "Part", "manufacturer_confidence": 0})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response([eh, ab])
            result = agent.run(_make_run({"manufacturer_confidence": 0, "part_id_confidence": 0}),
                               {"text": "Cerabar PMC21, 22C-D010N104 drive", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert result["confidence_summary"]["proceed_state"] == "multi_part_detected"
        parts = result["multi_part_specs"]
        assert len(parts) == 2
        assert parts[0]["manufacturer"] == "Endress+Hauser"     # inferred per-element
        assert parts[1]["manufacturer"] == "Allen-Bradley"      # inferred per-element (not just the first)
        assert "manufacturer" not in result["asset_specs"]      # nothing merged into THIS run

    def test_boundary_single_part_with_attribute_commas_infers_mfg(self):
        # THE CASE THE GATE COULD NOT DO: "Cerabar PMC21, 4-20mA output" is ONE part (the LLM's
        # attribute rule keeps it single); per-object inference still sets manufacturer=E+H, and it
        # is NOT split into two. Split-first delegates the part-count decision entirely to the LLM.
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer": None, "model": "PMC21", "part_number": "PMC21",
                            "description": "PMC21 pressure transmitter, 4-20mA output",
                            "detected_type": "pressure transmitter", "manufacturer_confidence": 0}))
            result = agent.run(_make_run(), {"text": "Cerabar PMC21, 4-20mA output", "images": [], "force_proceed": False})
        assert result["confidence_summary"]["proceed_state"] != "multi_part_detected"
        assert result["asset_specs"]["manufacturer"] == "Endress+Hauser"

    def test_prior_specs_pn_infers_on_followup(self):
        # PRIOR-SPECS PATH PRESERVED: on a follow-up turn the new extraction has no PN, but an
        # established part_number in prior_specs (PMC21) still infers the manufacturer per-object.
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {"part_number": "PMC21", "manufacturer_confidence": 0, "part_id_confidence": 85}
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer": None, "part_number": None, "model": None,
                            "detected_type": "pressure transmitter", "manufacturer_confidence": 0}))
            result = agent.run(_make_run(prior), {"text": "it's a pressure transmitter", "images": [], "force_proceed": False})
        assert result["asset_specs"]["manufacturer"] == "Endress+Hauser"


# ---------------------------------------------------------------------------
# Over-ask fix: a confident PART NUMBER uniquely identifies the part, so the category
# DIMENSION fields (bore_diameter, shaft_size, material_spec) are redundant and must not be
# re-asked. The skip is narrowed to the PN-present case — the no-PN (spec-based) path is intact.
# ---------------------------------------------------------------------------

from utils.procurement_agent.agents.intake_agent import assess_proceed_state


class TestPartNumberBypassesDimensionCheck:
    def test_skf_bearing_with_pn_no_bore_is_sufficient(self):
        # HEADLINE: SKF 6205-2RS1, confident mfg+PN, no bore_diameter -> proceed, NO bore re-ask.
        specs = {"detected_type": "deep groove ball bearing", "manufacturer": "SKF",
                 "part_number": "6205-2RS1", "bore_diameter": None}
        state, missing, caveat = assess_proceed_state(specs, 92, 85)
        assert state == "proceed_full_confidence" and missing is None

    def test_gusher_seal_with_pn_no_material_is_sufficient(self):
        # A mechanical seal needs shaft_size + material_spec by category; a PN makes both redundant.
        specs = {"detected_type": "mechanical seal", "manufacturer": "Gusher",
                 "part_number": "TYPE-21-S", "shaft_size": None, "material_spec": None}
        state, missing, caveat = assess_proceed_state(specs, 90, 88)
        assert state == "proceed_full_confidence" and missing is None

    def test_no_pn_bearing_still_asks_for_bore(self):
        # REGRESSION: spec-based sourcing (no PN) MUST still require the dimensions.
        specs = {"detected_type": "bearing", "bore_diameter": None}   # no part_number
        state, missing, caveat = assess_proceed_state(specs, 90, 75)
        assert state == "needs_clarification" and missing == "bore_diameter"

    def test_model_only_still_asks_for_dimension(self):
        # Model alone names a family with dimensioned variants -> does NOT bypass; still asks.
        specs = {"detected_type": "bearing", "model": "6205", "bore_diameter": None}  # no part_number
        state, missing, caveat = assess_proceed_state(specs, 90, 75)
        assert state == "needs_clarification" and missing == "bore_diameter"

    def test_placeholder_part_number_does_not_bypass(self):
        # "UNKNOWN-PN" is a null value, not a real PN -> no bypass, still asks.
        specs = {"detected_type": "bearing", "part_number": "UNKNOWN-PN", "bore_diameter": None}
        state, missing, caveat = assess_proceed_state(specs, 90, 75)
        assert state == "needs_clarification" and missing == "bore_diameter"

    def test_run_end_to_end_pn_present_is_sufficient_no_followup(self):
        # The full run() path: a PN'd bearing with no bore -> sufficient, no follow-up question.
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = _extracted({"manufacturer": "SKF", "model": None, "part_number": "6205-2RS1",
                              "detected_type": "deep groove ball bearing", "category": "Part",
                              "gpm": None, "bore_diameter": None,
                              "manufacturer_confidence": 92, "part_id_confidence": 85})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(payload)
            result = agent.run(_make_run(), {"text": "SKF 6205-2RS1 bearing", "images": [], "force_proceed": False})
        assert result["sufficient"] is True
        assert result["follow_up_question"] is None


# ---------------------------------------------------------------------------
# Over-questioning fix — bounded intake: identity-first opener, never re-ask,
# hard cap (3) + auto-commit, and the manufacturer→refinement gate (#4).
# Internal `_`-prefixed ledger keys (_asked_fields, _intake_turns) ride on
# asset_specs_json (no new column) and are filtered from the extractor context.
# ---------------------------------------------------------------------------

from utils.procurement_agent.agents.intake_agent import (
    INTAKE_TURN_CAP,
    _DEFAULT_QUESTIONS,
    _spec_based_ready,
    _has_identity,
)


class TestSpecBasedReadyGate:
    """The manufacturer-refinement gate (#4) helper: type known + category dims present +
    no identity → spec-based ready; identity present or dims absent → not ready."""

    def test_valve_with_connection_size_no_identity_is_ready(self):
        specs = {"detected_type": "ball valve", "connection_size": "2 inch"}
        assert _spec_based_ready(specs) is True

    def test_valve_missing_connection_size_not_ready(self):
        specs = {"detected_type": "ball valve", "connection_size": None}
        assert _spec_based_ready(specs) is False

    def test_identity_present_not_ready(self):
        # A model or PN means a more specific handle exists → not pure spec-based.
        specs = {"detected_type": "ball valve", "connection_size": "2 inch", "model": "BV-200"}
        assert _spec_based_ready(specs) is False

    def test_no_category_not_ready(self):
        # "pump" has no category entry → can't confirm dims → keep asking.
        specs = {"detected_type": "pump"}
        assert _spec_based_ready(specs) is False

    def test_no_detected_type_not_ready(self):
        assert _spec_based_ready({}) is False


class TestManufacturerRefinementGate:
    """Fix #4 at the assess_proceed_state level: type known + category dims present +
    mfg unknown + no identity → proceed_spec_based (not blocked_need_either / caveat)."""

    def test_valve_type_and_dims_mfg_unknown_proceeds_spec_based(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "ball valve", "connection_size": "2 inch"}
        # mfg low, pid in [70,80) — old behavior was blocked_need_either (pid<80).
        state, missing, caveat = assess_proceed_state(specs, 25, 75)
        assert state == "proceed_spec_based"
        assert missing is None
        assert caveat is None

    def test_valve_type_and_dims_high_pid_proceeds_spec_based_not_caveat(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "ball valve", "connection_size": "2 inch"}
        # pid>=80 would normally be proceed_with_manufacturer_caveat; dims present → spec-based.
        state, missing, caveat = assess_proceed_state(specs, 25, 85)
        assert state == "proceed_spec_based"
        assert caveat is None

    def test_caveat_still_applies_when_dims_absent(self):
        # Regression: the existing PMC11-style caveat path is intact when dims are absent.
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "pressure sensor", "model": "PMC11"}  # psi missing, model present
        state, missing, caveat = assess_proceed_state(specs, 45, 85)
        assert state == "proceed_with_manufacturer_caveat"
        assert caveat is not None

    def test_blocked_either_still_applies_when_pid_below_70(self):
        from utils.procurement_agent.agents.intake_agent import assess_proceed_state
        specs = {"detected_type": "ball valve", "connection_size": "2 inch"}
        state, missing, caveat = assess_proceed_state(specs, 25, 55)  # pid<70 → not spec-based ready
        assert state == "blocked_need_either"


class TestIdentityFirstOpener:
    """Fix #1: on the first clarification turn with no part identity, ask the identity
    question verbatim (not via the haiku generator)."""

    def test_first_vague_turn_asks_identity_question_verbatim(self):
        agent = IntakeAgent(anthropic_api_key=None)  # no key → fallback extraction (mfg=0,pid=0)
        result = agent.run(_make_run(), {"text": "I need a part", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert result["follow_up_question"] == _DEFAULT_QUESTIONS["part_identity"]
        # The opener is recorded in the ledger so it is never re-asked.
        assert result["asset_specs"].get("_asked_fields") == ["part_identity"]
        assert result["asset_specs"].get("_intake_turns") == 1

    def test_opener_skipped_when_identity_present_on_first_turn(self):
        # A first turn that already carries a model/PN must NOT ask the opener — it goes
        # through the normal field selection.
        agent = IntakeAgent(anthropic_api_key="test-key")
        # model present → has identity → not opener; blocked_need_either → asks manufacturer.
        low_conf = _extracted({"manufacturer_confidence": 30, "part_id_confidence": 55})  # model="CR32-5"
        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_anthropic_response(low_conf),
                _mock_anthropic_response(low_conf),  # haiku clarification call
            ]
            result = agent.run(_make_run(), {"text": "a pump", "images": [], "force_proceed": False})
        assert result["sufficient"] is False
        assert result["follow_up_question"] != _DEFAULT_QUESTIONS["part_identity"]
        assert "part_identity" not in (result["asset_specs"].get("_asked_fields") or [])


class TestNeverReAsk:
    """Fix #2: an already-asked-and-still-unanswered field is not repeated; if nothing fresh
    remains to ask, the turn commits to spec-based sourcing."""

    def test_unanswerable_required_field_asked_once_not_repeated(self):
        # A no-PN bearing whose bore was already asked (in _asked_fields) and is still
        # unanswerable. The assessor still says needs_clarification (bore missing), but the
        # picker must NOT re-ask bore — with no other field available it commits spec-based.
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {
            "detected_type":           "bearing",
            "manufacturer":            "SKF",
            "bore_diameter":           None,
            "manufacturer_confidence": 90,
            "part_id_confidence":      75,
            "_asked_fields":           ["bore_diameter"],
            "_intake_turns":           1,
        }
        turn = _extracted({
            "detected_type": "bearing", "manufacturer": "SKF",
            "model": None, "part_number": None, "bore_diameter": None,
            "manufacturer_confidence": 90, "part_id_confidence": 75,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn)
            result = agent.run(_make_run(specs=prior),
                               {"text": "I don't know the bore", "images": [], "force_proceed": False})
        # Committed spec-based — NOT another bore question.
        assert result["sufficient"] is True
        assert result["follow_up_question"] is None
        assert result.get("commit_message") is not None
        assert result["asset_specs"].get("spec_based_sourcing") is True
        # bore never fabricated.
        assert result["asset_specs"].get("bore_diameter") in (None, "", "null", "N/A")

    def test_picker_moves_to_next_unasked_dim(self):
        # Motor missing hp+frame+rpm (voltage present); hp already asked (unanswered) →
        # ask the next unasked missing dim, not hp again. Motor field order is
        # [detected_type, hp, voltage, frame, rpm]; voltage present so it is skipped.
        from utils.procurement_agent.agents.intake_agent import _first_unasked_missing_field
        specs = {"detected_type": "induction motor", "hp": None,
                 "voltage": "460V", "frame": None, "rpm": None}
        assert _first_unasked_missing_field(specs, ["hp"]) == "frame"
        assert _first_unasked_missing_field(specs, ["hp", "frame"]) == "rpm"
        assert _first_unasked_missing_field(specs, ["hp", "frame", "rpm"]) is None


class TestHardCapAutoCommit:
    """Fix #3: after INTAKE_TURN_CAP non-sufficient clarification turns, the agent commits
    to spec-based sourcing instead of asking again."""

    def test_three_vague_turns_auto_commit_spec_based(self):
        # Totally vague input (no type, no identity), no API key → blocked_need_either each
        # turn. Turn 1 asks the opener (part_identity), turn 2 asks manufacturer, turn 3
        # hits the cap and commits spec-based.
        agent = IntakeAgent(anthropic_api_key=None)
        run = _make_run()

        # Turn 1 — opener
        r1 = agent.run(run, {"text": "I need a part", "images": [], "force_proceed": False})
        assert r1["sufficient"] is False
        assert r1["follow_up_question"] == _DEFAULT_QUESTIONS["part_identity"]
        assert r1["asset_specs"]["_intake_turns"] == 1

        # Turn 2 — manufacturer question
        run2 = _make_run(specs=r1["asset_specs"])
        r2 = agent.run(run2, {"text": "no idea", "images": [], "force_proceed": False})
        assert r2["sufficient"] is False
        assert r2["asset_specs"]["_intake_turns"] == 2
        assert "manufacturer" in (r2["asset_specs"].get("_asked_fields") or [])

        # Turn 3 — cap reached → auto-commit spec-based
        run3 = _make_run(specs=r2["asset_specs"])
        r3 = agent.run(run3, {"text": "still no idea", "images": [], "force_proceed": False})
        assert r3["sufficient"] is True
        assert r3["follow_up_question"] is None
        assert r3.get("commit_message") is not None
        assert r3["confidence_summary"]["proceed_state"] == "forced_commit"
        assert r3["asset_specs"].get("spec_based_sourcing") is True


class TestValveCaseNoLoop:
    """The headline valve case: a vague valve input + a 'no preference' answer does NOT loop
    on the manufacturer question — it commits spec-based within the cap via the
    manufacturer-refinement gate (#4)."""

    def test_valve_no_preference_commits_spec_based(self):
        agent = IntakeAgent(anthropic_api_key="test-key")

        # Turn 1: "I need a ball valve" — type known, no connection_size, no identity.
        # mfg low, pid ~60 → blocked_need_either; first turn + no identity → opener asks
        # part_identity (NOT manufacturer).
        turn1 = _extracted({
            "manufacturer": None, "model": None, "part_number": None,
            "detected_type": "ball valve", "connection_size": None,
            "manufacturer_confidence": 25, "part_id_confidence": 60,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn1)
            r1 = agent.run(_make_run(), {"text": "I need a ball valve", "images": [], "force_proceed": False})
        assert r1["sufficient"] is False
        assert r1["follow_up_question"] == _DEFAULT_QUESTIONS["part_identity"]

        # Turn 2: "no preference on brand, it's a 2 inch valve" — dims now present, still no
        # identity. mfg<70, pid>=70, _spec_based_ready → proceed_spec_based (NO manufacturer
        # re-ask, NO loop).
        turn2 = _extracted({
            "manufacturer": None, "model": None, "part_number": None,
            "detected_type": "ball valve", "connection_size": "2 inch",
            "manufacturer_confidence": 25, "part_id_confidence": 78,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn2)
            r2 = agent.run(_make_run(specs=r1["asset_specs"]),
                           {"text": "no preference on brand, it's a 2 inch valve",
                            "images": [], "force_proceed": False})
        assert r2["sufficient"] is True
        assert r2["follow_up_question"] is None
        assert r2["confidence_summary"]["proceed_state"] == "proceed_spec_based"
        assert r2["asset_specs"].get("spec_based_sourcing") is True


class TestInternalKeysFiltered:
    """The `_`-prefixed ledger keys must not leak into the extractor context summary."""

    def test_build_context_summary_excludes_internal_keys(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        prior = {
            "manufacturer": "SKF", "model": "6205-2RS1",
            "manufacturer_confidence": 90, "part_id_confidence": 85,
            "_asked_fields": ["part_identity"], "_intake_turns": 2,
        }
        summary = agent._build_context_summary(prior)
        assert "_asked_fields" not in summary
        assert "_intake_turns" not in summary
        # Real spec fields still present.
        assert summary["manufacturer"] == "SKF"

    def test_run_does_not_call_haiku_for_opener(self):
        # The opener is verbatim — the haiku clarification endpoint is NOT hit on turn 1
        # when the opener fires (only the extraction call).
        agent = IntakeAgent(anthropic_api_key="test-key")
        turn1 = _extracted({
            "manufacturer": None, "model": None, "part_number": None,
            "detected_type": "ball valve", "connection_size": None,
            "manufacturer_confidence": 25, "part_id_confidence": 60,
        })
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(turn1)
            agent.run(_make_run(), {"text": "I need a ball valve", "images": [], "force_proceed": False})
        # Exactly one call (extraction only) — no haiku clarification call for the opener.
        assert mock_post.call_count == 1


class TestCleanPnRegression:
    """Regression: the existing clean-PN paths (SKF/AB style) still reach sufficient normally
    with no clarification, no commit, and no internal ledger keys on a fresh run."""

    def test_skf_bearing_with_pn_sufficient_no_ledger(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        payload = _extracted({"manufacturer": "SKF", "model": None, "part_number": "6205-2RS1",
                              "detected_type": "deep groove ball bearing", "category": "Part",
                              "bore_diameter": None,
                              "manufacturer_confidence": 92, "part_id_confidence": 85})
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(payload)
            result = agent.run(_make_run(), {"text": "SKF 6205-2RS1 bearing", "images": [], "force_proceed": False})
        assert result["sufficient"] is True
        assert result["follow_up_question"] is None
        assert result.get("commit_message") is None
        assert result["confidence_summary"]["proceed_state"] == "proceed_full_confidence"
        # No internal ledger keys on a fresh sufficient run.
        assert "_asked_fields" not in result["asset_specs"]
        assert "_intake_turns" not in result["asset_specs"]
        # spec_based_sourcing not set on a PN-identified part.
        assert "spec_based_sourcing" not in result["asset_specs"]

    def test_has_identity_helper(self):
        assert _has_identity({"manufacturer": "SKF"}) is True
        assert _has_identity({"model": "CR32-5"}) is True
        assert _has_identity({"part_number": "6205-2RS1"}) is True
        assert _has_identity({"detected_type": "bearing"}) is False
        assert _has_identity({}) is False
