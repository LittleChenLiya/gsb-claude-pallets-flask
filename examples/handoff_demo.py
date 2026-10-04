"""Short local demo: stage an upload for a background thread.

Run it without a server (uses the test client)::

    python examples/handoff_demo.py

The view calls :func:`flask.stage_handoff`, which copies the chosen
form values and uploaded files into a private temporary directory and
returns a :class:`~flask.RequestHandoff`.  The handoff is plain data
plus file paths: it has no link to the request, its context, or any
open upload handle, so the background thread can read it after the
response has been sent and close it when finished.
"""

from __future__ import annotations

import io
import threading
import time

from flask import Flask
from flask import RequestHandoff
from flask import stage_handoff

app = Flask(__name__)
_workers: list[threading.Thread] = []


def background_job(handoff: RequestHandoff) -> None:
    """Runs after the request context is gone."""
    print(f"[worker] form title = {handoff.form['title']!r}")
    for staged in handoff.files:
        # Original client filename is metadata; path is server controlled.
        print(
            f"[worker] field={staged.field!r} filename={staged.filename!r}"
            f" size={staged.size}"
        )
        print(f"[worker] contents = {staged.read_bytes()!r}")
    handoff.close()  # deletes the staging directory; safe to call twice
    handoff.close()
    print(f"[worker] closed={handoff.closed}")


@app.post("/submit")
def submit() -> tuple[str, int]:
    handoff = stage_handoff(
        form_fields=("title",),
        file_fields=("doc", "image"),
        max_file_size=1024,
        max_total_size=4096,
    )
    # Only the handoff crosses the thread boundary -- never the request.
    worker = threading.Thread(target=background_job, args=(handoff,))
    worker.start()
    _workers.append(worker)
    return "accepted", 202


def main() -> None:
    client = app.test_client()
    response = client.post(
        "/submit",
        data={
            "title": "quarterly report",
            "doc": (io.BytesIO(b"plain text body"), "notes.txt"),
            "image": (io.BytesIO(b"\x89PNG-fake"), "../../../evil.png"),
        },
        content_type="multipart/form-data",
    )
    print(f"[server] status = {response.status_code}")
    # Give the detached worker a moment; the request is already gone.
    time.sleep(0.2)
    for worker in _workers:
        worker.join()


if __name__ == "__main__":
    main()
