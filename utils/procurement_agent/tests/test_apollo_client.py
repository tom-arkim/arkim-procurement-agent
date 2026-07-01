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
# _clean_domain — normalize to bare host (consistent with supplier_registry)
# ---------------------------------------------------------------------------

class TestCleanDomain:
    def test_strips_www(self):
        assert ApolloClient._clean_domain("www.mescocorp.com") == "mescocorp.com"

    def test_strips_scheme_and_path(self):
        assert ApolloClient._clean_domain("https://www.x.com/path") == "x.com"

    def test_strips_scheme_without_www(self):
        assert ApolloClient._clean_domain("http://x.com") == "x.com"

    def test_lowercases(self):
        assert ApolloClient._clean_domain("X.COM") == "x.com"

    def test_bare_domain_idempotent(self):
        assert ApolloClient._clean_domain("mescocorp.com") == "mescocorp.com"

    def test_surrounding_whitespace_and_www(self):
        assert ApolloClient._clean_domain("  www.x.com  ") == "x.com"

    def test_empty_stays_empty(self):
        assert ApolloClient._clean_domain("") == ""
        assert ApolloClient._clean_domain(None) == ""

    def test_matches_supplier_registry_normalization(self):
        """Client and store must produce the same key so cache lookups align."""
        from utils.supplier_registry import _normalize_domain
        for d in ("www.mescocorp.com", "https://www.x.com/path", "http://x.com",
                  "X.COM", "mescocorp.com", "  www.x.com  ", ""):
            assert ApolloClient._clean_domain(d) == _normalize_domain(d)


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

    def test_sends_bare_domain_stripping_www_and_scheme(self):
        """The www./scheme are stripped before the request hits Apollo."""
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {"organization": {"name": "Mesco"}}
        with patch("utils.apollo_client.requests.get", return_value=_mock_response(payload)) as mget:
            client.org_enrich("https://www.mescocorp.com/about")
        assert mget.call_args.kwargs["params"]["domain"] == "mescocorp.com"

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
                {"id": "p1", "name": "Jane Sales", "first_name": "Jane", "last_name": "Sales",
                 "title": "Sales Manager", "seniority": "manager",
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
            "person_id": "p1",
            "name": "Jane Sales",
            "first_name": "Jane",
            "last_name": "Sales",
            "title": "Sales Manager",
            "seniority": "manager",
            "email_status": "verified",
            "has_email": True,
            "masked_email": "ja***@phoenixpumps.com",
        }
        # Name composed from first/last when 'name' absent.
        assert contacts[1]["name"] == "Bob Support"
        assert contacts[1]["has_email"] is False
        # Domain goes out as the list param + include_similar_titles present.
        sent = mpost.call_args.kwargs["json"]
        assert sent["q_organization_domains_list"] == ["phoenixpumps.com"]
        assert "include_similar_titles" in sent

    def test_uses_api_search_endpoint_not_deprecated(self):
        """The legacy mixed_people/search is deprecated for API callers (HTTP 422);
        the client must hit mixed_people/api_search."""
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post", return_value=_mock_response({})) as mpost:
            client.people_search("x.com")
        url = mpost.call_args.args[0] if mpost.call_args.args else mpost.call_args.kwargs.get("url")
        assert url.endswith("/mixed_people/api_search")
        assert not url.endswith("/mixed_people/search")

    def test_parses_lean_api_search_shape(self):
        """api_search returns a leaner person: id / first_name / title / has_email,
        with last_name obfuscated and no email/seniority. Parse it into the stable
        contact contract (person_id present for the later enrich; has_email honored)."""
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {
            "total_entries": 2,
            "people": [
                {"id": "p1", "first_name": "Jeff", "last_name_obfuscated": "S.",
                 "title": "Inside Sales", "has_email": True, "organization": {"name": "Bay Power"}},
                {"id": "p2", "first_name": "Dana", "last_name": "Reed",
                 "title": "Account Executive", "has_email": False},
            ],
        }
        with patch("utils.apollo_client.requests.post", return_value=_mock_response(payload)):
            contacts = client.people_search("baypower.com")

        assert contacts[0] == {
            "person_id": "p1",
            "name": "Jeff",            # only first_name available (last name obfuscated)
            "first_name": "Jeff",
            "last_name": None,
            "title": "Inside Sales",
            "seniority": None,
            "email_status": None,
            "has_email": True,         # taken from Apollo's explicit boolean
            "masked_email": None,      # api_search returns no email
        }
        # Falls back to composing name when a plain last_name happens to be present.
        assert contacts[1]["name"] == "Dana Reed"
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
# people_match (People Enrichment — 1 credit; reveals email)
# ---------------------------------------------------------------------------

class TestPeopleMatch:
    def test_hit_returns_email(self):
        client = ApolloClient(api_key=_TEST_KEY)
        payload = {"person": {"id": "p1", "name": "Jane Sales", "title": "Sales Manager",
                              "email": "jane@phoenixpumps.com", "email_status": "verified"}}
        with patch("utils.apollo_client.requests.post", return_value=_mock_response(payload)) as mpost:
            res = client.people_match(person_id="p1", domain="phoenixpumps.com")
        mpost.assert_called_once()
        assert res == {"name": "Jane Sales", "title": "Sales Manager",
                       "email": "jane@phoenixpumps.com", "email_status": "verified", "person_id": "p1"}
        assert mpost.call_args.kwargs["json"]["id"] == "p1"

    def test_miss_no_email_returns_none(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post",
                   return_value=_mock_response({"person": {"id": "p1", "email": None}})):
            assert client.people_match(person_id="p1") is None
        with patch("utils.apollo_client.requests.post", return_value=_mock_response({})):
            assert client.people_match(person_id="p1") is None

    def test_no_identifiers_no_http(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post") as mpost:
            assert client.people_match() is None
        mpost.assert_not_called()

    def test_timeout_and_http_error_return_none(self):
        client = ApolloClient(api_key=_TEST_KEY)
        with patch("utils.apollo_client.requests.post", side_effect=requests.exceptions.Timeout):
            assert client.people_match(person_id="p1") is None
        bad = MagicMock()
        bad.raise_for_status.side_effect = requests.exceptions.HTTPError("429")
        with patch("utils.apollo_client.requests.post", return_value=bad):
            assert client.people_match(person_id="p1") is None

    def test_noop_without_key(self, monkeypatch):
        monkeypatch.delenv("APOLLO_API_KEY", raising=False)
        client = ApolloClient()
        with patch("utils.apollo_client.requests.post") as mpost:
            assert client.people_match(person_id="p1") is None
        mpost.assert_not_called()


# ---------------------------------------------------------------------------
# Missing key / config — no-op cleanly
# ---------------------------------------------------------------------------

class TestNoKeyNoOp:
    def test_default_client_disabled_in_test_session(self):
        """No per-test env handling: proves the autouse conftest neutralizer makes a
        default ApolloClient() disabled, so no real key can leak into the suite."""
        assert ApolloClient().enabled is False

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
