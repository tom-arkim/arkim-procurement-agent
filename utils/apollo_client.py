"""
utils/apollo_client.py
Thin Apollo.io API client — organization enrich + people search.

UNWIRED in this step: nothing in the live sourcing flow, the supplier store, or
contact resolution calls this yet. It exists so the later Tier 3 clarifier can
import it.

Conventions are copied from the existing sourcing layer rather than invented:
  - APOLLO_API_KEY read from the environment, same as TAVILY_API_KEY /
    ANTHROPIC_API_KEY (see utils/sourcing_archieved/__init__.py). The key is also
    accepted as a constructor argument, mirroring SourcingAgent.__init__.
  - HTTP via `requests` (same library as utils/sourcing_archieved/llm_parsing.py),
    with an explicit timeout and raise_for_status().
  - Bracket-prefixed print logging ("[Apollo] ...") like [Sourcing] /
    [SupplierRegistry].

Fail-soft by contract: every method returns None / [] and logs on ANY error
(missing key, timeout, rate limit, HTTP error, malformed payload) and NEVER
raises into a caller — so the later clarifier can degrade to the existing
heuristic when Apollo is unavailable or unconfigured.

Credit model:
  - org_enrich  : 1 Apollo credit on a match, 0 on a miss.
  - people_search: free (mixed_people/api_search), and returns NO email at all. This
    step only surfaces who works there (person_id/first_name/title/has_email);
    revealing an email requires people_match (1 credit).
"""

import os
from typing import Optional

import requests

_ORG_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"
# NOTE: the legacy mixed_people/search endpoint is deprecated for API callers
# (returns HTTP 422 "deprecated for API callers"). The current People Search API is
# mixed_people/api_search. Its response is leaner: people carry id / first_name /
# last_name_obfuscated / title / has_email / organization, and DO NOT include a
# plain last_name, seniority, or any email (masked or otherwise).
_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
_PEOPLE_MATCH_URL = "https://api.apollo.io/api/v1/people/match"

_TIMEOUT = 30  # seconds — matches _anthropic_complete in llm_parsing.py


class ApolloClient:
    """Thin, fail-soft wrapper over the two Apollo endpoints the clarifier needs."""

    def __init__(self, api_key: Optional[str] = None):
        # Mirror SourcingAgent.__init__: accept an explicit key, else read env.
        self._api_key = (
            api_key if api_key is not None else os.environ.get("APOLLO_API_KEY")
        )

    @property
    def enabled(self) -> bool:
        """True when an API key is configured. When False, all methods no-op."""
        return bool(self._api_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": self._api_key,
        }

    @staticmethod
    def _clean_domain(domain: Optional[str]) -> str:
        """Normalize to a bare host: lowercase, no scheme/path, no leading www.

        Mirrors supplier_registry._normalize_domain so the client and the store
        agree on the same domain key (store-check-first lookups match). Apollo's
        org-enrich requires a bare domain (no scheme, no www.). Replicated rather
        than imported to keep this client standalone (no store dependency).
        """
        raw = (domain or "").lower().strip()
        if not raw:
            return ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
            host = parsed.hostname or raw
        except Exception:
            host = raw
        if host.startswith("www."):
            host = host[4:]
        return host.strip()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def org_enrich(self, domain: Optional[str]) -> Optional[dict]:
        """Enrich a single organization by domain.

        Returns a structured dict on a match, or None on a miss / any error /
        unset key. 1 Apollo credit is consumed only on a match (Apollo side).

        Result keys: name, industry, country, state, raw_address, description,
        keywords (list).
        """
        if not self.enabled:
            print("[Apollo] org_enrich skipped -- APOLLO_API_KEY unset")
            return None

        clean = self._clean_domain(domain)
        if not clean:
            print("[Apollo] org_enrich skipped -- empty domain")
            return None

        try:
            resp = requests.get(
                _ORG_ENRICH_URL,
                headers=self._headers(),
                params={"domain": clean},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            print(f"[Apollo] org_enrich failed for {clean!r}: {type(exc).__name__}: {exc}")
            return None

        org = data.get("organization") or None
        if not org:
            # Miss — 0 credits consumed.
            print(f"[Apollo] org_enrich miss for {clean!r}")
            return None

        return {
            "name": org.get("name"),
            "industry": org.get("industry"),
            "country": org.get("country"),
            "state": org.get("state"),
            "raw_address": org.get("raw_address"),
            "description": org.get("short_description") or org.get("description"),
            "keywords": org.get("keywords") or [],
        }

    def people_search(
        self,
        domain: Optional[str],
        titles: Optional[list] = None,
        verified_email_only: bool = False,
        include_similar_titles: bool = False,
    ) -> list:
        """Search people at an organization domain (mixed_people/api_search). FREE.

        Returns who works there with identity fields — but NOT a usable email
        (the search API never returns one; revealing it requires people_match, which
        costs a credit). The api_search response is lean: it carries person_id /
        first_name / title / has_email, but typically NOT a plain last_name (it is
        obfuscated), seniority, or any email. The output dict keeps the full key set
        (last_name, seniority, email_status, masked_email present but usually None)
        for a stable contract; has_email is taken from Apollo's boolean when present.

        Args:
            titles: person titles to filter on (e.g. ["sales", "account executive"]).
            include_similar_titles: let Apollo expand the title set.
            verified_email_only: keep only verified-email-status people.

        Returns [] on any error / unset key.
        """
        if not self.enabled:
            print("[Apollo] people_search skipped -- APOLLO_API_KEY unset")
            return []

        clean = self._clean_domain(domain)
        if not clean:
            print("[Apollo] people_search skipped -- empty domain")
            return []

        payload: dict = {
            "q_organization_domains_list": [clean],
            "page": 1,
            "per_page": 10,
            "include_similar_titles": bool(include_similar_titles),
        }
        if titles:
            payload["person_titles"] = list(titles)
        if verified_email_only:
            payload["contact_email_status"] = ["verified"]

        try:
            resp = requests.post(
                _PEOPLE_SEARCH_URL,
                headers=self._headers(),
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            print(f"[Apollo] people_search failed for {clean!r}: {type(exc).__name__}: {exc}")
            return []

        people = data.get("people") or []
        contacts: list = []
        for p in people:
            if not isinstance(p, dict):
                continue
            email_status = p.get("email_status")
            name = p.get("name") or " ".join(
                x for x in (p.get("first_name"), p.get("last_name")) if x
            ).strip() or None
            # api_search returns an explicit has_email boolean; fall back to the
            # legacy derivation (email present / verified status) when it's absent.
            has_email = p.get("has_email")
            if has_email is None:
                has_email = bool(p.get("email")) or email_status == "verified"
            contacts.append({
                "person_id": p.get("id"),               # used by people_match to enrich
                "name": name,
                "first_name": p.get("first_name"),
                "last_name": p.get("last_name"),        # obfuscated/absent in api_search
                "title": p.get("title"),
                "seniority": p.get("seniority"),        # used to pick the best contact
                "email_status": email_status,  # "verified" | "guessed" | "unavailable" | None
                "has_email": bool(has_email),
                "masked_email": p.get("email"),  # api_search returns no email here
            })

        if verified_email_only:
            contacts = [c for c in contacts if c["email_status"] == "verified"]

        return contacts

    def people_match(
        self,
        person_id: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> Optional[dict]:
        """Enrich ONE person (People Enrichment, /people/match) to reveal their email.

        Costs 1 Apollo credit per match. Provide person_id (preferred, from
        people_search) or first_name+last_name (+domain). Fail-soft: returns None on
        miss / error / timeout / unset key — never raises.

        Returns {name, title, email, email_status, person_id} or None.
        """
        if not self.enabled:
            print("[Apollo] people_match skipped -- APOLLO_API_KEY unset")
            return None

        payload: dict = {}
        if person_id:
            payload["id"] = person_id
        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        clean = self._clean_domain(domain)
        if clean:
            payload["domain"] = clean
        if not payload:
            print("[Apollo] people_match skipped -- no identifiers")
            return None

        try:
            resp = requests.post(
                _PEOPLE_MATCH_URL,
                headers=self._headers(),
                json=payload,
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json() or {}
        except Exception as exc:
            print(f"[Apollo] people_match failed: {type(exc).__name__}: {exc}")
            return None

        person = data.get("person") or None
        if not person or not person.get("email"):
            # Miss — no email revealed.
            print("[Apollo] people_match miss (no email revealed)")
            return None

        return {
            "name": person.get("name"),
            "title": person.get("title"),
            "email": person.get("email"),
            "email_status": person.get("email_status"),
            "person_id": person.get("id"),
        }
