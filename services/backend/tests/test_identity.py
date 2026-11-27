from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import jwt
import pytest
from regagent_backend.identity import (
    AuthError,
    UserIdentity,
    current_identity,
    identity_to_user_dict,
)

MakeToken = Callable[..., str]
Verify = Callable[..., UserIdentity]


def test_valid_token(make_token: MakeToken, verify: Verify) -> None:
    ident = verify(
        make_token(sub="u-42", groups=["/levels/closed_fin"], realm_access={"roles": ["qc"]}),
    )
    assert ident.sub == "u-42"
    assert ident.username == "user-u-42"
    assert ident.groups == frozenset({"/levels/closed_fin"})
    assert ident.roles == frozenset({"qc"})


def test_expired_token_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(exp_in=-120))


def test_leeway_accepts_just_expired(make_token: MakeToken, verify: Verify) -> None:
    assert verify(make_token(exp_in=-5), leeway_s=30).sub == "u-1"


def test_wrong_issuer_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(iss="http://evil/auth/realms/regagent"))


def test_wrong_audience_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(aud="account"))


def test_audience_in_list_accepted(make_token: MakeToken, verify: Verify, audience: str) -> None:
    assert verify(make_token(aud=["account", audience])).sub == "u-1"


def test_token_without_groups_gets_public_only(make_token: MakeToken, verify: Verify) -> None:
    assert verify(make_token()).levels == frozenset({"public"})
    assert verify(make_token(groups=[])).levels == frozenset({"public"})


def test_malformed_groups_claim_gets_public_only(make_token: MakeToken, verify: Verify) -> None:
    ident = verify(make_token(groups="/levels/closed_fin"))
    assert ident.levels == frozenset({"public"})
    assert ident.groups == frozenset()


def test_fin_group_gets_closed_fin(make_token: MakeToken, verify: Verify) -> None:
    ident = verify(make_token(groups=["/levels/closed_fin", "/other"]))
    assert ident.levels == frozenset({"public", "closed_fin"})


def test_unknown_kid_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(kid="other"))


def test_symmetric_alg_rejected(verify: Verify, issuer: str, audience: str) -> None:
    now = int(time.time())
    forged = jwt.encode(
        {"sub": "x", "iss": issuer, "aud": audience, "exp": now + 60},
        "secret-secret-secret-secret-secret!",
        algorithm="HS256",
        headers={"kid": "test-key"},
    )
    with pytest.raises(AuthError):
        verify(forged)


def test_missing_sub_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(drop=("sub",)))


def test_missing_exp_rejected(make_token: MakeToken, verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify(make_token(drop=("exp",)))


def test_garbage_token_rejected(verify: Verify) -> None:
    with pytest.raises(AuthError):
        verify("not-a-jwt")


# --- идентичность внутри графа ---


class AegraUser:
    """Как Aegra отдаёт пользователя в графе: поля словаря — атрибуты."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.__dict__.update(data)


def _ident() -> UserIdentity:
    return UserIdentity(
        sub="u-1",
        username="alice",
        groups=frozenset({"/levels/closed_fin"}),
        roles=frozenset({"qc"}),
        levels=frozenset({"public", "closed_fin"}),
    )


def test_current_identity_from_langgraph_auth_user() -> None:
    user = AegraUser(identity_to_user_dict(_ident()))
    assert current_identity({"configurable": {"langgraph_auth_user": user}}) == _ident()


def test_current_identity_accepts_mapping_user() -> None:
    user = identity_to_user_dict(_ident())
    assert current_identity({"configurable": {"langgraph_auth_user": user}}) == _ident()


def test_current_identity_ignores_client_configurable() -> None:
    public = UserIdentity("bob", "bob", frozenset(), frozenset(), frozenset({"public"}))
    config = {
        "configurable": {
            "langgraph_auth_user": AegraUser(identity_to_user_dict(public)),
            "user_id": "u-1",
            "levels": ["closed_fin"],
            "sub": "u-1",
        }
    }
    assert current_identity(config) == public


def test_current_identity_without_user_fails_closed() -> None:
    with pytest.raises(AuthError):
        current_identity({"configurable": {"user_id": "u-1", "levels": ["public"]}})


def test_current_identity_without_levels_fails_closed() -> None:
    user = AegraUser({"identity": "u-1", "display_name": "x"})
    with pytest.raises(AuthError):
        current_identity({"configurable": {"langgraph_auth_user": user}})
