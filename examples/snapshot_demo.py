"""Demo: capture a read-only request snapshot and use it from a background
task after the request has ended.

Run from the repository root:

    PYTHONPATH=src python examples/snapshot_demo.py

Only the caller-allowed header and query fields are captured. The body is
never read, and the snapshot holds no reference to the request, session, or
app. ``flask.request`` is never replaced; the snapshot is bound through its
own ``current_snapshot`` proxy.
"""

import asyncio
import io

from flask import current_snapshot
from flask import Flask
from flask import snapshot_request

ALLOWED_HEADERS = ["X-Request-Id", "Accept"]
ALLOWED_QUERY = ["tag", "page"]

app = Flask(__name__)
captured = []


class CountingStream(io.BytesIO):
    """Body stream that proves the snapshot never reads the body."""

    def __init__(self, data):
        super().__init__(data)
        self.reads = 0

    def read(self, *args):
        self.reads += 1
        return super().read(*args)


body_stream = CountingStream(b'{"secret": "never read"}')


@app.route("/items/<int:item_id>", methods=["POST"])
def item(item_id):
    captured.append(snapshot_request(headers=ALLOWED_HEADERS, query=ALLOWED_QUERY))
    return "captured\n"


async def background_task(snap):
    # The request is over. Bind the snapshot in this task only.
    async with snap.bind():
        print("background task sees only the allowed fields:")
        print(f"  method:    {current_snapshot.method}")
        print(f"  path:      {current_snapshot.path}")
        print(f"  endpoint:  {current_snapshot.endpoint}")
        print(f"  view_args: {dict(current_snapshot.view_args)}")
        print(f"  headers:   {current_snapshot.headers}")
        print(f"  query:     {current_snapshot.query}")
        print(f"  tag list:  {current_snapshot.get_query_all('tag')}")


def main():
    client = app.test_client()
    response = client.post(
        "/items/3?tag=a&tag=b&page=1&secret=no",
        input_stream=body_stream,
        content_type="application/json",
        headers={
            "X-Request-Id": "req-42",
            "Accept": "application/json",
            "X-Internal": "hidden",
        },
    )
    print(f"view response: {response.status} {response.text!r}")
    print(f"body stream reads during capture: {body_stream.reads}")

    asyncio.run(background_task(captured[-1]))


if __name__ == "__main__":
    main()
