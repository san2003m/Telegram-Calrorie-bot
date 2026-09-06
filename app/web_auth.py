from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

import jwt

logger = logging.getLogger(__name__)


class AccessVerificationError(ValueError):
    pass


@dataclass(frozen=True)
class AccessIdentity:
    email: str
    subject: str


class AccessTokenVerifier(Protocol):
    async def verify(self, token: str) -> AccessIdentity: ...


def normalize_team_domain(value: str) -> str:
    candidate = value.strip().rstrip("/")
    if candidate and "://" not in candidate:
        candidate = f"https://{candidate}"
    parsed = urlsplit(candidate)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Cloudflare Access team domain 형식이 올바르지 않습니다.") from exc
    hostname = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username
        or parsed.password
        or port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not hostname.endswith(".cloudflareaccess.com")
    ):
        raise ValueError("Cloudflare Access team domain 형식이 올바르지 않습니다.")
    return f"https://{hostname}"


class CloudflareAccessVerifier:
    def __init__(
        self,
        *,
        team_domain: str,
        audience: str,
        allowed_email: str,
    ) -> None:
        self.team_domain = normalize_team_domain(team_domain)
        self.audience = audience.strip()
        self.allowed_email = allowed_email.strip().casefold()
        if not self.audience:
            raise ValueError("Cloudflare Access AUD가 비어 있습니다.")
        if not self.allowed_email:
            raise ValueError("Cloudflare Access 허용 이메일이 비어 있습니다.")
        self._jwk_client = jwt.PyJWKClient(
            f"{self.team_domain}/cdn-cgi/access/certs",
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=21_600,
            timeout=5,
        )

    def _verify_sync(self, token: str) -> AccessIdentity:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256":
                raise AccessVerificationError("지원하지 않는 Access 서명 방식입니다.")
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self.audience,
                issuer=self.team_domain,
                options={"require": ["aud", "exp", "iat", "iss", "sub", "type"]},
            )
        except AccessVerificationError:
            raise
        except Exception as exc:
            logger.warning("Cloudflare Access JWT verification failed: %s", type(exc).__name__)
            raise AccessVerificationError("Access 인증을 확인하지 못했습니다.") from exc

        email = str(claims.get("email") or "").strip()
        subject = str(claims.get("sub") or "").strip()
        if claims.get("type") != "app" or not subject or not email:
            raise AccessVerificationError("사용자 Access 토큰이 아닙니다.")
        if email.casefold() != self.allowed_email:
            raise AccessVerificationError("허용된 계정이 아닙니다.")
        return AccessIdentity(email=email, subject=subject)

    async def verify(self, token: str) -> AccessIdentity:
        if not token or len(token) > 16_384:
            raise AccessVerificationError("Access 토큰이 없습니다.")
        return await asyncio.to_thread(self._verify_sync, token)
