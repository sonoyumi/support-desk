import pytest

from support_desk import security
from support_desk.config import ConfigError, load_settings

KEY = "k" * 40


def test_password_hash_verifies_and_is_salted():
    a, b = security.hash_password("correct horse"), security.hash_password("correct horse")
    assert a != b  # random salt: equal passwords give different hashes
    assert security.verify_password("correct horse", a)
    assert not security.verify_password("wrong horse", a)


@pytest.mark.parametrize("stored", ["", "plain", "md5$abc", "scrypt$x$y$z$salt$hash"])
def test_broken_hash_never_verifies(stored):
    assert security.verify_password("anything", stored) is False


def test_session_roundtrip_and_expiry():
    cookie = security.make_session(KEY, 7, now=1000, ttl=60)
    assert security.read_session(KEY, cookie, now=1059) == 7
    assert security.read_session(KEY, cookie, now=1061) is None


def test_tampered_or_foreign_session_is_rejected():
    cookie = security.make_session(KEY, 7, now=1000)
    op, expires, sig = cookie.split(".")
    assert security.read_session(KEY, f"1.{expires}.{sig}", now=1000) is None  # another operator id
    assert security.read_session(KEY, f"{op}.{int(expires) + 999}.{sig}", now=1000) is None  # longer life
    assert security.read_session("x" * 40, cookie, now=1000) is None  # another server key
    assert security.read_session(KEY, "garbage", now=1000) is None


def test_csrf_token_is_bound_to_the_session():
    c1, c2 = security.make_session(KEY, 1, now=1), security.make_session(KEY, 2, now=1)
    assert security.check_csrf(KEY, c1, security.csrf_token(KEY, c1))
    assert not security.check_csrf(KEY, c2, security.csrf_token(KEY, c1))
    assert not security.check_csrf(KEY, None, "x")


def test_config_reports_every_error_at_once(monkeypatch):
    for name in ("BOT_TOKEN", "SECRET_KEY", "OPERATORS_CHAT_ID", "PORT", "MAX_MESSAGE_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SECRET_KEY", "short")
    monkeypatch.setenv("OPERATORS_CHAT_ID", "support-team")
    monkeypatch.setenv("PORT", "eighty")
    monkeypatch.setenv("MAX_MESSAGE_CHARS", "5000")
    with pytest.raises(ConfigError) as err:
        load_settings(None)
    text = str(err.value)
    for name in ("BOT_TOKEN", "SECRET_KEY", "OPERATORS_CHAT_ID", "PORT", "MAX_MESSAGE_CHARS"):
        assert name in text


def test_panel_alone_does_not_need_a_bot_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.setenv("SECRET_KEY", "z" * 64)
    monkeypatch.setenv("PANEL_URL", "https://support.example/")
    s = load_settings(None, need_bot=False)
    assert s.panel_url == "https://support.example"  # trailing slash removed
    assert "z" * 64 not in repr(s)  # secrets are hidden from repr
