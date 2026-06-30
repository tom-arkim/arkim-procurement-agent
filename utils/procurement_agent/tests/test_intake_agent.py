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

        # Fix A: part_conf = max(72, 30) = 72 (floor preserved from prior turn)
        # mfg_conf = max(0, 90) = 90
        # assess_proceed_state(merged, 90, 72) → both ≥ 70 → proceed_full_confidence
        assert result["manufacturer_confidence"] == 90
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
