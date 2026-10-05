import pytest

from flask import Blueprint
from flask import make_response
from flask import negotiation
from flask import request


def test_language_and_media_negotiation_vary(app, client):
    """Recording Accept-Language and Accept produces exactly those two Vary
    items, in registration order."""

    @app.route("/")
    def index():
        accept_lang = request.headers.get("Accept-Language", "en")
        accept = request.accept_mimetypes.best_match(["text/html", "application/json"])
        negotiation.record("Accept-Language")
        negotiation.record("Accept")
        return f"{accept_lang}:{accept}"

    rv = client.get(
        "/", headers={"Accept-Language": "zh", "Accept": "application/json"}
    )
    assert rv.status_code == 200
    assert rv.headers["Vary"] == "Accept-Language, Accept"


def test_record_dedup_case_insensitive(app, client):
    """Registering the same header name with different letter cases results
    in a single Vary item."""

    @app.route("/")
    def index():
        negotiation.record("accept")
        negotiation.record("ACCEPT")
        negotiation.record("Accept")
        negotiation.record("accept-language")
        negotiation.record("Accept-Language")
        return ""

    rv = client.get("/")
    assert rv.headers["Vary"] == "accept, accept-language"


def test_existing_vary_order_preserved(app, client):
    """Names already in Vary keep their order and are not duplicated;
    recorded names are appended."""

    @app.route("/")
    def index():
        negotiation.record("Accept-Language")
        negotiation.record("accept-encoding")  # already present, different case
        rv = make_response("")
        rv.vary.add("Accept-Encoding")
        rv.vary.add("Cookie")
        return rv

    rv = client.get("/")
    assert rv.headers["Vary"] == "Accept-Encoding, Cookie, Accept-Language"


def test_existing_star_is_kept(app, client):
    """An existing ``Vary: *`` is left untouched, recorded names are not
    appended."""

    @app.route("/")
    def index():
        negotiation.record("Accept")
        rv = make_response("")
        rv.vary.add("*")
        return rv

    rv = client.get("/")
    assert rv.headers["Vary"] == "*"


def test_recorded_star_takes_priority(app, client):
    """Recording ``*`` subsumes other recorded and existing names."""

    @app.route("/")
    def index():
        negotiation.record("Accept")
        negotiation.record("*")
        negotiation.record("Accept-Language")
        rv = make_response("")
        rv.vary.add("Accept-Encoding")
        return rv

    rv = client.get("/")
    assert rv.headers["Vary"] == "*"


def test_304_keeps_same_vary(app, client):
    """A 304 response carries the same Vary (cache identity) as the 200."""

    @app.route("/")
    def index():
        negotiation.record("Accept-Language")
        negotiation.record("Accept")
        rv = make_response("hello")
        rv.set_etag("v1")
        return rv.make_conditional(request)

    rv_200 = client.get("/")
    assert rv_200.status_code == 200
    assert rv_200.headers["Vary"] == "Accept-Language, Accept"

    rv_304 = client.get("/", headers={"If-None-Match": rv_200.headers["ETag"]})
    assert rv_304.status_code == 304
    assert rv_304.headers["Vary"] == rv_200.headers["Vary"]


def test_head_keeps_same_vary(app, client):
    """A HEAD response carries the same Vary as the corresponding GET."""

    @app.route("/")
    def index():
        negotiation.record("Accept-Language")
        negotiation.record("Accept")
        return "hello"

    rv_get = client.get("/")
    rv_head = client.head("/")
    assert rv_head.status_code == 200
    assert (
        rv_head.headers["Vary"] == rv_get.headers["Vary"] == ("Accept-Language, Accept")
    )


def test_nested_blueprint_callbacks_share_state(app, client):
    """The view and after_request callbacks of nested blueprints and the app
    all share one recorder for the current response."""
    child = Blueprint("child", __name__)
    parent = Blueprint("parent", __name__)
    seen = []

    @child.after_request
    def child_after(rv):
        seen.append(negotiation._get_current_object())
        negotiation.record("Accept")
        return rv

    @parent.after_request
    def parent_after(rv):
        seen.append(negotiation._get_current_object())
        negotiation.record("Accept-Language")
        return rv

    @app.after_request
    def app_after(rv):
        seen.append(negotiation._get_current_object())
        negotiation.record("accept")  # duplicate of child's, different case
        return rv

    @child.route("/")
    def index():
        seen.append(negotiation._get_current_object())
        return ""

    parent.register_blueprint(child)
    app.register_blueprint(parent)

    rv = client.get("/")
    assert rv.headers["Vary"] == "Accept, Accept-Language"
    # One shared recorder instance across view and all nested callbacks.
    assert len(seen) == 4
    assert all(obj is seen[0] for obj in seen)


def test_unused_recorder_leaves_response_unchanged(app, client):
    """Responses that never use the recorder do not get a Vary header."""

    @app.route("/")
    def index():
        return ""

    rv = client.get("/")
    assert "Vary" not in rv.headers


def test_recorder_does_not_store_values(app, client):
    """Only header names are recorded, never header values."""

    @app.route("/")
    def index():
        value = request.headers["Accept"]
        negotiation.record("Accept")
        recorder = negotiation._get_current_object()
        assert recorder.field_names == ("Accept",)
        assert value not in repr(recorder)
        assert not hasattr(recorder, "values")
        return ""

    client.get("/", headers={"Accept": "application/vnd.secret+json"})


def test_record_rejects_empty_name(app, client):
    @app.route("/")
    def index():
        with pytest.raises(ValueError):
            negotiation.record("  ")
        return ""

    rv = client.get("/")
    assert "Vary" not in rv.headers
