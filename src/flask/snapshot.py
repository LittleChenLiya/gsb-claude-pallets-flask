from __future__ import annotations

import typing as t
from contextvars import ContextVar
from contextvars import Token
from types import MappingProxyType

from werkzeug.local import LocalProxy

from .globals import request as _request

if t.TYPE_CHECKING:  # pragma: no cover
    from .wrappers import Request


class RequestSnapshot:
    """A read-only snapshot of selected data from a request.

    A snapshot captures a small, explicit set of request information so that
    background work can inspect it after the request has ended, without
    keeping the request context alive:

    -   Only the header names and query argument keys the caller lists in
        :func:`snapshot_request` are captured. Repeated values keep the order
        they appeared in the request.
    -   The route identity (method, path, endpoint, blueprint, and view
        arguments) is copied from the matched request.
    -   The body is never read, and lazy form parsing is never triggered.
    -   The snapshot holds no reference to the session, the application, or
        the original :class:`~flask.Request` object.

    A snapshot is immutable. All accessors return immutable values, so reading
    from a snapshot can never modify it.

    Use :meth:`bind` (or :func:`bind_snapshot`) to temporarily make a snapshot
    available through the :data:`current_snapshot` proxy in another context,
    such as a background task. This never replaces the :data:`flask.request`
    proxy.

    .. versionadded:: 3.2
    """

    __slots__ = (
        "_method",
        "_path",
        "_endpoint",
        "_blueprint",
        "_view_args",
        "_headers",
        "_query",
    )

    def __init__(
        self,
        *,
        method: str,
        path: str,
        endpoint: str | None,
        blueprint: str | None,
        view_args: dict[str, t.Any] | None,
        headers: list[tuple[str, str]],
        query: list[tuple[str, str]],
    ) -> None:
        object.__setattr__(self, "_method", method)
        object.__setattr__(self, "_path", path)
        object.__setattr__(self, "_endpoint", endpoint)
        object.__setattr__(self, "_blueprint", blueprint)
        object.__setattr__(
            self, "_view_args", MappingProxyType(dict(view_args) if view_args else {})
        )
        object.__setattr__(self, "_headers", tuple(headers))
        object.__setattr__(self, "_query", tuple(query))

    def __setattr__(self, name: str, value: t.Any) -> None:
        raise AttributeError("RequestSnapshot is read-only.")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("RequestSnapshot is read-only.")

    @property
    def method(self) -> str:
        """The HTTP method of the captured request."""
        return self._method

    @property
    def path(self) -> str:
        """The path of the captured request."""
        return self._path

    @property
    def endpoint(self) -> str | None:
        """The endpoint that matched the captured request, if any."""
        return self._endpoint

    @property
    def blueprint(self) -> str | None:
        """The registered name of the blueprint for the endpoint, if any."""
        return self._blueprint

    @property
    def view_args(self) -> t.Mapping[str, t.Any]:
        """A read-only mapping of the view arguments that matched the
        captured request.
        """
        return self._view_args

    @property
    def headers(self) -> tuple[tuple[str, str], ...]:
        """The captured ``(name, value)`` header pairs, in the order they
        appeared in the request. Only allowed names are present.
        """
        return self._headers

    @property
    def query(self) -> tuple[tuple[str, str], ...]:
        """The captured ``(key, value)`` query argument pairs, in the order
        they appeared in the query string. Only allowed keys are present.
        """
        return self._query

    def get_header(self, name: str, default: str | None = None) -> str | None:
        """Return the first captured value for the header ``name``
        (case-insensitive), or ``default`` if it was not captured.
        """
        lowered = name.lower()

        for key, value in self.headers:
            if key.lower() == lowered:
                return value

        return default

    def get_headers(self, name: str) -> tuple[str, ...]:
        """Return all captured values for the header ``name``
        (case-insensitive), in request order.
        """
        lowered = name.lower()
        return tuple(value for key, value in self.headers if key.lower() == lowered)

    def get_query(self, key: str, default: str | None = None) -> str | None:
        """Return the first captured value for the query argument ``key``,
        or ``default`` if it was not captured.
        """
        for pair_key, value in self.query:
            if pair_key == key:
                return value

        return default

    def get_query_all(self, key: str) -> tuple[str, ...]:
        """Return all captured values for the query argument ``key``, in the
        order they appeared in the query string.
        """
        return tuple(value for pair_key, value in self.query if pair_key == key)

    def bind(self) -> SnapshotBinding:
        """Return a context manager that binds this snapshot to
        :data:`current_snapshot` for the current context.

        Works as both a regular and an async ``with`` block. Bindings can be
        nested; exiting a block, normally or through an exception, restores
        the previously bound snapshot. Because the binding uses a
        :class:`~contextvars.ContextVar`, concurrent async tasks each see
        their own binding.
        """
        return SnapshotBinding(self)

    def __repr__(self) -> str:
        return (
            f"<RequestSnapshot {self.method} {self.path}"
            f" endpoint={self.endpoint!r}"
            f" headers={self.headers!r} query={self.query!r}>"
        )


def snapshot_request(
    *,
    headers: t.Iterable[str] = (),
    query: t.Iterable[str] = (),
    request: Request | None = None,
) -> RequestSnapshot:
    """Capture a read-only :class:`RequestSnapshot` of the current request.

    Only the given header names and query argument keys are copied into the
    snapshot. Everything else, including the body, form data, files, cookies,
    and session, is left out. The body stream is never read and lazy form
    parsing is never triggered.

    :param headers: Header names to capture. Matching is case-insensitive.
        Repeated headers keep the order they appeared in the request.
    :param query: Query argument keys to capture. Repeated keys keep the
        order they appeared in the query string.
    :param request: The request to capture. Defaults to the current
        :data:`flask.request`.

    .. versionadded:: 3.2
    """
    req = request if request is not None else _request._get_current_object()

    seen: set[str] = set()
    header_pairs: list[tuple[str, str]] = []

    for name in headers:
        lowered = name.lower()

        if lowered in seen:
            continue

        seen.add(lowered)

        for value in req.headers.getlist(name):
            header_pairs.append((name, value))

    allowed_query = set(query)
    query_pairs = [
        (key, value)
        for key, value in req.args.items(multi=True)
        if key in allowed_query
    ]

    return RequestSnapshot(
        method=req.method,
        path=req.path,
        endpoint=req.endpoint,
        blueprint=req.blueprint,
        view_args=dict(req.view_args) if req.view_args else None,
        headers=header_pairs,
        query=query_pairs,
    )


_no_snapshot_msg = """\
No request snapshot is bound.

Use 'with snapshot.bind():' or 'bind_snapshot(snapshot)' to temporarily bind a
snapshot to the current context.\
"""

_cv_snapshot: ContextVar[RequestSnapshot] = ContextVar("flask.snapshot_ctx")

current_snapshot: RequestSnapshot = LocalProxy(  # type: ignore[assignment]
    _cv_snapshot, unbound_message=_no_snapshot_msg
)
"""A proxy to the :class:`RequestSnapshot` bound to the current context.

This is only available while a snapshot is bound with
:meth:`RequestSnapshot.bind` or :func:`bind_snapshot`. It is independent of
the :data:`flask.request` proxy, which it never replaces.
"""


class SnapshotBinding:
    """Binds a :class:`RequestSnapshot` to :data:`current_snapshot` for the
    duration of a ``with`` or ``async with`` block.

    Created by :meth:`RequestSnapshot.bind` and :func:`bind_snapshot`. On
    exit, including when an exception is raised, the previously bound
    snapshot (if any) is restored.
    """

    def __init__(self, snapshot: RequestSnapshot) -> None:
        self._snapshot = snapshot
        self._token: Token[RequestSnapshot] | None = None

    def __enter__(self) -> RequestSnapshot:
        self._token = _cv_snapshot.set(self._snapshot)
        return self._snapshot

    def __exit__(self, exc_type: t.Any, exc_value: t.Any, tb: t.Any) -> None:
        if self._token is not None:
            _cv_snapshot.reset(self._token)
            self._token = None

    async def __aenter__(self) -> RequestSnapshot:
        return self.__enter__()

    async def __aexit__(self, exc_type: t.Any, exc_value: t.Any, tb: t.Any) -> None:
        self.__exit__(exc_type, exc_value, tb)


def bind_snapshot(snapshot: RequestSnapshot) -> SnapshotBinding:
    """Return a context manager that temporarily binds ``snapshot`` to
    :data:`current_snapshot`.

    Equivalent to :meth:`RequestSnapshot.bind`. Bindings can be nested and
    are isolated between async tasks.

    .. versionadded:: 3.2
    """
    return SnapshotBinding(snapshot)
