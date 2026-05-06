"""
Tests for IntakeAgent — multimodal extraction, sufficiency assessment, clarification.

All LLM calls (requests.post) are mocked so tests run without API keys.
"""

import json
from unittest.mock import patch, MagicMock

import pytest

from utils.models import ProcurementRun
from utils.procurement_agent.agents.intake_agent import (
    IntakeAgent,
    CATEGORY_REQUIRED_FIELDS,
    SUFFICIENCY_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_run(specs: dict | None = None) -> ProcurementRun:
    return ProcurementRun(asset_specs_json=specs)


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
    def test_fails_when_manufacturer_confidence_low(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        with patch("requests.post") as mock_post:
            mock_post.return_value = _mock_anthropic_response(
                _extracted({"manufacturer_confidence": 40, "manufacturer": None})
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
        agent = IntakeAgent(anthropic_api_key="test-key")
        haiku_response = MagicMock()
        haiku_response.raise_for_status = MagicMock()
        haiku_response.json.return_value = {"content": [{"text": "What is the HP rating?"}]}

        low_conf = _extracted({"manufacturer_confidence": 30})
        with patch("requests.post") as mock_post:
            mock_post.side_effect = [
                _mock_anthropic_response(low_conf),  # extraction call
                haiku_response,                        # clarification call
            ]
            result = agent.run(_make_run(), {"text": "unknown part", "images": [], "force_proceed": False})

        assert result["follow_up_question"] == "What is the HP rating?"

    def test_fallback_question_on_clarification_error(self):
        agent = IntakeAgent(anthropic_api_key="test-key")
        low_conf = _extracted({"manufacturer_confidence": 30})
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
