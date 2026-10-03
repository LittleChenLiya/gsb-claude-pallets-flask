Request Snapshots
=================

A request snapshot is a read-only copy of request data that is independent of
the active request context. It captures only the header and query fields that
the caller allows, along with the matched route identity (endpoint and view
args). It never reads the request body and holds no references to the session,
app, or original request.

Use a snapshot when a background task needs a small amount of request
information without keeping the full request context alive. Because the
snapshot is a plain copy, it remains readable after the request ends, and it
can be safely passed to threads, async tasks, or other workers.


Creating a Snapshot
-------------------

Call :func:`.snapshot_request` within a request context. Pass the names of the
header and query fields you want to capture. Only those fields are copied; all
others are omitted.

.. code-block:: python

    from flask import snapshot_request

    @app.route("/")
    def index():
        snap = snapshot_request(
            headers=["X-Custom", "X-Request-Id"],
            args=["page", "filter"],
        )
        return "Done"

Header matching is case-insensitive. Query matching is case-sensitive. If a
field is not present in the request, it is simply omitted from the snapshot.

The snapshot captures:

-   ``headers``: the allowed request headers, as a read-only multi-value
    mapping.
-   ``args``: the allowed query arguments, as a read-only multi-value mapping.
    Duplicate values preserve input order.
-   ``endpoint``: the endpoint that matched the request URL, or ``None``.
-   ``view_args``: a copy of the view arguments that matched the request.

The request body is never read. Accessing ``form``, ``json``, ``data``, or
other body-based attributes is not triggered. The snapshot holds no session,
app, or request references.


Binding a Snapshot
------------------

A snapshot is not tied to the request context. To make it available to
background tasks, bind it with :func:`.bind_snapshot`. Within the ``with``
block, the :data:`.snapshot` proxy points to the bound snapshot.

.. code-block:: python

    from flask import bind_snapshot, snapshot

    with bind_snapshot(snap):
        print(snapshot.headers["X-Custom"])
        print(snapshot.args.getlist("page"))
        print(snapshot.endpoint)

Binding supports nesting. The previous snapshot is restored when the block
exits, even if an exception is raised.

.. code-block:: python

    with bind_snapshot(outer):
        assert snapshot.headers["X-Custom"] == "outer"
        try:
            with bind_snapshot(inner):
                assert snapshot.headers["X-Custom"] == "inner"
                raise ValueError("boom")
        except ValueError:
            pass
        # outer is restored
        assert snapshot.headers["X-Custom"] == "outer"

Binding uses :mod:`contextvars`, so async tasks started within a binding see
the bound snapshot without affecting each other. Two async tasks that bind
different snapshots will not cross-contaminate.

.. code-block:: python

    import asyncio
    from flask import bind_snapshot, snapshot

    async def task(snap):
        with bind_snapshot(snap):
            await asyncio.sleep(0)
            return snapshot.headers["X-Custom"]

    async def main():
        return await asyncio.gather(task(snap1), task(snap2))


Read-Only Guarantees
--------------------

The snapshot is read-only. Reading from it cannot modify the snapshot:

-   ``headers`` and ``args`` are immutable mappings. Assigning to or deleting
    items raises an error.
-   ``view_args`` returns a copy. Modifying the returned dict does not affect
    the snapshot.
-   The original :data:`.request` proxy is not replaced. :data:`.snapshot` is
    a separate proxy.


After the Request Ends
----------------------

Because the snapshot is a copy and holds no request reference, it remains
readable after the request context has ended. You can create a snapshot in a
view, then read it later in a background task or after the response has been
sent.

.. code-block:: python

    from flask import snapshot_request

    @app.route("/")
    def index():
        snap = snapshot_request(headers=["X-Custom"], args=["page"])
        # snap is independent of the request context
        return "Done"

    # Later, outside the request context:
    print(snap.headers["X-Custom"])
    print(snap.args.getlist("page"))
    print(snap.endpoint)
