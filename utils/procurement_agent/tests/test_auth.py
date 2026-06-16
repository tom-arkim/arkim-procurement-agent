"""
Tests for utils/auth — the ported Cognito JWT auth (get_caller dependency).

All crypto is real but LOCAL: a per-module RSA keypair signs tokens; the JWKS is
mocked (get_jwks patched) so there is NO network and NO live Cognito. Mirrors the
suite's offline discipline.

Covers: RS256 verify (issuer/aud/exp/token_use, id-vs-access aud policy), claim
parsing (incl. the new custom:companies_procurement + multi-role), the active-
tenant binding (X-Arkim-CompanyId must be token-granted), the admin-superset role
check, and the service-signature elevation (bypasses ROLE only, fail-closed).
"""

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from jose import jwk, jwt

from utils.auth import (
    HEADER_COMPANY_ID, ROLE_ADMIN, ROLE_MONITORING, ROLE_PROCUREMENT, ROLE_TECHNICIAN,
    JWTValidationError, check_company_role, get_caller, parse_cognito_user_from_claims,
    require_role, validate_jwt_token,
)
from utils.auth import validation as _validation

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL"
CLIENT_ID = "test-client-id"
KID = "test-key-1"


def _pem() -> str:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="module")
def keys() -> dict:
    """A signing key (in the JWKS) + an unrelated key (NOT in the JWKS, for the
    bad-signature case)."""
    private_pem = _pem()
    pub_jwk = jwk.construct(private_pem, algorithm="RS256").public_key().to_dict()
    pub_jwk["kid"] = KID
    pub_jwk["alg"] = "RS256"
    pub_jwk["use"] = "sig"
    return {"private_pem": private_pem, "private_pem_unrelated": _pem(), "jwks": [pub_jwk]}


@pytest.fixture
def auth_env(monkeypatch, keys) -> dict:
    """Set the COGNITO_* env get_caller reads + patch JWKS to the local key (no network)."""
    monkeypatch.setenv("COGNITO_ISSUER_URL", ISSUER)
    monkeypatch.setenv("COGNITO_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(_validation, "get_jwks", lambda issuer, force=False: keys["jwks"])
    return keys


def _token(private_pem, *, token_use="id", aud=CLIENT_ID, iss=ISSUER,
           exp_delta=3600, kid=KID, **extra) -> str:
    now = int(time.time())
    claims = {"sub": "user-abc", "email": "tom@arkim.ai", "token_use": token_use,
              "iss": iss, "iat": now, "exp": now + exp_delta}
    if token_use == "id":
        claims["aud"] = aud
    else:
        claims["client_id"] = aud   # access tokens carry client_id, not aud
    claims.update(extra)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


def _call(*, authorization=None, company_header=None, service_signature=None, service_name=None):
    """Invoke get_caller directly with explicit args (bypasses FastAPI's Header
    sentinel defaults, which are not None when called outside a request)."""
    return get_caller(authorization=authorization, company_header=company_header,
                      service_signature=service_signature, service_name=service_name)


# ---------------------------------------------------------------------------
# JWT validation (port of _validate_jwt_token)
# ---------------------------------------------------------------------------

class TestValidateJwt:
    def test_valid_id_token_checks_aud(self, auth_env):
        claims = validate_jwt_token(_token(auth_env["private_pem"], token_use="id"),
                                    issuer=ISSUER, client_id=CLIENT_ID)
        assert claims["sub"] == "user-abc" and claims["aud"] == CLIENT_ID

    def test_valid_access_token_skips_aud(self, auth_env):
        # No aud claim on an access token; it must still validate (aud skipped).
        claims = validate_jwt_token(_token(auth_env["private_pem"], token_use="access"),
                                    issuer=ISSUER, client_id=CLIENT_ID)
        assert claims["token_use"] == "access" and "aud" not in claims

    def test_expired_401(self, auth_env):
        with pytest.raises(JWTValidationError):
            validate_jwt_token(_token(auth_env["private_pem"], exp_delta=-30),
                               issuer=ISSUER, client_id=CLIENT_ID)

    def test_bad_signature_401(self, auth_env):
        # Signed by an unrelated key but kid points at the JWKS key -> verify fails.
        tok = _token(auth_env["private_pem_unrelated"], kid=KID)
        with pytest.raises(JWTValidationError):
            validate_jwt_token(tok, issuer=ISSUER, client_id=CLIENT_ID)

    def test_wrong_issuer_401(self, auth_env):
        with pytest.raises(JWTValidationError):
            validate_jwt_token(_token(auth_env["private_pem"], iss="https://evil.example"),
                               issuer=ISSUER, client_id=CLIENT_ID)

    def test_wrong_aud_401(self, auth_env):
        with pytest.raises(JWTValidationError):
            validate_jwt_token(_token(auth_env["private_pem"], aud="other-client"),
                               issuer=ISSUER, client_id=CLIENT_ID)

    def test_invalid_token_use_401(self, auth_env):
        with pytest.raises(JWTValidationError):
            validate_jwt_token(_token(auth_env["private_pem"], token_use="refresh"),
                               issuer=ISSUER, client_id=CLIENT_ID)

    def test_unknown_kid_401(self, monkeypatch, auth_env):
        monkeypatch.setattr(_validation, "get_jwks", lambda issuer, force=False: [])
        with pytest.raises(JWTValidationError):
            validate_jwt_token(_token(auth_env["private_pem"]), issuer=ISSUER, client_id=CLIENT_ID)

    def test_malformed_401(self, auth_env):
        with pytest.raises(JWTValidationError):
            validate_jwt_token("not.a.jwt", issuer=ISSUER, client_id=CLIENT_ID)


# ---------------------------------------------------------------------------
# Claim parsing (port of parse_cognito_user_from_claims) + admin-superset check
# ---------------------------------------------------------------------------

class TestParseAndRoleCheck:
    def test_csv_claims_parsed_to_sets(self):
        u = parse_cognito_user_from_claims({
            "sub": "u1", "email": "a@b.com",
            "companies_admin": "PIN1, PIN2", "companies_technician": "PIN3",
            "custom:companies_procurement": "PIN1",
        })
        assert u.user_id == "u1"
        assert u.companies_admin == frozenset({"PIN1", "PIN2"})
        assert u.companies_technician == frozenset({"PIN3"})
        assert u.companies_procurement == frozenset({"PIN1"})
        assert u.companies_monitoring == frozenset()

    def test_procurement_absent_is_empty_no_error(self):
        u = parse_cognito_user_from_claims({"sub": "u1"})
        assert u.companies_procurement == frozenset()   # valid: no one holds the role yet

    def test_email_falls_back_to_cognito_username(self):
        u = parse_cognito_user_from_claims({"sub": "u1", "cognito:username": "bob"})
        assert u.email == "bob"

    def test_multiple_roles_same_company(self):
        u = parse_cognito_user_from_claims({
            "sub": "u1", "companies_admin": "PIN1", "companies_technician": "PIN1"})
        assert u.roles_in_company("PIN1") == frozenset({ROLE_ADMIN, ROLE_TECHNICIAN})

    def test_admin_is_superset(self):
        u = parse_cognito_user_from_claims({"sub": "u", "companies_admin": "PIN1"})
        assert check_company_role(u, "PIN1", ROLE_PROCUREMENT) is True
        assert check_company_role(u, "PIN1", ROLE_MONITORING) is True
        assert check_company_role(u, "PIN1", ROLE_TECHNICIAN) is True
        assert check_company_role(u, "PIN1", None) is True

    def test_specific_role_and_no_cross_company(self):
        u = parse_cognito_user_from_claims({"sub": "u", "custom:companies_procurement": "PIN1"})
        assert check_company_role(u, "PIN1", ROLE_PROCUREMENT) is True
        assert check_company_role(u, "PIN1", ROLE_TECHNICIAN) is False
        assert check_company_role(u, "PIN2", None) is False


# ---------------------------------------------------------------------------
# get_caller — token validation + active-tenant binding
# ---------------------------------------------------------------------------

class TestGetCaller:
    def test_no_token_returns_none(self, auth_env):
        assert _call(authorization=None) is None

    def test_valid_token_binds_company_and_roles(self, auth_env):
        tok = _token(auth_env["private_pem"], **{"custom:companies_procurement": "PIN1"})
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1")
        assert caller is not None
        assert caller.user_id == "user-abc" and caller.email == "tom@arkim.ai"
        assert caller.company_id == "PIN1"
        assert ROLE_PROCUREMENT in caller.roles
        assert caller.is_admin is False and caller.service_authenticated is False

    def test_invalid_token_401(self, auth_env):
        with pytest.raises(HTTPException) as ei:
            _call(authorization="Bearer not.a.jwt")
        assert ei.value.status_code == 401

    def test_company_not_granted_403(self, auth_env):
        tok = _token(auth_env["private_pem"], companies_admin="PIN1")
        with pytest.raises(HTTPException) as ei:
            _call(authorization=f"Bearer {tok}", company_header="PIN2")
        assert ei.value.status_code == 403

    def test_admin_in_company_sets_is_admin(self, auth_env):
        tok = _token(auth_env["private_pem"], companies_admin="PIN1")
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1")
        assert caller.is_admin is True and ROLE_ADMIN in caller.roles


# ---------------------------------------------------------------------------
# Service signature elevation + require_role enforcement
# ---------------------------------------------------------------------------

class TestServiceSignatureAndRequireRole:
    def test_valid_signature_bypasses_role_keeps_company(self, auth_env, monkeypatch):
        monkeypatch.setenv("INTERNAL_REQUEST_SIGNATURE", "sig-secret")
        # User has ONLY technician in PIN1 (not procurement) — service auth elevates.
        tok = _token(auth_env["private_pem"], companies_technician="PIN1")
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1",
                       service_signature="sig-secret", service_name="core")
        assert caller.service_authenticated is True and caller.service_name == "core"
        # require procurement -> bypassed (company access already validated).
        assert require_role(ROLE_PROCUREMENT)(caller=caller) is caller

    def test_wrong_signature_no_bypass(self, auth_env, monkeypatch):
        monkeypatch.setenv("INTERNAL_REQUEST_SIGNATURE", "sig-secret")
        tok = _token(auth_env["private_pem"], companies_technician="PIN1")
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1",
                       service_signature="WRONG", service_name="core")
        assert caller.service_authenticated is False and caller.service_name is None
        with pytest.raises(HTTPException) as ei:
            require_role(ROLE_PROCUREMENT)(caller=caller)
        assert ei.value.status_code == 403   # technician != procurement, no elevation

    def test_signature_unset_never_bypasses(self, auth_env, monkeypatch):
        monkeypatch.delenv("INTERNAL_REQUEST_SIGNATURE", raising=False)
        tok = _token(auth_env["private_pem"], companies_technician="PIN1")
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1",
                       service_signature="anything")
        assert caller.service_authenticated is False   # fail-closed

    def test_require_role_no_caller_401(self):
        with pytest.raises(HTTPException) as ei:
            require_role(ROLE_PROCUREMENT)(caller=None)
        assert ei.value.status_code == 401

    def test_require_role_missing_company_400(self, auth_env):
        tok = _token(auth_env["private_pem"], **{"custom:companies_procurement": "PIN1"})
        caller = _call(authorization=f"Bearer {tok}")   # no company header
        assert caller.company_id is None
        with pytest.raises(HTTPException) as ei:
            require_role(ROLE_PROCUREMENT, require_company=True)(caller=caller)
        assert ei.value.status_code == 400

    def test_require_role_passes_with_role(self, auth_env):
        tok = _token(auth_env["private_pem"], **{"custom:companies_procurement": "PIN1"})
        caller = _call(authorization=f"Bearer {tok}", company_header="PIN1")
        assert require_role(ROLE_PROCUREMENT)(caller=caller) is caller
