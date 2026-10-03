"""Demo: read-only request snapshots for background tasks.

This demo shows how to capture a small, read-only snapshot of request data and
bind it for use in a background task. The snapshot only contains the header
and query fields you allow, plus the matched route identity. It never reads the
request body and holds no references to the session, app, or original request.

Run it with:

    python snapshot_demo.py

Then visit:

    http://localhost:5000/report?page=1&page=2&filter=active

The background task will print the allowed fields from the snapshot.
"""

from __future__ import annotations

import threading

from flask import Flask
from flask import bind_snapshot
from flask import copy_current_request_context
from flask import snapshot
from flask import snapshot_request

app = Flask(__name__)


def print_allowed_fields() -> None:
    """Print the allowed fields from the bound snapshot."""
    print("--- Snapshot fields ---")
    print(f"  endpoint: {snapshot.endpoint}")
    print(f"  view_args: {snapshot.view_args}")
    print("  headers:")
    for key in snapshot.headers:
        print(f"    {key}: {snapshot.headers.getlist(key)}")
    print("  args:")
    for key in snapshot.args:
        print(f"    {key}: {snapshot.args.getlist(key)}")
    print("-----------------------")


@app.route("/report")
def report():
    # Capture only the allowed header and query fields.
    snap = snapshot_request(
        headers=["X-Request-Id", "X-Custom"],
        args=["page", "filter"],
    )

    @copy_current_request_context
    def background_task() -> None:
        # Bind the snapshot so the background task can read the allowed fields.
        with bind_snapshot(snap):
            print_allowed_fields()

    threading.Thread(target=background_task).start()
    return "Background task started. Check the console for snapshot fields."


if __name__ == "__main__":
    app.run(debug=True)
