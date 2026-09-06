from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.web_auth import (
    AccessVerificationError,
    CloudflareAccessVerifier,
    normalize_team_domain,
)


def test_normalize_team_domain_accepts_only_cloudflare_access_https() -> None:
    assert (
        normalize_team_domain("my-team.cloudflareaccess.com/")
        == "https://my-team.cloudflareaccess.com"
    )
    with pytest.raises(ValueError):
        normalize_team_domain("http://my-team.cloudflareaccess.com")
    with pytest.raises(ValueError):
        normalize_team_domain("https://example.com")


async def test_cloudflare_access_verifier_checks_signature_claims_and_email() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    now = datetime.now(UTC)
    claims = {
        "aud": ["expected-audience"],
        "email": "owner@example.com",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "iss": "https://my-team.cloudflareaccess.com",
        "sub": "owner-subject",
        "type": "app",
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
    verifier = CloudflareAccessVerifier(
        team_domain="my-team.cloudflareaccess.com",
        audience="expected-audience",
        allowed_email="OWNER@example.com",
    )
    verifier._jwk_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda value: SimpleNamespace(key=public_key)
    )

    identity = await verifier.verify(token)

    assert identity.email == "owner@example.com"
    assert identity.subject == "owner-subject"


async def test_cloudflare_access_verifier_rejects_wrong_email_and_algorithm() -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    now = datetime.now(UTC)
    claims = {
        "aud": ["expected-audience"],
        "email": "other@example.com",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "iss": "https://my-team.cloudflareaccess.com",
        "sub": "other-subject",
        "type": "app",
    }
    verifier = CloudflareAccessVerifier(
        team_domain="my-team.cloudflareaccess.com",
        audience="expected-audience",
        allowed_email="owner@example.com",
    )
    verifier._jwk_client = SimpleNamespace(
        get_signing_key_from_jwt=lambda value: SimpleNamespace(key=public_key)
    )
    rsa_token = jwt.encode(
        claims,
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    hmac_token = jwt.encode(claims, "not-a-real-access-key-with-32-bytes", algorithm="HS256")

    with pytest.raises(AccessVerificationError, match="허용된 계정"):
        await verifier.verify(rsa_token)
    with pytest.raises(AccessVerificationError, match="서명 방식"):
        await verifier.verify(hmac_token)
