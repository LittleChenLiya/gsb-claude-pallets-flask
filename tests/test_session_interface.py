import hashlib
import logging

import pytest

import flask
from flask import session
from flask.globals import app_ctx
from flask.sessions import SecureCookieSessionInterface
from flask.sessions import SessionInterface
from flask.sessions import TaggedJSONSerializer


def test_open_session_with_endpoint():
    """If request.endpoint (or other URL matching behavior) is needed
    while loading the session, RequestContext.match_request() can be
    called manually.
    """

    class MySessionInterface(SessionInterface):
        def save_session(self, app, session, response):
            pass

        def open_session(self, app, request):
            app_ctx.match_request()
            assert request.endpoint is not None

    app = flask.Flask(__name__)
    app.session_interface = MySessionInterface()

    @app.get("/")
    def index():
        return "Hello, World!"

    response = app.test_client().get("/")
    assert response.status_code == 200


class CountingSerializer(TaggedJSONSerializer):
    """Records how many times a session payload is serialized.

    itsdangerous probes the serializer with an empty dict when the
    signing serializer is constructed, so only non-empty payloads (the
    actual session data) are counted.
    """

    def __init__(self):
        super().__init__()
        self.dumps_calls = 0

    def dumps(self, obj, **kwargs):
        if obj:
            self.dumps_calls += 1

        return super().dumps(obj, **kwargs)


def incompressible(length):
    """Deterministic pseudo-random hex data that zlib cannot compress,
    so the signed cookie payload grows with the session data.
    """
    out = []

    for i in range((length // 64) + 1):
        out.append(hashlib.sha256(str(i).encode()).hexdigest())

    return "".join(out)[:length]


def make_budget_app(cookie_budget=512, **config):
    """An app with a fake signing key and the cookie budget policy."""
    app = flask.Flask(__name__)
    app.secret_key = "fake-signing-key-for-tests"
    app.config.update(config)
    interface = SecureCookieSessionInterface(cookie_budget=cookie_budget)
    interface.serializer = CountingSerializer()
    app.session_interface = interface

    @app.get("/set/<value>")
    def set_value(value):
        session["data"] = value
        return "set"

    @app.get("/set_chinese")
    def set_chinese():
        # Distinct CJK characters escape to \uXXXX in JSON and do not
        # compress, inflating the final header beyond the budget.
        session["data"] = "".join(chr(0x4E00 + i) for i in range(500))
        return "set"

    @app.get("/get")
    def get_value():
        return session.get("data", "<empty>")

    @app.get("/clear")
    def clear():
        session.clear()
        return "cleared"

    return app


def test_budget_small_session_sets_cookie():
    """A session within the budget is saved as usual."""
    app = make_budget_app()
    client = app.test_client()

    response = client.get("/set/hello")
    assert response.status_code == 200
    set_cookie = response.headers.get("Set-Cookie")
    assert set_cookie is not None
    assert set_cookie.startswith("session=")

    # The cookie can be read back on the next request.
    assert client.get("/get").get_data(as_text=True) == "hello"
    # The serializer ran exactly once for the save.
    assert app.session_interface.serializer.dumps_calls == 1


def test_budget_long_value_refused_and_old_cookie_kept(caplog):
    """If the final Set-Cookie header exceeds the budget, no new cookie
    is sent and the client's existing cookie remains readable.
    """
    app = make_budget_app()
    client = app.test_client()

    # Establish a small, valid cookie first.
    client.get("/set/original")
    assert app.session_interface.serializer.dumps_calls == 1

    # A long value pushes the final header over the budget.
    with caplog.at_level(logging.ERROR, logger=app.logger.name):
        response = client.get("/set/" + incompressible(3000))

    assert response.status_code == 200
    # No new Set-Cookie header: nothing truncated, nothing deleted.
    assert response.headers.get("Set-Cookie") is None
    # The failure is diagnosable from the logged error.
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "exceeding the configured budget of 512 bytes" in message
    # The old cookie is still intact and readable.
    assert client.get("/get").get_data(as_text=True) == "original"
    # The serializer still ran exactly once per save attempt.
    assert app.session_interface.serializer.dumps_calls == 2


def test_budget_chinese_value_refused():
    """Non-ASCII data that inflates the signed payload is also refused."""
    app = make_budget_app()
    client = app.test_client()

    client.get("/set/original")
    response = client.get("/set_chinese")
    assert response.headers.get("Set-Cookie") is None
    assert client.get("/get").get_data(as_text=True) == "original"


def test_budget_delete_cookie_keeps_attributes():
    """The empty-session deletion path is unaffected by the budget and
    keeps the same Path and SameSite attributes as the set path.
    """
    app = make_budget_app(
        SESSION_COOKIE_PATH="/app",
        SESSION_COOKIE_SAMESITE="Strict",
    )
    client = app.test_client()

    set_cookie = client.get("/set/hello").headers["Set-Cookie"]
    assert "Path=/app" in set_cookie
    assert "SameSite=Strict" in set_cookie

    delete_cookie = client.get("/clear").headers["Set-Cookie"]
    assert "session=;" in delete_cookie
    assert "Expires=Thu, 01 Jan 1970" in delete_cookie
    assert "Path=/app" in delete_cookie
    assert "SameSite=Strict" in delete_cookie


def test_no_budget_default_behavior_unchanged():
    """Without the policy, an oversized cookie is still sent and only
    causes Werkzeug's warning, as before.
    """
    app = flask.Flask(__name__)
    app.secret_key = "fake-signing-key-for-tests"
    assert app.session_interface.cookie_budget is None
    assert app.session_interface.get_cookie_budget(app) is None

    @app.get("/")
    def index():
        session["data"] = incompressible(8000)
        return "set"

    with pytest.warns(UserWarning, match="cookie is too large"):
        response = app.test_client().get("/")

    assert response.headers["Set-Cookie"].startswith("session=")


def test_budget_refuse_hook_can_be_overridden(caplog):
    """Subclasses can change how a refused cookie is handled."""
    refused = []

    class MyInterface(SecureCookieSessionInterface):
        def refuse_oversized_cookie(self, app, session, response, size, budget):
            refused.append((size, budget))

    app = flask.Flask(__name__)
    app.secret_key = "fake-signing-key-for-tests"
    app.session_interface = MyInterface(cookie_budget=512)

    @app.get("/")
    def index():
        session["data"] = incompressible(3000)
        return "set"

    response = app.test_client().get("/")
    assert response.headers.get("Set-Cookie") is None
    assert len(refused) == 1
    size, budget = refused[0]
    assert budget == 512
    assert size > budget
