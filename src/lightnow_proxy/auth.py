from __future__ import annotations

import time
from typing import Any

import httpx
import jwt
from jwt import PyJWTError
from jwt import PyJWKClient
from starlette.datastructures import Headers

from lightnow_proxy.config import AuthConfig


class AuthError(Exception):
    pass


class Principal:
    def __init__(self, subject: str, username: str | None, groups: set[str], claims: dict[str, Any]):
        self.subject = subject
        self.username = username
        self.groups = groups
        self.claims = claims


class TokenVerifier:
    def __init__(self, config: AuthConfig):
        self.config = config
        self._jwks_client: PyJWKClient | None = None
        self._jwks_client_expires_at = 0.0

    async def verify_headers(self, headers: Headers) -> Principal:
        if not self.config.enabled:
            return Principal(subject="anonymous", username="anonymous", groups={"*"}, claims={})

        auth_header = headers.get("authorization")
        if not auth_header or not auth_header.lower().startswith("bearer "):
            raise AuthError("missing bearer token")

        token = auth_header.split(" ", 1)[1].strip()
        if token in self.config.dev_bearer_tokens:
            return self._principal_from_claims(self.config.dev_bearer_tokens[token])

        claims = await self._decode_jwt(token)
        return self._principal_from_claims(claims)

    async def _decode_jwt(self, token: str) -> dict[str, Any]:
        client = await self._jwks()
        signing_key = client.get_signing_key_from_jwt(token)
        try:
            return self._decode_with_audience_or_authorized_party(token, signing_key.key)
        except PyJWTError as exc:
            raise AuthError(f"{exc.__class__.__name__}: {exc}") from exc

    def _decode_with_audience_or_authorized_party(self, token: str, signing_key: Any) -> dict[str, Any]:
        audiences = configured_audiences(self.config.audience)
        if not audiences:
            return jwt.decode(
                token,
                signing_key,
                algorithms=["RS256", "RS384", "RS512"],
                issuer=self.config.issuer,
                options={"verify_aud": False},
            )

        unverified_claims = jwt.decode(token, options={"verify_signature": False})
        if unverified_claims.get("aud"):
            try:
                return jwt.decode(
                    token,
                    signing_key,
                    algorithms=["RS256", "RS384", "RS512"],
                    audience=audiences,
                    issuer=self.config.issuer,
                )
            except jwt.InvalidAudienceError:
                if unverified_claims.get("azp") not in audiences:
                    raise
        elif unverified_claims.get("azp") not in audiences:
            raise jwt.MissingRequiredClaimError("aud")

        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256", "RS384", "RS512"],
            issuer=self.config.issuer,
            options={"verify_aud": False},
        )
        if claims.get("azp") not in audiences:
            raise jwt.InvalidAudienceError("authorized party is not allowed")
        return claims

    async def _jwks(self) -> PyJWKClient:
        now = time.time()
        if self._jwks_client and now < self._jwks_client_expires_at:
            return self._jwks_client

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.config.issuer.rstrip('/')}/.well-known/openid-configuration")
            response.raise_for_status()
            metadata = response.json()

        jwks_uri = metadata.get("jwks_uri")
        if not isinstance(jwks_uri, str) or not jwks_uri:
            raise AuthError("issuer metadata does not contain jwks_uri")

        self._jwks_client = PyJWKClient(jwks_uri)
        self._jwks_client_expires_at = now + self.config.jwks_cache_seconds
        return self._jwks_client

    def _principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        subject = str(claims.get("sub") or "")
        if not subject:
            raise AuthError("token does not contain sub")

        raw_groups = claims.get(self.config.groups_claim, [])
        groups = normalize_groups(raw_groups)
        username = claims.get("preferred_username") or claims.get("email")
        return Principal(subject=subject, username=str(username) if username else None, groups=groups, claims=claims)


def normalize_groups(raw_groups: Any) -> set[str]:
    if raw_groups is None:
        return set()
    if isinstance(raw_groups, str):
        raw_iterable = [raw_groups]
    elif isinstance(raw_groups, list):
        raw_iterable = raw_groups
    else:
        return set()

    groups: set[str] = set()
    for item in raw_iterable:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if not normalized:
            continue
        groups.add(normalized)
        groups.add(normalized.strip("/"))
        groups.add(normalized.rsplit("/", 1)[-1])
    return groups


def configured_audiences(raw_audience: str | list[str] | None) -> list[str]:
    if raw_audience is None:
        return []
    if isinstance(raw_audience, str):
        return [raw_audience]
    return [audience for audience in raw_audience if audience]


def has_required_group(principal: Principal, required_groups: list[str]) -> bool:
    if "*" in principal.groups:
        return True
    if not required_groups:
        return True
    wanted = {group.strip("/").rsplit("/", 1)[-1] for group in required_groups}
    return bool(principal.groups.intersection(wanted))
