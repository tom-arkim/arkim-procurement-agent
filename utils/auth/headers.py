"""
utils/auth/headers.py — Arkim cross-service header + env constants.

Ported from core's app/arkim_headers.py so the procurement service speaks the
same header protocol as the rest of the platform. Header names are the wire
contract (case-insensitive per HTTP, but kept canonical here); env names match
core EXACTLY (core uses COGNITO_ISSUER_URL / COGNITO_CLIENT_ID, not the
AWS_COGNITO_* spellings seen elsewhere).
"""

from __future__ import annotations

# Wire headers (mirror core).
HEADER_AUTHORIZATION: str = "Authorization"
HEADER_COMPANY_ID: str = "X-Arkim-CompanyId"        # active tenant (company PIN) for the request
HEADER_SERVICE_NAME: str = "X-Arkim-Service-Name"   # calling service, when service-signed
HEADER_SERVICE_SIGNATURE: str = "X-Arkim-Service-Signature"  # shared secret for s2s elevation

# Environment variable names (core's exact names).
ENV_COGNITO_ISSUER_URL: str = "COGNITO_ISSUER_URL"
ENV_COGNITO_CLIENT_ID: str = "COGNITO_CLIENT_ID"
ENV_INTERNAL_REQUEST_SIGNATURE: str = "INTERNAL_REQUEST_SIGNATURE"
