import io
import threading

import pytest
from werkzeug.exceptions import ClientDisconnected
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.test import EnvironBuilder
from werkzeug.test import run_wsgi_app

from flask import Flask
from flask import jsonify
from flask import request
from flask.body_replay import BodyReplayMiddleware
from flask.body_replay import BodyReplayStream


class TrackingStream:
    """A raw WSGI input stream that records how it was consumed."""

    def __init__(self, data, *, fail=False):
        self._buffer = io.BytesIO(data)
        self.read_calls = 0
        self.closed = False
        self._fail = fail

    def read(self, size=-1):
        self.read_calls += 1

        if self._fail:
            raise OSError("connection reset")

        return self._buffer.read(size)

    def close(self):
        self.closed = True
        self._buffer.close()


class ChunkedStream:
    """A non-seekable stream that yields the body in chunks, like a
    chunked request without a Content-Length.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, size=-1):
        if not self._chunks:
            return b""

        return self._chunks.pop(0)


def test_stream_reads_and_replays():
    raw = TrackingStream(b"line one\nline two\n")
    stream = BodyReplayStream(raw, content_length=18)

    assert stream.readline() == b"line one\n"
    assert stream.read(4) == b"line"
    assert stream.read() == b" two\n"
    assert stream.read() == b""

    first_pass_calls = raw.read_calls
    stream.seek(0)
    assert stream.read() == b"line one\nline two\n"
    # Replaying buffered data must not consume the raw stream again.
    assert raw.read_calls == first_pass_calls

    stream.close()
    assert stream.closed
    assert raw.closed


def test_stream_empty_body():
    stream = BodyReplayStream(io.BytesIO(b""), content_length=0)
    assert stream.read() == b""
    assert stream.readline() == b""
    stream.close()


def test_stream_limit_allows_exact_size():
    stream = BodyReplayStream(io.BytesIO(b"12345"), max_bytes=5, content_length=5)
    assert stream.read() == b"12345"
    stream.close()


def test_stream_limit_exceeded_raises_immediately():
    raw = TrackingStream(b"x" * 100)
    stream = BodyReplayStream(raw, max_bytes=10, content_length=100)

    with pytest.raises(RequestEntityTooLarge):
        stream.read()

    # The failure is deterministic and the resources are released.
    with pytest.raises(RequestEntityTooLarge):
        stream.read()

    assert stream.closed
    assert raw.closed


def test_stream_disconnect_short_body():
    raw = TrackingStream(b"short")
    stream = BodyReplayStream(raw, content_length=100)

    with pytest.raises(ClientDisconnected):
        stream.read()

    assert stream.closed
    assert raw.closed


def test_stream_disconnect_read_error():
    stream = BodyReplayStream(TrackingStream(b"", fail=True), content_length=10)

    with pytest.raises(ClientDisconnected):
        stream.read()

    assert stream.closed


def test_stream_disk_rollover_and_cleanup(tmp_path):
    stream = BodyReplayStream(
        io.BytesIO(b"x" * 100), memory_threshold=1, temp_dir=str(tmp_path)
    )
    assert stream.read() == b"x" * 100
    # The body spilled over to a temporary file on disk.
    assert len(list(tmp_path.iterdir())) == 1

    stream.seek(0)
    assert stream.read(10) == b"x" * 10
    stream.close()
    # Closing releases the temporary file.
    assert list(tmp_path.iterdir()) == []


def test_stream_failure_releases_temp_file(tmp_path):
    stream = BodyReplayStream(
        io.BytesIO(b"x" * 100),
        max_bytes=10,
        memory_threshold=1,
        temp_dir=str(tmp_path),
    )

    with pytest.raises(RequestEntityTooLarge):
        stream.read()

    assert list(tmp_path.iterdir()) == []


def test_streams_do_not_share_buffers():
    first = BodyReplayStream(io.BytesIO(b"aaa"))
    second = BodyReplayStream(io.BytesIO(b"bbb"))

    assert first.read(1) == b"a"
    assert second.read(1) == b"b"
    assert first.read() == b"aa"
    assert second.read() == b"bb"
    first.close()
    second.close()


def test_middleware_route_limit_resolution():
    middleware = BodyReplayMiddleware(
        lambda e, s: [],
        max_bytes=100,
        route_limits={"/uploads": 1000, "/uploads/small": 10, "/open": None},
    )

    assert middleware.max_bytes_for("/other") == 100
    assert middleware.max_bytes_for("/uploads/file") == 1000
    # The longest matching prefix wins.
    assert middleware.max_bytes_for("/uploads/small/file") == 10
    assert middleware.max_bytes_for("/open/anything") is None


@pytest.fixture
def replay_app(tmp_path):
    app = Flask(__name__)

    @app.post("/echo")
    def echo():
        data = request.get_data()
        # Repeated reads through the replay buffer are consistent.
        assert request.get_data() == data
        return data

    @app.post("/json")
    def json_view():
        parsed = request.get_json()
        raw = request.get_data()
        return jsonify(parsed=parsed, raw=raw.decode())

    @app.post("/form")
    def form():
        return jsonify(request.form.to_dict())

    @app.post("/mixed")
    def mixed():
        raw = request.get_data()
        # get_data caches the body, so form parsing sees the same content.
        return jsonify(raw=len(raw), form=request.form.to_dict())

    @app.errorhandler(413)
    def too_large(e):
        return "custom-413", 413

    app.wsgi_app = BodyReplayMiddleware(
        app.wsgi_app,
        max_bytes=64,
        route_limits={"/uploads": 1024},
        memory_threshold=16,
        temp_dir=str(tmp_path),
    )
    return app


def test_empty_body(replay_app):
    client = replay_app.test_client()
    assert client.post("/echo").data == b""
    assert client.post("/form").json == {}


def test_repeated_reads_are_consistent(replay_app):
    client = replay_app.test_client()
    assert client.post("/echo", data=b"hello").data == b"hello"

    rv = client.post("/json", json={"a": 1})
    assert rv.json["parsed"] == {"a": 1}
    assert rv.json["raw"] == '{"a": 1}'

    assert client.post("/form", data={"x": "1", "y": "2"}).json == {
        "x": "1",
        "y": "2",
    }

    rv = client.post("/mixed", data={"x": "1"})
    assert rv.json["raw"] == len("x=1")
    assert rv.json["form"] == {"x": "1"}


def test_chunked_body(replay_app):
    builder = EnvironBuilder(path="/echo", method="POST")
    environ = builder.get_environ()
    environ["wsgi.input"] = ChunkedStream([b"chunk1-", b"chunk2-", b"chunk3"])
    environ.pop("CONTENT_LENGTH", None)
    environ["wsgi.input_terminated"] = True

    app_iter, status, headers = run_wsgi_app(replay_app.wsgi_app, environ)
    body = b"".join(app_iter)

    assert status == "200 OK"
    assert body == b"chunk1-chunk2-chunk3"


def test_over_limit_returns_413_through_error_handling(replay_app):
    client = replay_app.test_client()
    rv = client.post("/echo", data=b"x" * 65)
    # The 413 is produced by Flask's regular exception handling, so the
    # registered error handler is used.
    assert rv.status_code == 413
    assert rv.data == b"custom-413"


def test_limit_boundary(replay_app):
    client = replay_app.test_client()
    assert client.post("/echo", data=b"x" * 64).status_code == 200
    assert client.post("/echo", data=b"x" * 65).status_code == 413


def test_route_limit_overrides_default(replay_app, tmp_path):
    @replay_app.post("/uploads/file")
    def upload():
        return request.get_data()

    client = replay_app.test_client()
    rv = client.post("/uploads/file", data=b"x" * 200)
    assert rv.status_code == 200
    assert rv.data == b"x" * 200


def test_disconnect_returns_400_and_releases_temp_file(replay_app, tmp_path):
    builder = EnvironBuilder(
        path="/echo",
        method="POST",
        input_stream=io.BytesIO(b"short"),
    )
    environ = builder.get_environ()
    # The client announced more bytes than it actually sent.
    environ["CONTENT_LENGTH"] = "100"

    app_iter, status, headers = run_wsgi_app(replay_app.wsgi_app, environ)
    b"".join(app_iter)

    assert status.startswith("400")
    assert list(tmp_path.iterdir()) == []


def test_over_limit_releases_temp_file(replay_app, tmp_path):
    client = replay_app.test_client()
    assert client.post("/echo", data=b"x" * 100).status_code == 413
    assert list(tmp_path.iterdir()) == []


def test_temp_file_released_after_request(replay_app, tmp_path):
    client = replay_app.test_client()
    assert client.post("/echo", data=b"x" * 32).data == b"x" * 32
    assert list(tmp_path.iterdir()) == []


def test_concurrent_requests_have_isolated_buffers(replay_app, tmp_path):
    errors = []

    def work(number):
        try:
            client = replay_app.test_client()

            for iteration in range(5):
                # Bodies exceed the memory threshold to force disk spooling.
                body = f"thread-{number}-{iteration}".encode() * 4
                rv = client.post("/echo", data=body)
                assert rv.status_code == 200
                assert rv.data == body
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert not errors
    # Every temporary file was released after its request finished.
    assert list(tmp_path.iterdir()) == []
