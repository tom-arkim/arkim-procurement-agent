"""
utils/auth/validation.py — Cognito JWT (RS256) verification against JWKS.

Ported from core's app/middleware/auth_middleware._validate_jwt_token. Fetches
the issuer's JWKS, verifies the RS256 signature, and checks the standard claims:
  - signature against the JWK whose kid matches the token header (one forced
    JWKS refresh on a kid miss, to tolerate key rotation);
  - exp (expiry);
  - iss == COGNITO_ISSUER_URL;
  - token_use in {"id", "access"};
  - aud == COGNITO_CLIENT_ID for id tokens ONLY (access tokens skip aud — they
    carry client_id, not aud, mirroring core).

Any failure raises JWTValidationError; the FastAPI dependency maps that to 401.
JWKS is cached in-process with a TTL (mirror core). All crypto/JWKS is mockable
(tests patch get_jwks) so the suite needs no network or live Cognito.
"""

from __future__ import annotations

import threading
import time

import httpx
from jose import jwt
from jose.exceptions import JWTError


class JWTValidationError(Exception):
    """Raised for any token validation failure (bad signature, expired, wrong
    issuer/aud, malformed, unknown key, JWKS unreachable)."""


# In-process JWKS cache: issuer -> {"keys": [...], "fetched_at": epoch}. Mirrors
# core's caching so we don't refetch on every request.
_JWKS_TTL_SECONDS: int = 3600
_jwks_cache: dict[str, dict] = {}
_jwks_lock = threading.Lock()


def _jwks_url(issuer: str) -> str:
    return f"{issuer.rstrip('/')}/.well-known/jwks.json"


def get_jwks(issuer: str, *, force: bool = False) -> list[dict]:
    """Return the issuer's JWKS key list, cached with a TTL. `force` bypasses the
    cache (used once on a kid miss for key rotation). Network errors propagate to
    the caller (validate_jwt_token converts them to a JWTValidationError)."""
    with _jwks_lock:
        cached = _jwks_cache.get(issuer)
        if cached and not force and (time.time() - cached["fetched_at"] < _JWKS_TTL_SECONDS):
            return cached["keys"]
    resp = httpx.get(_jwks_url(issuer), timeout=5.0)
    resp.raise_for_status()
    keys = resp.json().get("keys", [])
    with _jwks_lock:
        _jwks_cache[issuer] = {"keys": keys, "fetched_at": time.time()}
    return keys


def _find_key(issuer: str, kid: str) -> dict | None:
    """The JWK for a kid, refreshing the JWKS once on a miss (key rotation)."""
    keys = get_jwks(issuer)
    key = next((k for k in keys if k.get("kid") == kid), None)
    if key is None:
        keys = get_jwks(issuer, force=True)
        key = next((k for k in keys if k.get("kid") == kid), None)
    return key


def validate_jwt_token(token: str, *, issuer: str, client_id: str) -> dict:
    """Verify a Cognito JWT and return its claims, or raise JWTValidationError.

    issuer / client_id come from COGNITO_ISSUER_URL / COGNITO_CLIENT_ID.
    """
    if not token:
        raise JWTValidationError("missing token")
    if not issuer:
        raise JWTValidationError("issuer not configured")

    try:
        header = jwt.get_unverified_headers(token)
        unverified = jwt.get_unverified_claims(token)
    except JWTError as exc:
        raise JWTValidationError(f"malformed token: {exc}") from exc

    # token_use decides the aud policy and must be a Cognito token type. Read it
    # unverified to pick the policy; the signature still protects integrity.
    token_use = unverified.get("token_use")
    if token_use not in ("id", "access"):
        raise JWTValidationError(f"invalid token_use: {token_use!r}")

    try:
        key = _find_key(issuer, header.get("kid") or "")
    except Exception as exc:  # JWKS fetch failure -> cannot validate -> 401
        raise JWTValidationError(f"jwks unavailable: {exc}") from exc
    if key is None:
        raise JWTValidationError("signing key not found for kid")

    # id tokens carry aud == client_id; access tokens do not (they carry
    # client_id as a separate claim) — skip aud for them, exactly as core does.
    verify_aud = token_use == "id"
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            issuer=issuer,
            audience=client_id if verify_aud else None,
            options={"verify_aud": verify_aud},
        )
    except JWTError as exc:  # ExpiredSignatureError / JWTClaimsError / bad sig all subclass JWTError
        raise JWTValidationError(str(exc)) from exc
    return claims
