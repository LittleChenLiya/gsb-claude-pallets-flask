from __future__ import annotations

import asyncio

import pytest

import flask
from flask import Flask
from flask import RequestSnapshot
from flask import bind_snapshot
from flask import snapshot
from flask import snapshot_request


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True)

    @app.route("/user/<name>")
    def user(name):
        return name

    return app


class CountingStream:
    """A WSGI input stream that counts how many times it is read. Used to
    prove that creating a snapshot does not read the request body.
    """

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.read_count = 0
        self._pos = 0

    def read(self, *args, **kwargs) -> bytes:
        self.read_count += 1
        return self.data

    def readline(self, *args, **kwargs) -> bytes:
        self.read_count += 1
        return self.data

    def readlines(self, *args, **kwargs) -> list[bytes]:
        self.read_count += 1
        return [self.data]

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> None:
        self._pos = pos

    def __iter__(self):
        yield self.data

    def close(self) -> None:
        pass


def test_snapshot_captures_allowed_fields(app):
    with app.test_request_context(
        "/user/alice?page=1&page=2&filter=active",
        headers={"X-Custom": "hello", "X-Forbidden": "secret"},
    ):
        snap = snapshot_request(headers=["X-Custom"], args=["page", "filter"])

    assert snap.headers["X-Custom"] == "hello"
    assert snap.headers["x-custom"] == "hello"  # case-insensitive
    assert snap.args["page"] == "1"
    assert snap.args["filter"] == "active"


def test_snapshot_forbidden_fields_absent(app):
    with app.test_request_context(
        "/user/alice?page=1&secret=hidden",
        headers={"X-Custom": "hello", "X-Secret": "s3cret"},
    ):
        snap = snapshot_request(headers=["X-Custom"], args=["page"])

    assert "X-Secret" not in snap.headers
    assert "x-secret" not in snap.headers
    assert "secret" not in snap.args
    # Only the allowed fields are present
    assert set(snap.args) == {"page"}
    assert set(snap.headers) == {"X-Custom"}


def test_snapshot_duplicate_query_order(app):
    with app.test_request_context("/user/alice?a=1&a=2&a=3"):
        snap = snapshot_request(args=["a"])

    assert snap.args.getlist("a") == ["1", "2", "3"]
    # Order is preserved as input
    assert list(snap.args.items()) == [("a", "1"), ("a", "2"), ("a", "3")]


def test_snapshot_duplicate_header_order(app):
    # WSGI combines duplicate headers into one comma-separated value, so test
    # the immutable headers mapping directly with duplicate values.
    from flask.snapshot import _ImmutableHeaders

    headers = _ImmutableHeaders(
        [("X-Multi", "one"), ("X-Multi", "two"), ("X-Multi", "three")]
    )
    assert headers.getlist("X-Multi") == ["one", "two", "three"]
    assert headers.getlist("x-multi") == ["one", "two", "three"]  # case-insensitive
    assert list(headers.items()) == [
        ("X-Multi", "one"),
        ("X-Multi", "two"),
        ("X-Multi", "three"),
    ]

    # Through a real request, duplicate headers are combined by WSGI
    with app.test_request_context(
        "/user/alice",
        headers=[("X-Multi", "one"), ("X-Multi", "two")],
    ):
        snap = snapshot_request(headers=["X-Multi"])
        # WSGI combines them into a single comma-separated value
        assert snap.headers["X-Multi"] == "one, two"


def test_snapshot_readable_after_request_ends(app):
    with app.test_request_context(
        "/user/alice?page=5",
        headers={"X-Custom": "world"},
    ):
        snap = snapshot_request(headers=["X-Custom"], args=["page"])

    # Request context has ended, but snapshot is still readable
    assert snap.endpoint == "user"
    assert snap.view_args == {"name": "alice"}
    assert snap.args["page"] == "5"
    assert snap.headers["X-Custom"] == "world"


def test_snapshot_route_identity(app):
    with app.test_request_context("/user/bob?page=1"):
        snap = snapshot_request(headers=[], args=["page"])

    assert snap.endpoint == "user"
    assert snap.view_args == {"name": "bob"}


def test_snapshot_no_references(app):
    with app.test_request_context("/user/alice"):
        snap = snapshot_request()

    # The snapshot must not reference session, app, or the original request
    assert not hasattr(snap, "session")
    assert not hasattr(snap, "app")
    assert not hasattr(snap, "request")
    assert not hasattr(RequestSnapshot, "session")
    assert not hasattr(RequestSnapshot, "app")
    assert not hasattr(RequestSnapshot, "request")


def test_snapshot_view_args_copied(app):
    with app.test_request_context("/user/alice"):
        snap = snapshot_request()
        original_view_args = flask.request.view_args

    # view_args is copied, not referenced
    assert snap.view_args == {"name": "alice"}
    assert snap.view_args is not original_view_args

    # Modifying the returned dict doesn't affect the snapshot
    va = snap.view_args
    va["extra"] = "value"
    assert "extra" not in snap.view_args


def test_snapshot_immutable(app):
    with app.test_request_context(
        "/user/alice?page=1",
        headers={"X-Custom": "hello"},
    ):
        snap = snapshot_request(headers=["X-Custom"], args=["page"])

    # Cannot modify headers or args
    with pytest.raises((TypeError, AttributeError)):
        snap.headers["X-Custom"] = "modified"

    with pytest.raises((TypeError, AttributeError)):
        snap.args["page"] = "modified"

    with pytest.raises((TypeError, AttributeError)):
        snap.headers["new"] = "value"

    with pytest.raises((TypeError, AttributeError)):
        snap.args["new"] = "value"


def test_snapshot_no_body_read(app):
    body = b"name=value&other=123"
    stream = CountingStream(body)

    with app.test_request_context(
        "/user/alice?a=1&a=2",
        method="POST",
        input_stream=stream,
        content_length=len(body),
        content_type="application/x-www-form-urlencoded",
    ):
        snap = snapshot_request(headers=["Content-Type"], args=["a"])

    # Creating the snapshot must not read the body
    assert stream.read_count == 0
    assert snap.args.getlist("a") == ["1", "2"]
    assert snap.headers["Content-Type"] == "application/x-www-form-urlencoded"


def test_snapshot_does_not_trigger_form_parsing(app):
    body = b"name=value&other=123"
    stream = CountingStream(body)

    with app.test_request_context(
        "/user/alice",
        method="POST",
        input_stream=stream,
        content_length=len(body),
        content_type="application/x-www-form-urlencoded",
    ):
        # Snapshot only reads headers and args, not form data
        snap = snapshot_request(headers=["Content-Type"])
        assert stream.read_count == 0
        # Form was never accessed, so it wasn't parsed
        assert not hasattr(flask.request, "_form") or flask.request._form is None


def test_bind_snapshot_makes_proxy_available(app):
    with app.test_request_context("/user/alice?page=1"):
        snap = snapshot_request(args=["page"])

    with bind_snapshot(snap):
        assert snapshot.args["page"] == "1"
        assert snapshot.endpoint == "user"


def test_bind_snapshot_unbound_error(app):
    with pytest.raises(Exception, match="snapshot"):
        snapshot.args


def test_bind_snapshot_nested_restores_outer(app):
    with app.test_request_context("/user/alice?page=1"):
        outer = snapshot_request(args=["page"])

    with app.test_request_context("/user/bob?page=2"):
        inner = snapshot_request(args=["page"])

    with bind_snapshot(outer):
        assert snapshot.args["page"] == "1"
        with bind_snapshot(inner):
            assert snapshot.args["page"] == "2"
        # After inner exits, outer is restored
        assert snapshot.args["page"] == "1"


def test_bind_snapshot_nested_exception_restores_outer(app):
    with app.test_request_context("/user/alice?page=1"):
        outer = snapshot_request(args=["page"])

    with app.test_request_context("/user/bob?page=2"):
        inner = snapshot_request(args=["page"])

    with bind_snapshot(outer):
        assert snapshot.args["page"] == "1"
        with pytest.raises(ValueError, match="boom"):
            with bind_snapshot(inner):
                assert snapshot.args["page"] == "2"
                raise ValueError("boom")
        # After inner exits with exception, outer is restored
        assert snapshot.args["page"] == "1"


def test_bind_snapshot_restores_on_exception(app):
    with app.test_request_context("/user/alice?page=1"):
        snap = snapshot_request(args=["page"])

    with pytest.raises(RuntimeError, match="fail"):
        with bind_snapshot(snap):
            assert snapshot.args["page"] == "1"
            raise RuntimeError("fail")

    # After the block exits with exception, snapshot is unbound
    with pytest.raises(Exception, match="snapshot"):
        snapshot.args


def test_bind_snapshot_async_isolation(app):
    with app.test_request_context("/user/alice?page=1"):
        snap1 = snapshot_request(args=["page"])

    with app.test_request_context("/user/bob?page=2"):
        snap2 = snapshot_request(args=["page"])

    async def task(snap, label):
        with bind_snapshot(snap):
            await asyncio.sleep(0)
            return label, snapshot.args["page"]

    async def main():
        return await asyncio.gather(task(snap1, "a"), task(snap2, "b"))

    results = asyncio.run(main())
    assert results == [("a", "1"), ("b", "2")]


def test_bind_snapshot_async_nested(app):
    with app.test_request_context("/user/alice?page=1"):
        outer = snapshot_request(args=["page"])

    with app.test_request_context("/user/bob?page=2"):
        inner = snapshot_request(args=["page"])

    async def task():
        with bind_snapshot(outer):
            assert snapshot.args["page"] == "1"
            await asyncio.sleep(0)
            with bind_snapshot(inner):
                assert snapshot.args["page"] == "2"
            assert snapshot.args["page"] == "1"
            return snapshot.args["page"]

    result = asyncio.run(task())
    assert result == "1"


def test_flask_request_proxy_not_replaced(app):
    original_type = type(flask.request)

    with app.test_request_context("/user/alice"):
        snap = snapshot_request()
        # flask.request still works in request context
        assert flask.request.endpoint == "user"

    with bind_snapshot(snap):
        # flask.request is still the original proxy (not replaced by snapshot)
        assert type(flask.request) is original_type
        # The snapshot proxy is separate from the request proxy
        assert type(snapshot) is type(flask.request)
        assert snapshot is not flask.request


def test_snapshot_request_explicit_request(app):
    with app.test_request_context("/user/alice?page=1"):
        request = flask.request._get_current_object()
        snap = snapshot_request(request, args=["page"])

    assert snap.args["page"] == "1"
    assert snap.endpoint == "user"


def test_snapshot_request_no_request_raises(app):
    # Outside a request context, snapshot_request() with no request raises
    with pytest.raises(Exception, match="request context"):
        snapshot_request()


def test_snapshot_repr(app):
    with app.test_request_context(
        "/user/alice?page=1",
        headers={"X-Custom": "hello"},
    ):
        snap = snapshot_request(headers=["X-Custom"], args=["page"])

    r = repr(snap)
    assert "RequestSnapshot" in r
    assert "endpoint" in r
    assert "user" in r


def test_snapshot_empty(app):
    with app.test_request_context("/user/alice"):
        snap = snapshot_request()

    assert len(snap.headers) == 0
    assert len(snap.args) == 0
    assert snap.endpoint == "user"
    assert snap.view_args == {"name": "alice"}
