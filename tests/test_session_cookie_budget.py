"""Tests for the optional strict session cookie size budget.

When SESSION_COOKIE_MAX_SIZE is set, the signed session cookie's final
Set-Cookie header (including the signature, attributes and escaping) is
measured. If it exceeds the budget, SessionCookieTooLarge is raised and no
cookie is sent. The serializer runs exactly once. When the budget is not set,
the default behavior is unchanged.
"""

import pytest

from flask import session
from flask.sessions import SecureCookieSessionInterface
from flask.sessions import SessionCookieTooLarge


class CountingSerializer:
    """A fake signing serializer that counts how many times dumps is called
    and returns a fixed large value so the budget is always exceeded.
    """

    def __init__(self, value="x" * 5000):
        self.value = value
        self.dumps_calls = 0
        self.loads_calls = 0

    def dumps(self, obj):
        self.dumps_calls += 1
        return self.value

    def loads(self, val, max_age=None):
        self.loads_calls += 1
        return {}


def make_interface(serializer=None):
    """Create a session interface wired to a fake signing serializer."""
    if serializer is None:
        serializer = CountingSerializer()
    interface = SecureCookieSessionInterface()
    interface.get_signing_serializer = lambda app: serializer
    return interface, serializer


def test_small_session_succeeds(app, client):
    """A session whose final header is within the budget is set normally."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = 4093

    @app.get("/set")
    def set_session():
        session["user"] = "alice"
        return "ok"

    response = client.get("/set")
    assert response.status_code == 200
    assert "Set-Cookie" in response.headers
    assert response.headers["Set-Cookie"].startswith("session=")


def test_large_session_rejected_no_cookie(app, client):
    """When the final header exceeds the budget, no cookie is sent and the
    client's existing cookie is left untouched and still readable.
    """
    app.config["SESSION_COOKIE_MAX_SIZE"] = 200
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/set-small")
    def set_small():
        session["user"] = "alice"
        return "ok"

    @app.get("/set-large")
    def set_large():
        import os

        session["big"] = os.urandom(400).hex()
        return "ok"

    @app.get("/read")
    def read():
        return dict(session)

    response = client.get("/set-small")
    assert response.status_code == 200
    assert "Set-Cookie" in response.headers

    response = client.get("/set-large")
    assert response.status_code == 500
    assert "Set-Cookie" not in response.headers

    response = client.get("/read")
    assert response.status_code == 200
    assert b'"user":"alice"' in response.data
    assert b'"big"' not in response.data


def test_chinese_value_exceeds_budget(app, client):
    """A Chinese value that makes the final header exceed the budget is
    rejected with no new cookie.
    """
    app.config["SESSION_COOKIE_MAX_SIZE"] = 100
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/set")
    def set_session():
        session["greeting"] = "中文测试" * 500
        return "ok"

    response = client.get("/set")
    assert response.status_code == 500
    assert "Set-Cookie" not in response.headers


def test_serializer_called_once(app, client):
    """The signing serializer runs exactly once even when the budget is
    exceeded."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = 100
    app.config["PROPAGATE_EXCEPTIONS"] = False

    interface, serializer = make_interface()
    app.session_interface = interface

    @app.get("/set")
    def set_session():
        session["a"] = "b"
        return "ok"

    response = client.get("/set")
    assert response.status_code == 500
    assert serializer.dumps_calls == 1
    assert "Set-Cookie" not in response.headers


def test_budget_error_is_diagnosable(app, client):
    """The raised error reports the measured size and the configured limit."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = 100

    @app.get("/set")
    def set_session():
        session["a"] = "b" * 5000
        return "ok"

    with pytest.raises(SessionCookieTooLarge) as exc_info:
        client.get("/set")

    message = str(exc_info.value)
    assert "100" in message
    assert "SESSION_COOKIE_MAX_SIZE" in message
    assert "session" in message


def test_deletion_cookie_preserves_attributes(app, client):
    """The empty-session deletion path still sets a delete cookie with the
    same Path and SameSite attributes as the original interface."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = 100
    app.config["SESSION_COOKIE_PATH"] = "/custom"
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    @app.get("/clear")
    def clear_session():
        session.clear()
        return "cleared"

    response = client.get("/clear")
    assert response.status_code == 200
    cookie = response.headers["Set-Cookie"]
    assert "Path=/custom" in cookie
    assert "SameSite=Lax" in cookie
    assert "Max-Age=0" in cookie


def test_default_budget_disabled(app, client):
    """With no budget configured, the default behavior is unchanged: a large
    cookie is still set (Werkzeug may only warn)."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = None

    @app.get("/set")
    def set_session():
        session["big"] = "x" * 5000
        return "ok"

    response = client.get("/set")
    assert response.status_code == 200
    assert "Set-Cookie" in response.headers


def test_budget_via_interface_attribute(app, client):
    """The budget can also be set on the interface class attribute instead of
    the config key."""
    app.config["SESSION_COOKIE_MAX_SIZE"] = None
    app.config["PROPAGATE_EXCEPTIONS"] = False

    interface, serializer = make_interface()
    interface.max_cookie_size = 100
    app.session_interface = interface

    @app.get("/set")
    def set_session():
        session["a"] = "b"
        return "ok"

    response = client.get("/set")
    assert response.status_code == 500
    assert serializer.dumps_calls == 1
    assert "Set-Cookie" not in response.headers


def test_budget_measures_final_header_bytes(app):
    """The budget is measured on the final header bytes, including the
    signature, attributes and escaping."""
    from werkzeug.http import dump_cookie

    app.config["SECRET_KEY"] = "secret"
    app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
    app.config["SESSION_COOKIE_PATH"] = "/app"

    interface = SecureCookieSessionInterface()
    signing = interface.get_signing_serializer(app)
    val = signing.dumps({"a": "b"})

    header = dump_cookie(
        interface.get_cookie_name(app),
        value=val,
        path=interface.get_cookie_path(app),
        domain=interface.get_cookie_domain(app),
        secure=interface.get_cookie_secure(app),
        httponly=interface.get_cookie_httponly(app),
        samesite=interface.get_cookie_samesite(app),
        partitioned=interface.get_cookie_partitioned(app),
        max_size=0,
    )

    assert len(header.encode("latin1")) > len(val)
    assert "SameSite=Strict" in header
    assert "Path=/app" in header
