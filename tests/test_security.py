import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def _encode(claims: dict, secret: str | None = None) -> str:
    settings = get_settings()
    return jwt.encode(claims, secret or settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def _valid_claims(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    claims = {
        "sub": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": uuid.uuid4().hex,
        "auth_version": 1,
    }
    claims.update(overrides)
    return claims


def test_password_hash_uses_argon2id_and_is_salted():
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert first.startswith("$argon2id$")
    assert first != second
    assert "correct horse battery staple" not in first


def test_verify_password_accepts_the_right_password_and_rejects_others():
    password_hash = hash_password("s3cret-password")

    assert verify_password(password_hash, "s3cret-password") is True
    assert verify_password(password_hash, "wrong-password") is False


def test_verify_password_rejects_a_malformed_hash_instead_of_raising():
    assert verify_password("not-a-hash", "anything") is False


def test_access_token_carries_the_required_claims():
    user_id = uuid.uuid4()

    token = create_access_token(user_id=user_id, auth_version=7)

    settings = get_settings()
    payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    assert payload["sub"] == str(user_id)
    assert payload["auth_version"] == 7
    assert {"iat", "exp", "jti"} <= payload.keys()
    assert "permissions" not in payload


def test_access_tokens_are_short_lived():
    payload = jwt.decode(
        create_access_token(user_id=uuid.uuid4(), auth_version=1),
        get_settings().jwt_secret_key,
        algorithms=[get_settings().jwt_algorithm],
    )

    assert payload["exp"] - payload["iat"] <= 60 * 60


def test_decode_access_token_returns_identity_and_authorization_version():
    user_id = uuid.uuid4()

    claims = decode_access_token(create_access_token(user_id=user_id, auth_version=3))

    assert claims.user_id == user_id
    assert claims.auth_version == 3


def test_decode_access_token_rejects_an_expired_token():
    expired = _encode(
        _valid_claims(
            iat=int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp()),
            exp=int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()),
        )
    )

    with pytest.raises(AuthenticationError) as error:
        decode_access_token(expired)
    assert error.value.code == "INVALID_ACCESS_TOKEN"


def test_decode_access_token_rejects_a_malformed_token():
    with pytest.raises(AuthenticationError):
        decode_access_token("this-is-not-a-jwt")


def test_decode_access_token_rejects_an_invalid_signature():
    with pytest.raises(AuthenticationError):
        decode_access_token(
            _encode(_valid_claims(), secret="a-completely-different-secret-of-sufficient-length")
        )


@pytest.mark.parametrize("missing_claim", ["sub", "exp", "jti", "auth_version"])
def test_decode_access_token_rejects_a_token_missing_a_required_claim(missing_claim):
    claims = _valid_claims()
    del claims[missing_claim]

    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(claims))


def test_decode_access_token_rejects_a_non_uuid_subject():
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(_valid_claims(sub="not-a-uuid")))


def test_refresh_tokens_are_unguessable_and_stored_only_as_a_hash():
    raw = generate_refresh_token()

    assert len(raw) >= 43
    assert raw != generate_refresh_token()

    digest = hash_refresh_token(raw)
    assert len(digest) == 64
    assert raw not in digest
    assert digest == hash_refresh_token(raw)
