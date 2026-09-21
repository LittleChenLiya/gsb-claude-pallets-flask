Request Body Replay
===================

Some deployments need to bound how much memory a request body can use,
spool large bodies to disk, and reject oversized uploads with a
deterministic ``413`` response, without changing how views access
:attr:`~flask.request` data. The :class:`~flask.body_replay.BodyReplayMiddleware`
WSGI middleware provides this between the application factory and the WSGI
entry point.

Configuration
-------------

Apply the middleware to ``app.wsgi_app`` after creating the application::

    from flask.body_replay import BodyReplayMiddleware

    app = create_app()
    app.wsgi_app = BodyReplayMiddleware(
        app.wsgi_app,
        max_bytes=1024 * 1024,
        route_limits={"/uploads": 100 * 1024 * 1024},
        memory_threshold=512 * 1024,
        temp_dir="/var/tmp/myapp",
    )

-   ``max_bytes`` is the default request body limit. ``route_limits`` maps
    path prefixes to per-route limits; the longest matching prefix wins and
    overrides the default. A ``None`` value disables the limit for that
    prefix.
-   Each body is buffered in memory up to ``memory_threshold`` bytes, then
    falls back to a temporary file in ``temp_dir`` (or the system default
    temporary directory).
-   Every request gets its own buffer, so concurrent requests never share
    state. The buffer, including any temporary file, is released when the
    request finishes or the client disconnects.

Behavior and Limits
-------------------

The raw client stream is consumed at most once per request. Downstream
``request.get_data()``, ``request.get_json()``, and ``request.form`` keep
their usual caching behavior and see consistent content, and the wrapped
stream supports :meth:`~flask.body_replay.BodyReplayStream.seek` to replay
what has been buffered.

When a body exceeds its limit,
:class:`~werkzeug.exceptions.RequestEntityTooLarge` (413) is raised while
the body is being read, without consuming the rest of the body. When the
client disconnects mid-body,
:class:`~werkzeug.exceptions.ClientDisconnected` (400) is raised instead.
Both go through Flask's regular exception handling, so registered error
handlers and logging behave exactly as they do for other request errors::

    @app.errorhandler(413)
    def too_large(e):
        return "upload too large", 413

Verification
------------

The middleware is covered by unit and integration tests in
``tests/test_body_replay.py``, including empty bodies, chunked requests,
limit boundaries, client disconnects, temporary file cleanup, repeated
reads, and concurrent requests. Run them with the project's test command::

    pytest tests/test_body_replay.py
