"""
utils/auth — Cognito JWT auth for the procurement FastAPI surface.

Ported from core (no shared auth library exists). Public surface: get_caller (the
pure dependency) + require_role (per-route enforcement), the Caller/CognitoUser
models, check_company_role (admin-superset), validate_jwt_token, and the role/
header constants. No endpoint enforces these yet — M1/D2 consume them next.
"""

from __future__ import annotations

from utils.auth.cognito import (
    ROLE_ADMIN, ROLE_MONITORING, ROLE_PROCUREMENT, ROLE_TECHNICIAN,
    CognitoUser, parse_cognito_user_from_claims,
)
from utils.auth.dependencies import (
    Caller, check_company_role, get_caller, require_role,
)
from utils.auth.headers import (
    HEADER_AUTHORIZATION, HEADER_COMPANY_ID, HEADER_SERVICE_NAME, HEADER_SERVICE_SIGNATURE,
)
from utils.auth.validation import JWTValidationError, get_jwks, validate_jwt_token

__all__ = [
    "Caller", "CognitoUser", "parse_cognito_user_from_claims",
    "get_caller", "require_role", "check_company_role",
    "validate_jwt_token", "JWTValidationError", "get_jwks",
    "ROLE_ADMIN", "ROLE_MONITORING", "ROLE_TECHNICIAN", "ROLE_PROCUREMENT",
    "HEADER_AUTHORIZATION", "HEADER_COMPANY_ID", "HEADER_SERVICE_NAME",
    "HEADER_SERVICE_SIGNATURE",
]
