"""1.1 / ФТ-01: проверка подписи initData на сервере."""

import json
import time

import pytest

from app.core.telegram import InitDataError, sign_init_data, validate_init_data

TOKEN = "123456:TEST-bot-token-not-real"
NOW = 1_800_000_000.0
DAY = 86400


def _fields(**over: str) -> dict[str, str]:
    base = {
        "auth_date": str(int(NOW) - 10),
        "query_id": "AAH-test",
        "user": json.dumps({"id": 42, "first_name": "Анна"}, ensure_ascii=False),
    }
    return {**base, **over}


def test_valid_init_data_accepted() -> None:
    user = validate_init_data(sign_init_data(_fields(), TOKEN), TOKEN, DAY, now=NOW)
    assert user.id == 42


def test_forged_signature_rejected() -> None:
    """Критерий RoadMap 1.1: «Поддельная подпись отклоняется»."""
    forged = sign_init_data(_fields(), "999:another-bot")
    with pytest.raises(InitDataError, match="bad signature"):
        validate_init_data(forged, TOKEN, DAY, now=NOW)


def test_tampered_user_rejected() -> None:
    signed = sign_init_data(_fields(), TOKEN)
    tampered = signed.replace("%22id%22%3A+42", "%22id%22%3A+43")
    assert tampered != signed
    with pytest.raises(InitDataError, match="bad signature"):
        validate_init_data(tampered, TOKEN, DAY, now=NOW)


def test_added_field_rejected() -> None:
    signed = sign_init_data(_fields(), TOKEN) + "&start_param=evil"
    with pytest.raises(InitDataError, match="bad signature"):
        validate_init_data(signed, TOKEN, DAY, now=NOW)


def test_missing_hash_rejected() -> None:
    with pytest.raises(InitDataError):
        validate_init_data("auth_date=1&user=%7B%22id%22%3A1%7D", TOKEN, DAY, now=NOW)


def test_duplicate_keys_rejected() -> None:
    signed = sign_init_data(_fields(), TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(signed + "&auth_date=1", TOKEN, DAY, now=NOW)


def test_expired_rejected() -> None:
    old = sign_init_data(_fields(auth_date=str(int(NOW) - DAY - 1)), TOKEN)
    with pytest.raises(InitDataError, match="expired"):
        validate_init_data(old, TOKEN, DAY, now=NOW)


def test_future_auth_date_rejected() -> None:
    future = sign_init_data(_fields(auth_date=str(int(NOW) + 3600)), TOKEN)
    with pytest.raises(InitDataError, match="future"):
        validate_init_data(future, TOKEN, DAY, now=NOW)


@pytest.mark.parametrize("user", ['{"first_name": "x"}', '{"id": true}', '{"id": -5}', "not-json"])
def test_bad_user_payload_rejected(user: str) -> None:
    with pytest.raises(InitDataError):
        validate_init_data(sign_init_data(_fields(user=user), TOKEN), TOKEN, DAY, now=NOW)


def test_empty_bot_token_never_validates() -> None:
    """Пустой токен в конфиге не должен давать «подписи» с пустым ключом."""
    with pytest.raises(InitDataError):
        validate_init_data(sign_init_data(_fields(), ""), "", DAY, now=NOW)


def test_malformed_rejected() -> None:
    with pytest.raises(InitDataError):
        validate_init_data("%%%", TOKEN, DAY, now=time.time())


def test_missing_hash_reason() -> None:
    with pytest.raises(InitDataError, match="no hash"):
        validate_init_data("auth_date=1&user=%7B%22id%22%3A1%7D", TOKEN, DAY, now=NOW)


@pytest.mark.parametrize("bad", ["", "abc", "%D0%B0" * 32, "g" * 64, "a" * 65])
def test_bad_hash_format_rejected(bad: str) -> None:
    base = sign_init_data(_fields(), TOKEN).rsplit("&hash=", 1)[0]
    with pytest.raises(InitDataError):
        validate_init_data(f"{base}&hash={bad}", TOKEN, DAY, now=NOW)


def test_uppercase_hash_accepted() -> None:
    base, digest = sign_init_data(_fields(), TOKEN).rsplit("&hash=", 1)
    assert validate_init_data(f"{base}&hash={digest.upper()}", TOKEN, DAY, now=NOW).id == 42
