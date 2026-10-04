import gc
import io
import os
import queue
import threading
import weakref

import pytest
from werkzeug.datastructures import FileStorage

import flask
from flask import Flask
from flask import has_request_context
from flask.handoff import HandoffCopyError
from flask.handoff import HandoffFieldsError
from flask.handoff import HandoffFileError
from flask.handoff import HandoffLimitError
from flask.handoff import RequestHandoff
from flask.handoff import stage_handoff


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    return app


def _upload(data, filename):
    return (io.BytesIO(data), filename)


# ---------------------------------------------------------------------------
# Acceptance: background work after the request ended
# ---------------------------------------------------------------------------


def test_readable_after_request_context_exits(app, tmp_path):
    """Two small files staged during the request are readable later."""
    first = b"first file contents"
    second = b"second file contents"

    results: queue.Queue[object] = queue.Queue()

    @app.post("/submit")
    def submit():
        handoff = stage_handoff(
            form_fields=("title",),
            file_fields=("doc", "image"),
            max_file_size=1024,
            max_total_size=4096,
            parent_dir=tmp_path,
        )
        worker = threading.Thread(target=_consume, args=(handoff, results))
        worker.start()
        worker.join()
        return "accepted", 202

    def _consume(handoff: RequestHandoff, out: queue.Queue) -> None:
        # Runs with the request context already torn down.
        assert not has_request_context()
        out.put(
            {
                "form": handoff.form["title"],
                "doc": handoff.get("doc").read_bytes(),
                "image": handoff.get("image").read_bytes(),
            }
        )
        handoff.close()

    client = app.test_client()
    response = client.post(
        "/submit",
        data={
            "title": "hello",
            "doc": _upload(first, "a.txt"),
            "image": _upload(second, "b.bin"),
        },
        content_type="multipart/form-data",
    )
    assert response.status_code == 202

    assert results.get_nowait() == {
        "form": "hello",
        "doc": first,
        "image": second,
    }
    # Nothing is left behind in the controlled area.
    assert os.listdir(tmp_path) == []


def test_actual_background_thread_reads_after_response(app, tmp_path):
    """The handoff really survives being handed to another thread."""
    started = threading.Event()
    done = threading.Event()
    results: queue.Queue[bytes] = queue.Queue()

    def worker(handoff: RequestHandoff) -> None:
        started.set()
        started.wait(1)
        results.put(handoff.get("doc").read_bytes())
        handoff.close()
        done.set()

    @app.post("/submit")
    def submit():
        handoff = stage_handoff(
            file_fields=("doc",),
            max_file_size=1024,
            max_total_size=1024,
            parent_dir=tmp_path,
        )
        thread = threading.Thread(target=worker, args=(handoff,))
        thread.start()
        return "accepted", 202

    response = app.test_client().post(
        "/submit",
        data={"doc": (io.BytesIO(b"payload"), "doc.txt")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 202
    assert done.wait(2)
    assert results.get_nowait() == b"payload"
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Acceptance: total quota failure publishes nothing
# ---------------------------------------------------------------------------


def test_total_budget_exceeded_publishes_nothing(app, tmp_path):
    """The second file pushes the total over: no object, empty area."""
    with app.test_request_context(
        "/",
        method="POST",
        data={
            "doc": (io.BytesIO(b"a" * 10), "a.txt"),
            "image": (io.BytesIO(b"b" * 10), "b.bin"),
        },
        content_type="multipart/form-data",
    ):
        before = set(os.listdir(tmp_path))
        with pytest.raises(HandoffLimitError) as exc:
            stage_handoff(
                file_fields=("doc", "image"),
                max_file_size=100,
                max_total_size=15,
                parent_dir=tmp_path,
            )
        assert not exc.value.per_file
        assert exc.value.limit == 15

    assert set(os.listdir(tmp_path)) == before


def test_per_file_limit_failure_is_clean(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"x" * 50), "big.txt")},
        content_type="multipart/form-data",
    ):
        with pytest.raises(HandoffLimitError) as exc:
            stage_handoff(
                file_fields=("doc",),
                max_file_size=10,
                max_total_size=1000,
                parent_dir=tmp_path,
            )
        assert exc.value.per_file
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Acceptance: close() is idempotent
# ---------------------------------------------------------------------------


def test_close_is_idempotent(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"data"), "a.txt")},
        content_type="multipart/form-data",
    ):
        handoff = stage_handoff(
            file_fields=("doc",),
            max_file_size=100,
            max_total_size=100,
            parent_dir=tmp_path,
        )
        directory = handoff.directory
        assert os.path.isdir(directory)
        handoff.close()
        assert handoff.closed
        handoff.close()  # repeated call: no side effect, no error
        handoff.close()
    assert not os.path.exists(directory)
    assert os.listdir(tmp_path) == []


def test_context_manager_closes(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"data"), "a.txt")},
        content_type="multipart/form-data",
    ):
        with stage_handoff(
            file_fields=("doc",),
            max_file_size=100,
            max_total_size=100,
            parent_dir=tmp_path,
        ) as handoff:
            directory = handoff.directory
            assert handoff.get("doc").read_bytes() == b"data"
        assert handoff.closed
    assert not os.path.exists(directory)


# ---------------------------------------------------------------------------
# Acceptance: malicious filenames are metadata only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../../../etc/passwd",
        "..\\..\\Windows\\System32\\evil.dll",
        "/absolute/path/evil.sh",
        "C:\\Windows\\evil.exe",
        "....//....//escape.txt",
        "con",
    ],
)
def test_malicious_filename_never_used_as_path(app, tmp_path, bad_name):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"safe bytes"), bad_name)},
        content_type="multipart/form-data",
    ):
        handoff = stage_handoff(
            file_fields=("doc",),
            max_file_size=100,
            max_total_size=100,
            parent_dir=tmp_path,
        )
        staged = handoff.get("doc")
        # Original name preserved purely as metadata.
        assert staged.filename == bad_name
        # Real path stays inside the staging directory...
        assert os.path.commonpath([staged.path, handoff.directory]) == os.path.abspath(
            handoff.directory
        )
        # ...and none of its components come from the client name.
        assert os.path.basename(staged.path) not in {
            "passwd",
            "evil.dll",
            "evil.sh",
            "evil.exe",
            "escape.txt",
            "con",
        }
        assert staged.read_bytes() == b"safe bytes"
        handoff.close()
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Acceptance: missing fields
# ---------------------------------------------------------------------------


def test_missing_form_field(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"present": "yes", "doc": (io.BytesIO(b"x"), "a.txt")},
        content_type="multipart/form-data",
    ):
        with pytest.raises(HandoffFieldsError) as exc:
            stage_handoff(
                form_fields=("present", "absent"),
                file_fields=("doc",),
                max_file_size=10,
                max_total_size=10,
                parent_dir=tmp_path,
            )
        assert exc.value.missing == ["absent"]
    # Validation happens before any staging directory is created.
    assert os.listdir(tmp_path) == []


def test_empty_form_field_is_missing(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"title": ""},
        content_type="multipart/form-data",
    ):
        with pytest.raises(HandoffFieldsError) as exc:
            stage_handoff(
                form_fields=("title",),
                max_file_size=10,
                max_total_size=10,
                parent_dir=tmp_path,
            )
        assert exc.value.missing == ["title"]


def test_missing_file_field(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"x"), "a.txt")},
        content_type="multipart/form-data",
    ):
        with pytest.raises(HandoffFileError) as exc:
            stage_handoff(
                file_fields=("doc", "avatar"),
                max_file_size=10,
                max_total_size=10,
                parent_dir=tmp_path,
            )
        assert exc.value.missing == ["avatar"]
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Acceptance: copy interrupted mid-stream
# ---------------------------------------------------------------------------


class ExplodingStream(io.BytesIO):
    """Delivers ``fail_at`` bytes, then raises OSError on the next read."""

    def __init__(self, data: bytes, fail_at: int) -> None:
        super().__init__(data)
        self._until_fail = fail_at

    def read(self, size=-1):  # type: ignore[override]
        if size is None or size < 0:
            size = self._until_fail
        n = min(size, self._until_fail)
        if n == 0:
            raise OSError("simulated disk failure")
        chunk = super().read(n)
        self._until_fail -= len(chunk)
        if not chunk:
            raise OSError("simulated disk failure")
        return chunk


def test_interrupted_copy_cleans_partial_file(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"x" * 100), "a.txt")},
        content_type="multipart/form-data",
    ):
        # Swap in a stream that dies after 20 bytes, mid-copy.
        request_file = flask.request.files["doc"]
        original = request_file.stream
        request_file.stream = ExplodingStream(b"x" * 100, 20)
        original.close()

        with pytest.raises(HandoffCopyError) as exc:
            stage_handoff(
                file_fields=("doc",),
                max_file_size=10_000,
                max_total_size=10_000,
                parent_dir=tmp_path,
            )
        assert exc.value.field == "doc"
        assert isinstance(exc.value.original, OSError)
    assert os.listdir(tmp_path) == []


def test_interrupted_copy_after_first_file_is_clean(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={
            "first": (io.BytesIO(b"ok"), "a.txt"),
            "second": (io.BytesIO(b"x" * 100), "b.txt"),
        },
        content_type="multipart/form-data",
    ):
        original = flask.request.files["second"].stream
        flask.request.files["second"].stream = ExplodingStream(b"x" * 100, 10)
        original.close()
        with pytest.raises(HandoffCopyError):
            stage_handoff(
                file_fields=("first", "second"),
                max_file_size=10_000,
                max_total_size=10_000,
                parent_dir=tmp_path,
            )
    # The already-copied first file is removed as well.
    assert os.listdir(tmp_path) == []


# ---------------------------------------------------------------------------
# Misc behavior
# ---------------------------------------------------------------------------


def test_no_live_handle_or_request_referenced(app, tmp_path):
    with app.test_request_context(
        "/",
        method="POST",
        data={"doc": (io.BytesIO(b"abc"), "a.txt")},
        content_type="multipart/form-data",
    ):
        req_ref = weakref.ref(flask.request._get_current_object())
        handoff = stage_handoff(
            form_fields=(),
            file_fields=("doc",),
            max_file_size=10,
            max_total_size=10,
            parent_dir=tmp_path,
        )
    gc.collect()
    # The handoff does not keep the request alive.
    assert req_ref() is None
    assert handoff.get("doc").size == 3
    handoff.close()


def test_explicit_req_argument(tmp_path):
    storage = FileStorage(
        stream=io.BytesIO(b"explicit"),
        filename="c.txt",
        name="doc",
    )

    class FakeRequest:
        form = {}
        files = {"doc": storage}

    # A request-like object can be passed explicitly, so staging does not
    # depend on the request context proxy.
    handoff = stage_handoff(
        FakeRequest(),  # type: ignore[arg-type]
        file_fields=("doc",),
        max_file_size=100,
        max_total_size=100,
        parent_dir=tmp_path,
    )
    try:
        assert handoff.get("doc").read_bytes() == b"explicit"
    finally:
        handoff.close()
    assert os.listdir(tmp_path) == []


def test_negative_limits_rejected(app, tmp_path):
    with app.test_request_context("/"):
        with pytest.raises(ValueError):
            stage_handoff(
                max_file_size=-1,
                max_total_size=10,
                parent_dir=tmp_path,
            )
