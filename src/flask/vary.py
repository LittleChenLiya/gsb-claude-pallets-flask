from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:  # pragma: no cover
    from werkzeug.wrappers import Response


class Vary:
    """The response negotiation recorder.

    During a request, record the names of the request headers that are
    used to select a response representation (content negotiation). At the
    end of the request, the recorded header names are merged into the
    response's ``Vary`` header. Only header names are recorded, never their
    values.

    The recorder is available as :data:`flask.vary` and is shared by all
    callbacks handling the current request, including nested blueprints.
    It is only merged into the response if at least one header was
    recorded, so responses that do not use negotiation are left unchanged.

    .. code-block:: python

        from flask import Flask, request, vary

        app = Flask(__name__)

        @app.route("/")
        def index():
            language = request.accept_languages.best_match(["en", "zh"])
            vary.record("Accept-Language")
            ...

    See :doc:`/patterns/vary` for more information.

    .. versionadded:: 3.2
    """

    def __init__(self) -> None:
        # Maps the lowercased header name to the casing used when it was
        # first recorded. Header names are matched case-insensitively, but
        # the original casing is preserved for the Vary header.
        self._headers: dict[str, str] = {}

    def record(self, header: str) -> None:
        """Record that *header* was used to negotiate the response.

        Only the header name is recorded, not its value. Header names are
        matched case-insensitively, so recording ``accept`` and ``Accept``
        only adds one entry to ``Vary``. The casing of the first recording
        is kept.
        """
        self._headers.setdefault(header.lower(), header)

    def update(self, headers: t.Iterable[str]) -> None:
        """Record that each header in *headers* was used to negotiate the
        response.
        """
        for header in headers:
            self.record(header)

    def clear(self) -> None:
        """Remove all recorded header names."""
        self._headers.clear()

    @property
    def headers(self) -> frozenset[str]:
        """The recorded header names, lowercased."""
        return frozenset(self._headers)

    def apply(self, response: Response) -> None:
        """Merge the recorded headers into *response*'s ``Vary`` header.

        This is called automatically at the end of each request. It can
        also be called manually to merge early.

        - If ``Vary`` already contains ``*``, it is left unchanged.
        - If a recorded header is ``*``, ``Vary`` is set to ``*``.
        - Otherwise recorded headers are appended, matched
          case-insensitively, preserving the existing order.
        """
        if not self._headers:
            return

        vary = response.vary

        if "*" in vary:
            # A wildcard Vary already matches every request header. Keep
            # it and do not add anything.
            return

        if "*" in self._headers:
            vary.clear()
            vary.add("*")
            return

        vary.update(self._headers.values())
