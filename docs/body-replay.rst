.. _body-replay:

Request Body Replay
===================

.. currentmodule:: flask

Some deployments need to bound how large a request body can be, keep small
bodies in memory, and fall back to a temporary file on disk for larger ones,
all without consuming the body more than once from the WSGI server.
:class:`RequestBodyReplayMiddleware` is a composable WSGI middleware that
sits between the application factory and the WSGI entry point and provides
exactly that.

The middleware reads ``wsgi.input`` exactly once, spools it to memory up to a
configurable threshold, and rolls over to a temporary file on disk beyond
it. The buffered stream replaces ``wsgi.input``, so downstream consumers such
as ``request.get_data()``, ``request.json``, and ``request.form`` all see
consistent content, even when the body is accessed repeatedly.

Configuration
-------------

Wrap ``app.wsgi_app`` in the application factory:

.. code-block:: python

    from flask import Flask, RequestBodyReplayMiddleware

    def create_app():
        app = Flask(__name__)
        app.wsgi_app = RequestBodyReplayMiddleware(
            app.wsgi_app,
            max_bytes=1024 * 1024,  # default limit for every route
            memory_threshold=512 * 1024,  # spill to disk past this
            routes={
                "/upload": {"max_bytes": 10 * 1024 * 1024},
                "/api/small": 4096,  # int shorthand for max_bytes
                "/internal": {"max_bytes": None},  # no limit
            },
        )
        return app

-   ``max_bytes`` is the default body size limit in bytes. ``None`` means no
    limit.
-   ``memory_threshold`` is the number of bytes kept in memory before the
    body is spooled to a temporary file on disk.
-   ``routes`` maps URL paths to per-route overrides. An exact ``PATH_INFO``
    match wins; otherwise the longest matching path prefix is used. A value
    may be an int (``max_bytes``) or a dict with ``max_bytes`` and
    ``memory_threshold`` keys.

Limits and Errors
-----------------

-   If the body grows past the limit while it is being read,
    :exc:`~werkzeug.exceptions.RequestEntityTooLarge` (413) is raised
    immediately and is delivered through Flask's normal exception handling,
    so ``@app.errorhandler(413)`` handlers and logging behave as usual.
-   If the client disconnects while the body is being read, the temporary
    file and input stream are closed and
    :exc:`~werkzeug.exceptions.ClientDisconnected` (400) is raised instead.
-   Temporary files and buffered streams are released when the response
    finishes, when the limit is exceeded, or when the client disconnects.
-   Each request gets its own buffer. No state is shared between concurrent
    requests, so the middleware is safe to use with threaded or async
    servers.

Verification
------------

The behavior is covered by unit and integration tests, including empty
bodies, chunked bodies, limit violations, client disconnects, concurrent
requests, and repeated reads:

.. code-block:: text

    $ pytest tests/test_body_replay.py
