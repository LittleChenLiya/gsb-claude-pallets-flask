Content negotiation and the ``Vary`` header
============================================

When a request can produce different representations of the same
resource, the response must tell caches *which request headers were used
to select the representation*. It does so with the ``Vary`` header. For
example, a response selected based on the ``Accept-Language`` header must
be sent with ``Vary: Accept-Language``. A cache can then only reuse the
response for requests with a matching ``Accept-Language`` value.

``Vary`` is therefore the **cache identity** of the response: it lists the
request headers that participate in selecting the representation. If a
response is negotiated but no ``Vary`` header is sent, a cache could
serve the wrong representation to the wrong client.

Flask provides an explicit response negotiation recorder, :data:`flask.vary`,
that keeps track of the request headers used during a request and merges
them into the response's ``Vary`` header automatically.

Recording negotiation
---------------------

Call :meth:`~flask.Vary.record` each time you select a representation based
on a request header. Only the header *name* is recorded, never its value.

.. code-block:: python

    from flask import Flask, request, vary

    app = Flask(__name__)

    @app.route("/")
    def index():
        language = request.accept_languages.best_match(["en", "zh"])
        vary.record("Accept-Language")

        mimetype = request.accept_mimetypes.best_match(
            ["application/json", "text/html"]
        )
        vary.record("Accept")

        return render(language, mimetype)

This produces ``Vary: Accept-Language, Accept``.

The recorder is shared by every callback handling the current request,
including nested blueprints. A header recorded in a ``before_request``
function or in a child blueprint is merged into the final response.

Headers are matched case-insensitively. Recording ``accept`` and
``Accept`` only adds one entry to ``Vary``, and the casing of the first
recording is kept. Recorded headers are appended to any headers already
present in the response's ``Vary`` header; the existing order is not
changed.

If the response already sends ``Vary: *``, the wildcard is kept and no
other headers are added. If a recorded header is ``*``, the response is
sent with ``Vary: *``. A response that does not record any negotiation is
left completely unchanged.

Conditional and HEAD requests
-----------------------------

A ``304 Not Modified`` response has no body, but it must still carry the
same ``Vary`` header as the full response, because the cache identity is
unchanged. The recorder merges ``Vary`` after all ``after_request``
functions have run, so a 304 produced by
:meth:`~werkzeug.wrappers.Response.make_conditional` keeps the same
negotiation identity. The same applies to ``HEAD`` requests.

.. code-block:: python

    from flask import Flask, request, vary

    app = Flask(__name__)

    @app.after_request
    def conditional(response):
        return response.make_conditional(request)

    @app.route("/")
    def index():
        response = render()
        response.add_etag()
        vary.record("Accept-Language")
        return response

Running the tests
-----------------

The behavior is covered by pytest tests that drive the Flask test client.
Run them with:

.. code-block:: console

    pytest tests/test_vary.py -v
