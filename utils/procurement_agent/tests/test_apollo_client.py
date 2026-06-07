"""
Tests for utils/apollo_client.py — the thin Apollo client (org enrich + people search).

The HTTP boundary is mocked (patch requests.get / requests.post inside the
apollo_client module). NO live Apollo calls are made. Mirrors the suite's
unittest.mock / MagicMock pattern (see test_sourcing_agent.py).
"""

import requests
from unittest.mock import patch, MagicMock

from utils.apollo_client import ApolloClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(json_payload):
    """Build a MagicMock standing in for a requests.Response."""
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = json_payload
    return resp


_TEST_KEY = "test-apollo-key"


# ---------------------------------------------------------------------------
# org_enrich
# ---------------------------------------------------------------------------

class TestOrgEnrich:
    def test_hit_parses_fields(self):
        """A matched organization is mapped to the structured result dict."""
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {
            "organization": {
                "name": "Phoenix Pumps Inc",
                "industry": "industrial automation",
                "country": "United States",
                "state": "Arizona",
                "raw_address": "123 Main St, Phoenix, AZ 85001, USA",
                "short_description": "Distributor of industrial pumps and seals.",
                "keywords": ["pumps", "mechanical seals", "distribution"],
            }
        }
        with patch("utils.apollo_client.requests.get", return_value=_mock_response(payload)) as mget:
            result = client.org_enrich("phoenixpumps.com")

        mget.assert_called_once()
        assert result == {
            "name": "Phoenix Pumps Inc",
            "industry": "industrial automation",
            "country": "United States",
            "state": "Arizona",
            "raw_address": "123 Main St, Phoenix, AZ 85001, USA",
            "description": "Distributor of industrial pumps and seals.",
            "keywords": ["pumps", "mechanical seals", "distribution"],
        }

    def test_description_falls_back_to_description_field(self):
        """When short_description is absent, fall back to description."""
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {"organization": {"name": "Acme", "description": "Long form desc"}}
        with patch("utils.apollo_client.requests.get", return_value=_mock_response(payload)):
            result = client.org_enrich("acme.com")
        assert result["description"] == "Long form desc"
        assert result["keywords"] == []  # missing keywords default to []

    def test_miss_returns_none(self):
        """No organization in the payload -> miss -> None (0 credits)."""
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.get", return_value=_mock_response({})):
            assert client.org_enrich("nonexistent-domain.com") is None

    def test_empty_domain_returns_none_without_http(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.get") as mget:
            assert client.org_enrich("") is None
            assert client.org_enrich(None) is None
        mget.assert_not_called()

    def test_timeout_returns_none_not_raise(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.get", side_effect=requests.exceptions.Timeout):
            assert client.org_enrich("phoenixpumps.com") is None

    def test_http_error_returns_none_not_raise(self):
        client = ApolloClient(api_key=_TEST_KEY)
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.exceptions.HTTPError("429 rate limit")
        with patch("utils.apollo_client.requests.get", return_value=bad):
            assert client.org_enrich("phoenixpumps.com") is None


# ---------------------------------------------------------------------------
# people_search
# ---------------------------------------------------------------------------

class TestPeopleSearch:
    def test_returns_contacts(self):
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {
            "people": [
                {"name": "Jane Sales", "title": "Sales Manager",
                 "email": "ja***@phoenixpumps.com", "email_status": "verified"},
                {"first_name": "Bob", "last_name": "Support", "title": "Customer Service",
                 "email": None, "email_status": "guessed"},
            ]
        }
        with patch("utils.apollo_client.requests.post", return_value=_mock_response(payload)) as mpost:
            contacts = client.people_search("phoenixpumps.com")

        mpost.assert_called_once()
        assert len(contacts) == 2
        assert contacts[0] == {
            "name": "Jane Sales",
            "title": "Sales Manager",
            "email_status": "verified",
            "has_email": True,
            "masked_email": "ja***@phoenixpumps.com",
        }
        # Name composed from first/last when 'name' absent.
        assert contacts[1]["name"] == "Bob Support"
        assert contacts[1]["has_email"] is False

    def test_titles_and_verified_filter_passed_in_payload(self):
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {"people": [
            {"name": "V", "title": "Sales", "email": "v***@x.com", "email_status": "verified"},
            {"name": "G", "title": "Sales", "email": None, "email_status": "guessed"},
        ]}
        with patch("utils.apollo_client.requests.post", return_value=_mock_response(payload)) as mpost:
            contacts = client.people_search(
                "x.com", titles=["Sales", "Customer Service"], verified_email_only=True
            )
        sent = mpost.call_args.kwargs["json"]
        assert sent["person_titles"] == ["Sales", "Customer Service"]
        assert sent["contact_email_status"] == ["verified"]
        # verified_email_only drops the guessed contact.
        assert [c["name"] for c in contacts] == ["V"]

    def test_empty_people_returns_empty_list(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post", return_value=_mock_response({})):
            assert client.people_search("x.com") == []

    def test_empty_domain_returns_empty_without_http(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post") as mpost:
            assert client.people_search("") == []
        mpost.assert_not_called()

    def test_timeout_returns_empty_not_raise(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post", side_effect=requests.exceptions.Timeout):
            assert client.people_search("x.com") == []

    def test_http_error_returns_empty_not_raise(self):
        client = ApolloClient(api_key=_TEST_KEY)
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.exceptions.HTTPError("500")
        with patch("utils.apollo_client.requests.post", return_value=bad):
            assert client.people_search("x.com") == []


# ---------------------------------------------------------------------------
# Missing key / config — no-op cleanly
# ---------------------------------------------------------------------------

class TestNoKeyNoOp:
    def test_disabled_when_no_key(self, monkeypatch):
        monkeypatch.delenv("APOLLO_API_KEY", raising=False)
        client = ApolloClient()
        assert client.enabled is False

    def test_enabled_reads_env(self, monkeypatch):
        monkeypatch.setenv("APOLLO_API_KEY", "from-env")
        assert ApolloClient().enabled is True

    def test_explicit_key_overrides_env(self, monkeypatch):
        monkeypatch.delenv("APOLLO_API_KEY", raising=False)
        assert ApolloClient(api_key="explicit").enabled is True

    def test_org_enrich_noop_without_key(self, monkeypatch):
        monkeypatch.delenv("APOLLO_API_KEY", raising=False)
        client = ApolloClient()
        with patch("utils.apollo_client.requests.get") as mget:
            assert client.org_enrich("x.com") is None
        mget.assert_not_called()

    def test_people_search_noop_without_key(self, monkeypatch):
        monkeypatch.delenv("APOLLO_API_KEY", raising=False)
        client = ApolloClient()
        with patch("utils.apollo_client.requests.post") as mpost:
            assert client.people_search("x.com") == []
        mpost.assert_not_called()
