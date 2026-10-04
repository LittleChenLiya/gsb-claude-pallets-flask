"""Tests for :mod:`flask.handoff` -- explicit handoff of form data and
uploaded files out of a request.

The acceptance scenarios covered here:

* Two small files are handed off; after the request context is popped a
  background thread still reads the original content.
* When a file would exceed the total size limit, no handoff is returned and
  the temporary area is empty.
* Closing the handoff more than once has no effect.
* Malicious filenames cannot escape the temporary area.
* Fields that are not selected are not copied; fields that are selected but
  missing from the request are simply absent.
* A failure while copying removes the partially copied area and publishes no
  handoff.
"""

from __future__ import annotations

import io
import os
import tempfile
import threading

import pytest

from flask import Flask
from flask import HandoffError
from flask import request


def _read(handoff_file) -> bytes:
    """Read a copied file, closing the handle afterwards."""
    with handoff_file.open() as f:
        return f.read()


def _post(app: Flask, data: dict, directory: str | None = None, **kwargs):
    """Post a multipart request to a view that hands off selected fields."""
    holder: dict = {}

    @app.post("/upload")
    def upload():
        holder["handoff"] = request.handoff(
            kwargs.pop("fields", None),
            directory=directory,
            **kwargs,
        )
        return "ok"

    client = app.test_client()
    response = client.post("/upload", data=data)
    return response, holder


def test_handoff_copies_selected_fields_and_files(app, tmp_path):
    response, holder = _post(
        app,
        {
            "description": "quarterly",
            "file1": (io.BytesIO(b"one"), "a.txt"),
            "file2": (io.BytesIO(b"two"), "b.txt"),
        },
        directory=str(tmp_path),
        fields=["description", "file1", "file2"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]
    assert dict(handoff.fields) == {"description": "quarterly"}
    assert set(handoff.files) == {"file1", "file2"}
    assert handoff["description"] == "quarterly"
    assert handoff.get("missing") is None
    assert "description" in handoff

    first = handoff.file("file1")
    assert first.filename == "a.txt"
    assert first.content_length == 3
    assert _read(first) == b"one"

    second = handoff.file("file2")
    assert second.filename == "b.txt"
    assert second.content_length == 3
    assert _read(second) == b"two"

    # The copied files live inside the private temporary area.
    assert os.path.dirname(first.path) == handoff.directory
    assert os.path.dirname(second.path) == handoff.directory

    handoff.close()


def test_handoff_readable_from_background_thread(app, tmp_path):
    response, holder = _post(
        app,
        {
            "note": "remember this",
            "file1": (io.BytesIO(b"background data"), "report.txt"),
        },
        directory=str(tmp_path),
        fields=["note", "file1"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]

    # The request context has been popped by now. The handoff holds no
    # reference to it, so a background thread can still read the data.
    result: dict = {}

    def background_work() -> None:
        result["note"] = handoff["note"]
        result["data"] = _read(handoff.file("file1"))

    thread = threading.Thread(target=background_work)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result == {"note": "remember this", "data": b"background data"}

    handoff.close()
    assert not os.path.exists(handoff.directory)


def test_total_size_limit_raises_and_cleans_up(app, tmp_path):
    holder: dict = {}

    @app.post("/upload")
    def upload():
        try:
            holder["handoff"] = request.handoff(
                ["file1", "file2"],
                max_total_size=8,
                directory=str(tmp_path),
            )
        except HandoffError:
            # The view handles the failure; no handoff is published.
            pass

        return "ok"

    client = app.test_client()
    response = client.post(
        "/upload",
        data={
            "file1": (io.BytesIO(b"12345"), "a.txt"),
            "file2": (io.BytesIO(b"67890"), "b.txt"),
        },
    )

    assert response.status_code == 200
    assert "handoff" not in holder
    assert os.listdir(tmp_path) == []


def test_per_file_size_limit_raises_and_cleans_up(app, tmp_path):
    holder: dict = {}

    @app.post("/upload")
    def upload():
        try:
            holder["handoff"] = request.handoff(
                ["file1"],
                max_file_size=4,
                directory=str(tmp_path),
            )
        except HandoffError:
            pass

        return "ok"

    client = app.test_client()
    response = client.post(
        "/upload",
        data={"file1": (io.BytesIO(b"12345"), "a.txt")},
    )

    assert response.status_code == 200
    assert "handoff" not in holder
    assert os.listdir(tmp_path) == []


def test_view_can_let_limit_error_propagate(app, tmp_path):
    @app.post("/upload")
    def upload():
        return request.handoff(
            ["file1"],
            max_file_size=4,
            directory=str(tmp_path),
        )

    client = app.test_client()

    with pytest.raises(HandoffError):
        client.post(
            "/upload",
            data={"file1": (io.BytesIO(b"12345"), "a.txt")},
        )

    assert os.listdir(tmp_path) == []


def test_close_is_idempotent(app, tmp_path):
    response, holder = _post(
        app,
        {"file1": (io.BytesIO(b"x"), "a.txt")},
        directory=str(tmp_path),
        fields=["file1"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]
    assert handoff.closed is False
    assert os.path.exists(handoff.directory)

    handoff.close()
    assert handoff.closed is True
    assert not os.path.exists(handoff.directory)

    # Closing again has no effect.
    handoff.close()
    handoff.close()
    assert handoff.closed is True


def test_file_access_after_close_raises(app, tmp_path):
    response, holder = _post(
        app,
        {"file1": (io.BytesIO(b"x"), "a.txt")},
        directory=str(tmp_path),
        fields=["file1"],
    )

    handoff = holder["handoff"]
    handoff.close()

    with pytest.raises(ValueError):
        handoff.file("file1")

    with pytest.raises(ValueError):
        handoff.filelist("file1")


def test_context_manager_closes(app, tmp_path):
    holder: dict = {}

    @app.post("/upload")
    def upload():
        with request.handoff(["file1"], directory=str(tmp_path)) as handoff:
            holder["handoff"] = handoff
            assert _read(handoff.file("file1")) == b"x"

        assert handoff.closed is True
        assert not os.path.exists(handoff.directory)
        return "ok"

    client = app.test_client()
    response = client.post(
        "/upload", data={"file1": (io.BytesIO(b"x"), "a.txt")}
    )

    assert response.status_code == 200


@pytest.mark.parametrize(
    "filename",
    [
        "../../evil.txt",
        "../../../etc/passwd",
        "..\\..\\evil.txt",
        "/etc/passwd",
        "C:\\evil.txt",
        "....//....//evil.txt",
    ],
)
def test_malicious_filename_does_not_escape(app, tmp_path, filename):
    response, holder = _post(
        app,
        {"file1": (io.BytesIO(b"evil"), filename)},
        directory=str(tmp_path),
        fields=["file1"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]
    copied = handoff.file("file1")

    # The original filename is metadata only.
    assert copied.filename == filename
    assert copied.content_length == 4

    # The generated target path stays inside the temporary area and the
    # content is intact.
    assert os.path.dirname(copied.path) == handoff.directory
    assert _read(copied) == b"evil"

    # Nothing was written next to the temporary area.
    assert not os.path.exists(os.path.join(tmp_path, "evil.txt"))
    assert not os.path.exists(os.path.join(tmp_path, "passwd"))

    handoff.close()
    assert not os.path.exists(handoff.directory)


def test_missing_and_unselected_fields_are_absent(app, tmp_path):
    response, holder = _post(
        app,
        {
            "present": "yes",
            "unselected": "no",
            "file1": (io.BytesIO(b"x"), "x.txt"),
        },
        directory=str(tmp_path),
        # "ghost" is selected but not present; "unselected" and "file1" are
        # present but not selected.
        fields=["present", "ghost"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]

    assert dict(handoff.fields) == {"present": "yes"}
    assert "ghost" not in handoff
    assert "unselected" not in handoff
    assert handoff.files == {}

    # No files were copied, so the area is empty.
    assert os.listdir(handoff.directory) == []

    handoff.close()


def test_handoff_without_fields_copies_everything(app, tmp_path):
    response, holder = _post(
        app,
        {
            "a": "1",
            "b": "2",
            "file1": (io.BytesIO(b"x"), "x.txt"),
        },
        directory=str(tmp_path),
    )

    assert response.status_code == 200
    handoff = holder["handoff"]

    assert dict(handoff.fields) == {"a": "1", "b": "2"}
    assert set(handoff.files) == {"file1"}

    handoff.close()


def test_multiple_files_under_one_field(app, tmp_path):
    from werkzeug.datastructures import MultiDict

    response, holder = _post(
        app,
        data=MultiDict(
            [
                ("files", (io.BytesIO(b"one"), "a.txt")),
                ("files", (io.BytesIO(b"two"), "b.txt")),
            ]
        ),
        directory=str(tmp_path),
        fields=["files"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]

    all_files = handoff.filelist("files")
    assert [f.filename for f in all_files] == ["a.txt", "b.txt"]
    assert [_read(f) for f in all_files] == [b"one", b"two"]

    # The primary mapping exposes the first file.
    assert handoff.file("files") is all_files[0]
    assert handoff.files["files"] is all_files[0]

    handoff.close()


def test_copy_interruption_cleans_up(app, tmp_path):
    class FailingStream:
        """A stream that fails on read, delegating seek and close."""

        def __init__(self, stream):
            self._stream = stream

        def seek(self, *args):
            return self._stream.seek(*args)

        def read(self, size=-1):
            raise OSError("copy interrupted")

        def close(self):
            return self._stream.close()

    holder: dict = {}

    @app.post("/upload")
    def upload():
        storage = request.files["file1"]
        # Simulate a read failure while copying, after parsing succeeded.
        storage.stream = FailingStream(storage.stream)

        with pytest.raises(HandoffError):
            request.handoff(["file1"], directory=str(tmp_path))

        return "ok"

    client = app.test_client()
    response = client.post(
        "/upload", data={"file1": (io.BytesIO(b"x" * 100), "file.txt")}
    )

    assert response.status_code == 200
    # The partial copy was removed and no handoff was published.
    assert "handoff" not in holder
    assert os.listdir(tmp_path) == []


def test_handoff_uses_system_temp_by_default(app):
    response, holder = _post(
        app,
        {"file1": (io.BytesIO(b"x"), "a.txt")},
        fields=["file1"],
    )

    assert response.status_code == 200
    handoff = holder["handoff"]
    assert handoff.directory.startswith(tempfile.gettempdir())

    handoff.close()
    assert not os.path.exists(handoff.directory)


def test_invalid_limits_raise_value_error(app, tmp_path):
    @app.post("/upload")
    def upload():
        with pytest.raises(ValueError):
            request.handoff(
                ["file1"], max_file_size=-1, directory=str(tmp_path)
            )

        with pytest.raises(ValueError):
            request.handoff(
                ["file1"], max_total_size=-1, directory=str(tmp_path)
            )

        return "ok"

    client = app.test_client()
    response = client.post(
        "/upload", data={"file1": (io.BytesIO(b"x"), "a.txt")}
    )

    assert response.status_code == 200


def test_handoff_does_not_expose_request(app, tmp_path):
    response, holder = _post(
        app,
        {"file1": (io.BytesIO(b"x"), "a.txt")},
        directory=str(tmp_path),
        fields=["file1"],
    )

    handoff = holder["handoff"]

    # The handoff holds no request or file handle, only copied data and
    # metadata.
    assert not hasattr(handoff, "request")
    assert not hasattr(handoff, "stream")
    assert handoff.file("file1").path.endswith(
        os.path.basename(handoff.file("file1").path)
    )

    handoff.close()
