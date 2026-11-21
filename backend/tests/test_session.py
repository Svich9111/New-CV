import uuid

import pytest

from app.core.session import SessionError, issue_token, read_token


def test_roundtrip() -> None:
    uid = uuid.uuid4()
    assert read_token(issue_token(uid, "k" * 32, 60), "k" * 32) == uid


def test_short_secret_refused() -> None:
    with pytest.raises(SessionError):
        issue_token(uuid.uuid4(), "short", 60)


def test_alg_none_rejected() -> None:
    import jwt

    forged = jwt.encode({"sub": str(uuid.uuid4())}, key="", algorithm="none")
    with pytest.raises(SessionError):
        read_token(forged, "k" * 32)
