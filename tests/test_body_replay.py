import io
import tempfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from werkzeug.exceptions import ClientDisconnected
from werkzeug.exceptions import RequestEntityTooLarge

import flask
from flask import Flask
from flask import request
from flask.body_replay import RequestBodyReplayMiddleware


@pytest.fixture
def replay_app():
    app = Flask(__name__)
    app.config["TESTING"] = True

    @app.post("/echo")
    def echo():
        return request.get_data()

    @app.post("/json")
    def json_view():
        return flask.jsonify(request.get_json())

    @app.post("/form")
    def form_view():
        return request.form["field"]

    @app.post("/multi")
    def multi_view():
        first = request.get_data()
        second = request.get_data()
        third = request.get_data()
        assert first == second == third
        return first

    @app.post("/small")
    def small_view():
        return request.get_data()

    @app.post("/unlimited")
    def unlimited_view():
        return str(len(request.get_data()))

    @app.errorhandler(413)
    def too_large(e):
        return "handled: too big", 413

    app.wsgi_app = RequestBodyReplayMiddleware(
        app.wsgi_app,
        max_bytes=1024,
        memory_threshold=256,
        routes={
            "/small": 16,
            "/unlimited": {"max_bytes": None},
        },
    )
    return app


@pytest.fixture
def replay_client(replay_app):
    return replay_app.test_client()


def test_empty_body(replay_client):
    rv = replay_client.post("/echo")
    assert rv.status_code == 200
    assert rv.data == b""


def test_empty_body_explicit_length(replay_client):
    rv = replay_client.post("/echo", data=b"")
    assert rv.status_code == 200
    assert rv.data == b""


def test_replay_from_memory(replay_client):
    body = b"x" * 100
    rv = replay_client.post("/echo", data=body)
    assert rv.status_code == 200
    assert rv.data == body


def test_replay_from_disk(replay_client, monkeypatch):
    created = []
    real_temp_file = tempfile.TemporaryFile

    def tracking_temp_file(*args, **kwargs):
        f = real_temp_file(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr("flask.body_replay.tempfile.TemporaryFile", tracking_temp_file)
    body = b"y" * 1000
    rv = replay_client.post("/echo", data=body)
    assert rv.status_code == 200
    assert rv.data == body
    assert len(created) == 1
    assert created[0].closed


def test_json_consistent(replay_client):
    rv = replay_client.post("/json", json={"a": 1, "b": [2, 3]})
    assert rv.status_code == 200
    assert rv.get_json() == {"a": 1, "b": [2, 3]}


def test_form_consistent(replay_client):
    rv = replay_client.post("/form", data={"field": "value"})
    assert rv.status_code == 200
    assert rv.data == b"value"


def test_repeated_reads_consistent(replay_client):
    body = b"repeat me" * 40
    rv = replay_client.post("/multi", data=body)
    assert rv.status_code == 200
    assert rv.data == body


def test_chunked_body(replay_client):
    chunks = [b"alpha", b"beta", b"gamma"]
    rv = replay_client.open(
        "/echo",
        method="POST",
        environ_overrides={
            "wsgi.input": io.BytesIO(b"".join(chunks)),
            "wsgi.input_terminated": True,
            "CONTENT_LENGTH": "",
            "HTTP_TRANSFER_ENCODING": "chunked",
        },
    )
    assert rv.status_code == 200
    assert rv.data == b"".join(chunks)


def test_over_limit_returns_413(replay_client):
    rv = replay_client.post("/echo", data=b"z" * 1025)
    assert rv.status_code == 413
    assert rv.data == b"handled: too big"


def test_exact_limit_allowed(replay_client):
    body = b"z" * 1024
    rv = replay_client.post("/echo", data=body)
    assert rv.status_code == 200
    assert rv.data == body


def test_route_specific_limit(replay_client):
    assert replay_client.post("/small", data=b"s" * 16).status_code == 200
    rv = replay_client.post("/small", data=b"s" * 17)
    assert rv.status_code == 413
    assert rv.data == b"handled: too big"


def test_route_unlimited(replay_client):
    rv = replay_client.post("/unlimited", data=b"u" * 5000)
    assert rv.status_code == 200
    assert rv.data == b"5000"


class _FailingStream:
    """Stream that fails with a connection error after ``fail_at`` bytes."""

    def __init__(self, data, fail_at):
        self._data = data
        self._fail_at = fail_at
        self._pos = 0
        self.closed = False

    def read(self, size=-1):
        if self._pos >= self._fail_at:
            raise ConnectionError("client went away")
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 2:
            self._pos = len(self._data) + offset
        elif whence == 0:
            self._pos = offset
        return self._pos

    def close(self):
        self.closed = True


def test_client_disconnect_releases_resources(replay_app, monkeypatch):
    created = []
    real_temp_file = tempfile.TemporaryFile

    def tracking_temp_file(*args, **kwargs):
        f = real_temp_file(*args, **kwargs)
        created.append(f)
        return f

    monkeypatch.setattr("flask.body_replay.tempfile.TemporaryFile", tracking_temp_file)
    data = b"d" * 1000
    stream = _FailingStream(data, fail_at=500)
    client = replay_app.test_client()
    rv = client.open(
        "/echo",
        method="POST",
        input_stream=stream,
        content_length=len(data),
    )
    # ClientDisconnected is a 400 through Flask's normal handling.
    assert rv.status_code == 400
    assert stream.closed
    assert len(created) == 1
    assert created[0].closed


def test_concurrent_requests_isolated(replay_app):
    bodies = [bytes([i]) * 1000 for i in range(16)]

    def post(body):
        client = replay_app.test_client()
        rv = client.post("/echo", data=body)
        return rv.status_code, rv.data

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, bodies))

    for body, (status, data) in zip(bodies, results, strict=True):
        assert status == 200
        assert data == body


def test_plain_wsgi_app_replay():
    seen = []

    def wsgi_app(environ, start_response):
        try:
            body = environ["wsgi.input"].read()
        except RequestEntityTooLarge:
            start_response("413 Request Entity Too Large", [])
            return [b"too big"]
        seen.append(body)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [body]

    mw = RequestBodyReplayMiddleware(wsgi_app, max_bytes=10)

    def call(body):
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }
        status = []
        result = mw(environ, lambda s, h, e=None: status.append(s))
        payload = b"".join(result)
        if hasattr(result, "close"):
            result.close()
        return status[0], payload

    assert call(b"ok") == ("200 OK", b"ok")
    assert call(b"this is way too long") == ("413 Request Entity Too Large", b"too big")
    assert seen == [b"ok"]


def test_error_stream_raises_client_disconnected():
    from flask.body_replay import _ErrorStream

    stream = _ErrorStream(ClientDisconnected())
    with pytest.raises(ClientDisconnected):
        stream.read()
    with pytest.raises(ClientDisconnected):
        stream.readline()
    stream.close()
