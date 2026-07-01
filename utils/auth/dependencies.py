"""
utils/auth/dependencies.py — the get_caller() FastAPI dependency + role checks.

This is the binding point the auth cluster consumes: M1 (the authenticated `sub`
as approver_id) and D2 (tenant scoping by the validated active company) read the
Caller this yields. It is a PURE dependency here — no endpoint enforces it yet.

Trust model (never relaxed):
  - identity comes ONLY from a verified Cognito JWT (validation.validate_jwt_token);
  - the active tenant comes from the X-Arkim-CompanyId header but is accepted ONLY
    if the token actually grants the caller access to that company (never a company
    id from the body);
  - service-to-service elevation requires a constant-time match against
    INTERNAL_REQUEST_SIGNATURE and bypasses ONLY the role requirement, never the
    company-access requirement. With the env secret unset, elevation is impossible
    (fail-closed, mirroring require_admin).

Admin-superset (check_company_role): admin in a company grants every role there.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException

from utils.auth.cognito import (
    ROLE_ADMIN, ROLE_MONITORING, ROLE_PROCUREMENT, ROLE_TECHNICIAN,
    CognitoUser, parse_cognito_user_from_claims,
)
from utils.auth.headers import (
    ENV_COGNITO_CLIENT_ID, ENV_COGNITO_ISSUER_URL, ENV_INTERNAL_REQUEST_SIGNATURE,
    HEADER_AUTHORIZATION, HEADER_COMPANY_ID, HEADER_SERVICE_NAME, HEADER_SERVICE_SIGNATURE,
)
from utils.auth.validation import JWTValidationError, validate_jwt_token

# role label -> the CognitoUser company-set attribute holding that role's PINs.
_ROLE_SETS: dict[str, Callable[[CognitoUser], frozenset[str]]] = {
    ROLE_ADMIN:       lambda u: u.companies_admin,
    ROLE_MONITORING:  lambda u: u.companies_monitoring,
    ROLE_TECHNICIAN:  lambda u: u.companies_technician,
    ROLE_PROCUREMENT: lambda u: u.companies_procurement,
}


@dataclass(frozen=True)
class Caller:
    """The authenticated caller bound to a single active company. Consumed by M1
    (user_id as approver_id) and D2 (company_id as the tenant scope)."""
    user_id: str
    email: Optional[str]
    company_id: Optional[str]            # active tenant PIN (validated against the token)
    roles: frozenset[str]                # role labels held in the active company
    is_admin: bool                       # admin in the active company
    service_authenticated: bool          # matched INTERNAL_REQUEST_SIGNATURE
    service_name: Optional[str]          # X-Arkim-Service-Name, when service-authenticated
    cognito_user: CognitoUser            # full identity for downstream role re-checks


def check_company_role(user: CognitoUser, company_id: str, role: Optional[str] = None) -> bool:
    """Admin-superset role check (port of _check_company_role).

    - admin in `company_id` -> True for ANY role (the superset).
    - role is None -> True if the user has ANY access to the company.
    - otherwise -> True iff the user holds that specific role in the company.
    """
    if company_id in user.companies_admin:
        return True
    if role is None:
        return company_id in user.companies_with_any_role()
    role_set = _ROLE_SETS.get(role)
    return bool(role_set) and company_id in role_set(user)


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    """Extract the bearer token, or None when absent/non-bearer."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _service_authenticated(presented: Optional[str]) -> bool:
    """True only when a non-empty presented signature constant-time-matches the
    configured INTERNAL_REQUEST_SIGNATURE. Unset secret -> always False (fail-closed)."""
    secret = os.environ.get(ENV_INTERNAL_REQUEST_SIGNATURE) or ""
    if not secret or not presented:
        return False
    return secrets.compare_digest(presented, secret)


def get_caller(
    authorization: Optional[str] = Header(default=None, alias=HEADER_AUTHORIZATION),
    company_header: Optional[str] = Header(default=None, alias=HEADER_COMPANY_ID),
    service_signature: Optional[str] = Header(default=None, alias=HEADER_SERVICE_SIGNATURE),
    service_name: Optional[str] = Header(default=None, alias=HEADER_SERVICE_NAME),
) -> Optional[Caller]:
    """Validate the Cognito JWT, bind the active tenant, and yield a Caller.

    No token -> None (the per-route dependency decides whether to 401, mirroring
    core where the middleware sets user=None and the decorator enforces presence).
    Invalid/expired/malformed token -> 401. Active company present but NOT granted
    by the token -> 403.
    """
    token = _bearer_token(authorization)
    if not token:
        return None  # unauthenticated; require_role(...) enforces presence per route

    issuer = os.environ.get(ENV_COGNITO_ISSUER_URL) or ""
    client_id = os.environ.get(ENV_COGNITO_CLIENT_ID) or ""
    try:
        claims = validate_jwt_token(token, issuer=issuer, client_id=client_id)
    except JWTValidationError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    user = parse_cognito_user_from_claims(claims)
    svc_auth = _service_authenticated(service_signature)

    company_id = (company_header or "").strip() or None
    if company_id is not None and not check_company_role(user, company_id, role=None):
        # Active tenant must be one the TOKEN grants — never trust the header alone.
        raise HTTPException(status_code=403, detail="No access to the requested company")

    return Caller(
        user_id=user.user_id,
        email=user.email,
        company_id=company_id,
        roles=user.roles_in_company(company_id) if company_id else frozenset(),
        is_admin=bool(company_id) and company_id in user.companies_admin,
        service_authenticated=svc_auth,
        service_name=service_name if svc_auth else None,
        cognito_user=user,
    )


def require_role(
    role: Optional[str] = None,
    *,
    require_company: bool = True,
) -> Callable[..., Caller]:
    """FastAPI dependency factory mirroring core's @require_authentication.

    Endpoints declare their need, e.g. `Depends(require_role(ROLE_PROCUREMENT))`.
    Enforces: authenticated (else 401); active company present when require_company
    (else 400); and the role in the active company (admin-superset) UNLESS the
    request is service-authenticated (a matching INTERNAL_REQUEST_SIGNATURE bypasses
    the ROLE check only — company access was already validated in get_caller).
    """
    def _dependency(caller: Optional[Caller] = Depends(get_caller)) -> Caller:
        if caller is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        if require_company and not caller.company_id:
            raise HTTPException(status_code=400, detail=f"Missing {HEADER_COMPANY_ID}")
        if caller.service_authenticated:
            return caller  # s2s elevation: role bypassed, company access already checked
        if role is not None and not check_company_role(
            caller.cognito_user, caller.company_id or "", role
        ):
            raise HTTPException(status_code=403, detail=f"Role '{role}' required")
        return caller

    return _dependency
