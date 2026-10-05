"""Tests for the response negotiation recorder (``flask.vary``).

The recorder keeps track of which request headers were used to select a
response representation during a request, then merges them into the
``Vary`` header. These tests use the Flask test client to drive real
requests, including conditional (304) and HEAD requests.

Run with::

    pytest tests/test_vary.py -v
"""

from __future__ import annotations

import pytest

from flask import Blueprint
from flask import request
from flask import Response
from flask import vary


def test_language_negotiation_sets_vary(app, client):
    @app.route("/")
    def index():
        request.accept_languages.best_match(["en", "zh"])
        vary.record("Accept-Language")
        return ""

    rv = client.get("/", headers={"Accept-Language": "en"})
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language"


def test_media_negotiation_sets_vary(app, client):
    @app.route("/")
    def index():
        request.accept_mimetypes.best_match(["application/json", "text/html"])
        vary.record("Accept")
        return ""

    rv = client.get("/", headers={"Accept": "application/json"})
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept"


def test_language_and_media_negotiation_sets_both(app, client):
    @app.route("/")
    def index():
        request.accept_languages.best_match(["en", "zh"])
        vary.record("Accept-Language")
        request.accept_mimetypes.best_match(["application/json", "text/html"])
        vary.record("Accept")
        return ""

    rv = client.get(
        "/",
        headers={"Accept-Language": "en", "Accept": "application/json"},
    )
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language, Accept"


@pytest.mark.parametrize(
    "headers",
    [
        ("accept", "Accept", "ACCEPT"),
        ("Accept", "accept"),
        ("ACCEPT", "Accept", "accept"),
    ],
)
def test_different_case_registers_one_item(app, client, headers):
    @app.route("/")
    def index():
        for header in headers:
            vary.record(header)
        return ""

    rv = client.get("/")
    assert rv.status_code == 200
    # The casing of the first recording is kept, but there is only one
    # entry regardless of the different cases used.
    assert rv.headers["Vary"] == headers[0]
    assert len(rv.headers.get_all("Vary")) == 1


def test_existing_vary_order_is_preserved(app, client):
    @app.route("/")
    def index():
        response = Response()
        response.vary.update(("X-Foo", "X-Bar"))
        vary.record("Accept-Language")
        return response

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "X-Foo, X-Bar, Accept-Language"


def test_existing_vary_with_wildcard_is_kept(app, client):
    @app.route("/")
    def index():
        response = Response()
        response.vary.add("*")
        vary.record("Accept-Language")
        return response

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "*"


def test_recorded_wildcard_sets_vary(app, client):
    @app.route("/")
    def index():
        vary.record("*")
        vary.record("Accept-Language")
        return ""

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "*"


def test_304_keeps_same_vary(app, client):
    @app.route("/")
    def index():
        response = Response("hello")
        response.add_etag()
        vary.record("Accept-Language")
        return response.make_conditional(request)

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language"

    etag = rv.headers["ETag"]
    rv = client.get("/", headers={"If-None-Match": etag})
    assert rv.status_code == 304
    assert rv.headers["Vary"] == "Accept-Language"


def test_304_from_after_request_keeps_same_vary(app, client):
    @app.after_request
    def conditional(response):
        return response.make_conditional(request)

    @app.route("/")
    def index():
        response = Response("hello")
        response.add_etag()
        vary.record("Accept-Language")
        return response

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language"

    etag = rv.headers["ETag"]
    rv = client.get("/", headers={"If-None-Match": etag})
    assert rv.status_code == 304
    assert rv.headers["Vary"] == "Accept-Language"


def test_head_keeps_same_vary(app, client):
    @app.route("/", methods=["GET", "HEAD"])
    def index():
        vary.record("Accept-Language")
        return "hello"

    rv = client.head("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language"


def test_nested_blueprint_callbacks_share_state(app, client):
    parent = Blueprint("parent", __name__)
    child = Blueprint("child", __name__)

    @child.before_request
    def before():
        vary.record("Accept-Language")

    @child.after_request
    def after(response):
        vary.record("Accept")
        return response

    @child.route("/")
    def index():
        return ""

    parent.register_blueprint(child, url_prefix="/child")
    app.register_blueprint(parent, url_prefix="/parent")

    rv = client.get("/parent/child/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language, Accept"


def test_response_without_recorder_is_unchanged(app, client):
    @app.route("/")
    def index():
        return ""

    rv = client.get("/")
    assert rv.status_code == 200
    assert "Vary" not in rv.headers


def test_header_values_are_not_recorded(app):
    with app.test_request_context("/", headers={"Accept-Language": "en-US,en;q=0.9"}):
        vary.record("Accept-Language")
        assert "en-US" not in vary.headers
        assert "accept-language" in vary.headers


def test_recorder_update_and_clear(app, client):
    @app.route("/")
    def index():
        vary.update(("Accept-Language", "Accept"))
        vary.clear()
        vary.record("Accept")
        return ""

    rv = client.get("/")
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept"


def test_recorder_headers_are_lowercase(app):
    with app.test_request_context("/"):
        vary.record("Accept-Language")
        vary.record("ACCEPT")
        assert vary.headers == {"accept-language", "accept"}


def test_recorder_outside_context_raises():
    with pytest.raises(RuntimeError):
        vary.record("Accept-Language")


def test_apply_merges_into_response(app):
    with app.test_request_context("/"):
        vary.record("Accept-Language")
        response = Response()
        vary.apply(response)
        assert response.headers["Vary"] == "Accept-Language"


def test_apply_without_recording_does_nothing(app):
    with app.test_request_context("/"):
        response = Response()
        vary.apply(response)
        assert "Vary" not in response.headers
