from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from typing import Any, ClassVar

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ISSUER = "http://localhost:8080/auth/realms/regagent"
AUDIENCE = "regagent-web"
KID = "test-key"


class FakeLevels:
    """Та же семантика, что у AccessLevels из access_levels.yaml стенда."""

    mapping: ClassVar[dict[str, list[str]]] = {
        "public": ["*"],
        "closed_fin": ["/levels/closed_fin"],
    }

    def levels_for(self, groups: Iterable[str]) -> frozenset[str]:
        groups = set(groups)
        return frozenset(
            level
            for level, need in self.mapping.items()
            if "*" in need or groups.intersection(need)
        )


@pytest.fixture
def levels() -> FakeLevels:
    return FakeLevels()


@pytest.fixture(scope="session")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks_dict(rsa_key: rsa.RSAPrivateKey) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update({"kid": KID, "use": "sig", "alg": "RS256"})
    return {"keys": [jwk]}


@pytest.fixture(scope="session")
def jwks(jwks_dict: dict[str, Any]) -> jwt.PyJWKSet:
    return jwt.PyJWKSet.from_dict(jwks_dict)


MakeToken = Callable[..., str]


@pytest.fixture
def verify(jwks: jwt.PyJWKSet, levels: FakeLevels) -> Callable[..., Any]:
    from regagent_backend.identity import verify_jwt

    def _verify(token: str, **kw: Any) -> Any:
        return verify_jwt(token, jwks, issuer=ISSUER, audience=AUDIENCE, levels=levels, **kw)

    return _verify


@pytest.fixture
def issuer() -> str:
    return ISSUER


@pytest.fixture
def audience() -> str:
    return AUDIENCE


@pytest.fixture
def make_token(rsa_key: rsa.RSAPrivateKey) -> MakeToken:
    def _make(
        *,
        sub: str = "u-1",
        groups: list[str] | object | None = None,
        iss: str = ISSUER,
        aud: str | list[str] = AUDIENCE,
        exp_in: int = 300,
        kid: str = KID,
        drop: tuple[str, ...] = (),
        **extra: Any,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub,
            "preferred_username": f"user-{sub}",
            "iss": iss,
            "aud": aud,
            "iat": now,
            "exp": now + exp_in,
            "realm_access": {"roles": ["offline_access"]},
            **extra,
        }
        if groups is not None:
            claims["groups"] = groups
        for key in drop:
            claims.pop(key, None)
        return jwt.encode(claims, rsa_key, algorithm="RS256", headers={"kid": kid})

    return _make
