"""The startup refusal on a guessable SECRET_KEY.

SECRET_KEY signs every session token and derives the Fernet key that encrypts
stored bank credentials, using a salt that is a constant in this repository.
A default value therefore means forgeable logins and recoverable credentials,
and nothing about it is visible at runtime — the pods are Healthy either way.
These tests exist so the refusal cannot be quietly removed.
"""
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from app.main import _MIN_SECRET_KEY_LENGTH, _assert_secret_key_is_usable


class _Settings:
    def __init__(self, key: str, debug: bool = False):
        self.secret_key = SecretStr(key)
        self.debug = debug


def _run(key: str, debug: bool = False):
    with patch("app.main.get_settings", return_value=_Settings(key, debug)):
        _assert_secret_key_is_usable()


@pytest.mark.parametrize(
    "key",
    ["", "change-me-in-production", "dev-secret-change-in-production"],
)
def test_refuses_to_start_on_a_known_placeholder(key):
    with pytest.raises(RuntimeError) as exc:
        _run(key)
    assert "SECRET_KEY" in str(exc.value)


def test_refuses_to_start_on_a_short_key():
    with pytest.raises(RuntimeError) as exc:
        _run("x" * (_MIN_SECRET_KEY_LENGTH - 1))
    assert str(_MIN_SECRET_KEY_LENGTH) in str(exc.value)


def test_accepts_a_key_of_real_length():
    _run("x" * _MIN_SECRET_KEY_LENGTH)
    _run("A7xK2mQ9vB4nR6tY8uI0oP3sD5fG1hJ7kL9zX2cV4bN6mQ8wE0rT")


def test_the_error_says_how_to_generate_one():
    with pytest.raises(RuntimeError) as exc:
        _run("change-me-in-production")
    assert "token_urlsafe" in str(exc.value)


def test_debug_mode_warns_instead_of_refusing(caplog):
    """Compose ships a placeholder on purpose, so local dev must still boot —
    but it warns, because a developer who connects a real bank locally is
    storing a real credential under a publicly known key."""
    with caplog.at_level("WARNING"):
        _run("change-me-in-production", debug=True)
    assert any("SECRET_KEY" in r.message for r in caplog.records)


def test_debug_mode_is_silent_on_a_good_key(caplog):
    with caplog.at_level("WARNING"):
        _run("x" * 64, debug=True)
    assert not [r for r in caplog.records if "SECRET_KEY" in r.message]
