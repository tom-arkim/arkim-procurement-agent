"""
utils/auth/cognito.py — the CognitoUser identity model + claim parsing.

Ported from core's app/models/cognito.py. A Cognito JWT carries the caller's
company memberships as CSV claims, one per role: companies_admin /
companies_monitoring / companies_technician, plus the NEW
`custom:companies_procurement` (read now; core issues it later — an absent claim
is valid and simply means no one holds the procurement role yet).

A user may appear in several CSV lists (multiple roles, multiple companies) —
that falls out of the set model with no special handling. Admin-superset logic
lives in dependencies.check_company_role, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Role labels (procurement is the fourth role; admin is the superset — see
# dependencies.check_company_role).
ROLE_ADMIN: str = "admin"
ROLE_MONITORING: str = "monitoring"
ROLE_TECHNICIAN: str = "technician"
ROLE_PROCUREMENT: str = "procurement"

# Claim keys for each role's CSV company list (mirror core; procurement is the
# custom: namespaced claim).
_CLAIM_ADMIN: str = "companies_admin"
_CLAIM_MONITORING: str = "companies_monitoring"
_CLAIM_TECHNICIAN: str = "companies_technician"
_CLAIM_PROCUREMENT: str = "custom:companies_procurement"


def _parse_csv_pins(raw: object) -> frozenset[str]:
    """Parse a CSV (or already-list) claim into a set of trimmed company PINs.
    None / "" / missing -> empty set (valid — the user simply holds no companies
    under that role)."""
    if not raw:
        return frozenset()
    items: list[str]
    if isinstance(raw, (list, tuple, set, frozenset)):
        items = [str(x) for x in raw]
    else:
        items = str(raw).split(",")
    return frozenset(p.strip() for p in items if p and p.strip())


@dataclass(frozen=True)
class CognitoUser:
    """The authenticated caller as carried by the Cognito JWT (all companies/roles
    the token grants, before any single active-tenant binding)."""
    user_id: str
    email: Optional[str]
    companies_admin: frozenset[str]
    companies_monitoring: frozenset[str]
    companies_technician: frozenset[str]
    companies_procurement: frozenset[str]

    def companies_with_any_role(self) -> frozenset[str]:
        """Union of every company PIN the user has ANY role in."""
        return (self.companies_admin | self.companies_monitoring
                | self.companies_technician | self.companies_procurement)

    def roles_in_company(self, company_id: str) -> frozenset[str]:
        """The set of role labels the user holds in one company (no admin-superset
        expansion — that's a check concern, see check_company_role)."""
        roles: set[str] = set()
        if company_id in self.companies_admin:
            roles.add(ROLE_ADMIN)
        if company_id in self.companies_monitoring:
            roles.add(ROLE_MONITORING)
        if company_id in self.companies_technician:
            roles.add(ROLE_TECHNICIAN)
        if company_id in self.companies_procurement:
            roles.add(ROLE_PROCUREMENT)
        return frozenset(roles)


def parse_cognito_user_from_claims(claims: dict) -> CognitoUser:
    """Build a CognitoUser from verified JWT claims (port of
    parse_cognito_user_from_claims). email falls back through
    email -> username -> cognito:username (mirror core)."""
    return CognitoUser(
        user_id=claims.get("sub") or "",
        email=(claims.get("email") or claims.get("username")
               or claims.get("cognito:username")),
        companies_admin=_parse_csv_pins(claims.get(_CLAIM_ADMIN)),
        companies_monitoring=_parse_csv_pins(claims.get(_CLAIM_MONITORING)),
        companies_technician=_parse_csv_pins(claims.get(_CLAIM_TECHNICIAN)),
        companies_procurement=_parse_csv_pins(claims.get(_CLAIM_PROCUREMENT)),
    )
