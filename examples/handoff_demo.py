"""Short local demo: hand off uploaded files to a background thread.

Run with::

    python examples/handoff_demo.py

The view copies the selected form field and uploaded file into a private
temporary area, then starts a background thread. After the request context is
popped, the thread still reads the original content. The handoff is closed
when the work is done.
"""

from __future__ import annotations

import io
import threading

from flask import Flask
from flask import request

app = Flask(__name__)

# The background thread stores its result here.
result: dict = {}
_done = threading.Event()


@app.post("/upload")
def upload():
    handoff = request.handoff(
        ["description", "report"],
        max_file_size=1024 * 1024,
        max_total_size=10 * 1024 * 1024,
    )

    def background_work():
        result["description"] = handoff["description"]
        with handoff.file("report").open() as f:
            result["report"] = f.read()
        handoff.close()
        _done.set()

    threading.Thread(target=background_work).start()
    return "accepted"


client = app.test_client()
response = client.post(
    "/upload",
    data={
        "description": "quarterly results",
        "report": (io.BytesIO(b"report contents"), "report.txt"),
    },
)

print("response:", response.status_code, response.get_data(as_text=True))

# Wait for the background thread to finish, then print what it read after
# the request context was gone.
_done.wait(timeout=5)
print("background read:", result)
