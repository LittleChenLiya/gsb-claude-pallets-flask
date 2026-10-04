import asyncio
import io
from types import MappingProxyType

import pytest

import flask
from flask import bind_snapshot
from flask import current_snapshot
from flask import request
from flask import RequestSnapshot
from flask import session
from flask import snapshot_request
from flask.wrappers import Request


def make_snapshot(tag, **kwargs):
    values = {
        "method": "GET",
        "path": f"/{tag}",
        "endpoint": tag,
        "blueprint": None,
        "view_args": None,
        "headers": [("X-Tag", tag)],
        "query": [("k", tag)],
    }
    values.update(kwargs)
    return RequestSnapshot(**values)


class CountingStream(io.BytesIO):
    """A body stream that counts how many times it is read."""

    def __init__(self, data):
        super().__init__(data)
        self.reads = 0

    def read(self, *args):
        self.reads += 1
        return super().read(*args)

    def readline(self, *args):
        self.reads += 1
        return super().readline(*args)


def test_capture_allowed_fields_only(app):
    @app.route("/items/<int:item_id>")
    def item(item_id):
        return ""

    with app.test_request_context(
        "/items/3?tag=a&other=1&tag=b",
        headers={"X-Token": "abc", "X-Secret": "hidden"},
    ):
        snap = snapshot_request(headers=["X-Token"], query=["tag"])

        # allowed fields are captured
        assert snap.get_header("X-Token") == "abc"
        assert snap.get_query_all("tag") == ("a", "b")

        # route identity is captured
        assert snap.method == "GET"
        assert snap.path == "/items/3"
        assert snap.endpoint == "item"
        assert snap.blueprint is None
        assert snap.view_args == {"item_id": 3}

        # anything not allowed is absent
        assert snap.get_header("X-Secret") is None
        assert snap.get_header("X-Secret", "default") == "default"
        assert snap.get_headers("X-Secret") == ()
        assert snap.get_query("other") is None
        assert snap.get_query("other", "default") == "default"
        assert snap.get_query_all("other") == ()
        assert ("X-Secret", "hidden") not in snap.headers
        assert ("other", "1") not in snap.query


def test_repeated_values_keep_input_order(app, client):
    captured = []

    @app.route("/items")
    def items():
        captured.append(snapshot_request(headers=["X-Tag"], query=["tag"]))
        return ""

    client.get(
        "/items?tag=a&skip=1&tag=b&tag=c",
        headers=[("X-Tag", "x"), ("X-Other", "z")],
    )

    snap = captured[0]
    assert snap.query == (("tag", "a"), ("tag", "b"), ("tag", "c"))
    assert snap.get_query_all("tag") == ("a", "b", "c")
    assert snap.get_query("tag") == "a"
    assert snap.headers == (("X-Tag", "x"),)
    assert snap.get_headers("x-tag") == ("x",)


def test_repeated_headers_keep_input_order(app):
    # WSGI servers may pass repeated headers through as multiple values,
    # their order must be kept.
    from werkzeug.datastructures import Headers

    with app.test_request_context("/"):
        req = request._get_current_object()
        req.headers = Headers([("X-Tag", "x"), ("X-Other", "z"), ("X-Tag", "y")])

        snap = snapshot_request(headers=["X-Tag"], request=req)

        assert snap.headers == (("X-Tag", "x"), ("X-Tag", "y"))
        assert snap.get_headers("X-Tag") == ("x", "y")
        assert snap.get_header("X-Tag") == "x"


def test_snapshot_readable_after_request_ends(app, client):
    captured = []

    @app.route("/items")
    def items():
        captured.append(snapshot_request(headers=["X-Token"], query=["tag"]))
        return ""

    response = client.get(
        "/items?tag=a&tag=b&secret=1",
        headers={"X-Token": "abc", "X-Internal": "no"},
    )
    assert response.status_code == 200

    # The request context is gone, but the snapshot is still readable.
    snap = captured[0]
    assert snap.get_header("X-Token") == "abc"
    assert snap.get_header("X-Internal") is None
    assert snap.get_query_all("tag") == ("a", "b")
    assert snap.get_query("secret") is None
    assert snap.endpoint == "items"

    # And it can be bound outside of any request context.
    with snap.bind():
        assert current_snapshot.endpoint == "items"
        assert current_snapshot.get_header("X-Token") == "abc"


def test_snapshot_does_not_read_body(app):
    stream = CountingStream(b'{"secret": true}')

    with app.test_request_context(
        "/items?tag=a",
        method="POST",
        input_stream=stream,
        content_type="application/json",
        headers={"X-Token": "abc"},
    ):
        snap = snapshot_request(headers=["X-Token"], query=["tag"])

        # The body stream was never read, and lazy form parsing (which
        # would consume the stream) was never triggered.
        assert stream.reads == 0
        assert "form" not in request.__dict__
        assert "files" not in request.__dict__

        assert snap.get_header("X-Token") == "abc"
        assert snap.get_query("tag") == "a"


def test_snapshot_has_no_live_references(app):
    with app.test_request_context("/?tag=a", headers={"X-Token": "t"}):
        snap = snapshot_request(headers=["X-Token"], query=["tag"])

        for name in RequestSnapshot.__slots__:
            value = getattr(snap, name)
            assert value is not request
            assert value is not session
            assert not isinstance(value, Request)
            assert isinstance(value, (str, tuple, MappingProxyType, type(None)))

        # view_args is a copy, not the request's dict
        assert snap.view_args is not request.view_args


def test_snapshot_is_read_only():
    snap = make_snapshot("a", view_args={"id": 1})

    with pytest.raises(AttributeError):
        snap.method = "POST"

    with pytest.raises(AttributeError):
        snap.extra = 1

    with pytest.raises(AttributeError):
        del snap.method

    with pytest.raises(TypeError):
        snap.view_args["id"] = 2

    assert isinstance(snap.headers, tuple)
    assert isinstance(snap.query, tuple)
    assert snap.method == "GET"
    assert snap.view_args == {"id": 1}


def test_bind_and_unbind():
    snap = make_snapshot("a")

    with pytest.raises(RuntimeError):
        current_snapshot.get_query("k")

    with snap.bind() as bound:
        assert bound is snap
        assert current_snapshot._get_current_object() is snap
        assert current_snapshot.get_query("k") == "a"

    with pytest.raises(RuntimeError):
        current_snapshot.get_query("k")


def test_bind_snapshot_function():
    snap = make_snapshot("a")

    with bind_snapshot(snap):
        assert current_snapshot._get_current_object() is snap

    with pytest.raises(RuntimeError):
        current_snapshot.get_query("k")


def test_nested_binding_restores_on_exception():
    outer = make_snapshot("outer")
    inner = make_snapshot("inner")

    with outer.bind():
        assert current_snapshot.get_query("k") == "outer"

        with pytest.raises(RuntimeError, match="boom"):
            with inner.bind():
                assert current_snapshot.get_query("k") == "inner"
                raise RuntimeError("boom")

        # the inner binding exited through an exception, the outer
        # binding is restored
        assert current_snapshot.get_query("k") == "outer"

    with pytest.raises(RuntimeError):
        current_snapshot.get_query("k")


def test_nested_binding_restores_on_normal_exit():
    outer = make_snapshot("outer")
    inner = make_snapshot("inner")

    with outer.bind():
        with inner.bind():
            assert current_snapshot.get_query("k") == "inner"

        assert current_snapshot.get_query("k") == "outer"


def test_async_tasks_are_isolated():
    snap_a = make_snapshot("a")
    snap_b = make_snapshot("b")
    seen = {}

    async def worker(name, snap, ready, proceed):
        async with snap.bind():
            # both tasks are bound before either reads
            ready.set()
            await proceed.wait()
            seen[name] = current_snapshot.get_query("k")

    async def main():
        ready_a = asyncio.Event()
        ready_b = asyncio.Event()
        proceed = asyncio.Event()

        async def wait_for_both():
            await ready_a.wait()
            await ready_b.wait()
            proceed.set()

        await asyncio.gather(
            worker("a", snap_a, ready_a, proceed),
            worker("b", snap_b, ready_b, proceed),
            wait_for_both(),
        )

    asyncio.run(main())

    assert seen == {"a": "a", "b": "b"}

    with pytest.raises(RuntimeError):
        current_snapshot.get_query("k")


def test_async_exception_restores_outer_binding():
    outer = make_snapshot("outer")
    inner = make_snapshot("inner")
    seen = []

    async def main():
        async with outer.bind():
            try:
                async with inner.bind():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass

            seen.append(current_snapshot.get_query("k"))

    asyncio.run(main())
    assert seen == ["outer"]


def test_request_proxy_is_not_replaced(app):
    proxy_before = flask.request

    with app.test_request_context("/?tag=a"):
        real_request = request._get_current_object()
        snap = snapshot_request(query=["tag"])

        with snap.bind():
            # flask.request is untouched and still resolves to the request
            assert flask.request is proxy_before
            assert request._get_current_object() is real_request
            assert request.method == "GET"
            # the snapshot lives behind its own proxy
            assert current_snapshot._get_current_object() is snap
