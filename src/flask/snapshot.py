"""Read-only request snapshots for background tasks.

A :class:`RequestSnapshot` captures only the header and query fields that the
caller allows, along with the matched route identity (endpoint and view args).
It is independent of the active request context, holds no references to the
session, app, or original request, and never reads the request body.

Use :func:`snapshot_request` to create a snapshot, and :func:`bind_snapshot`
to make it available to background tasks through the :data:`snapshot` proxy.
"""

from __future__ import annotations

import contextvars
import typing as t
from collections.abc import Iterable
from collections.abc import Mapping
from contextlib import contextmanager

from werkzeug.local import LocalProxy

if t.TYPE_CHECKING:  # pragma: no cover
    from .wrappers import Request

    T = t.TypeVar("T", covariant=True)

    class SnapshotProxy(t.Protocol[T]):
        def _get_current_object(self) -> T: ...

    class RequestSnapshotProxy(SnapshotProxy["RequestSnapshot"], "RequestSnapshot"): ...


_no_snapshot_msg = """\
Working outside of a snapshot binding.

Attempted to use functionality that expected a snapshot to be bound. To solve
this, bind a snapshot using 'with bind_snapshot(snapshot)'. See the
documentation on request snapshots for more information.\
"""


class _ImmutableMultiValue(Mapping[str, str]):
    """An immutable, order-preserving multi-value mapping.

    Wraps a tuple of ``(key, value)`` pairs. Supports multiple values per key
    while preserving insertion order. Reading from the mapping cannot modify
    the snapshot.
    """

    __slots__ = ("_items",)

    def __init__(self, items: t.Iterable[tuple[str, str]]) -> None:
        self._items: tuple[tuple[str, str], ...] = tuple(items)

    def __getitem__(self, key: str) -> str:
        for k, v in self._items:
            if k == key:
                return v
        raise KeyError(key)

    def __iter__(self) -> t.Iterator[str]:
        seen: set[str] = set()
        for k, _ in self._items:
            if k not in seen:
                seen.add(k)
                yield k

    def __len__(self) -> int:
        return len({k for k, _ in self._items})

    def getlist(self, key: str) -> list[str]:
        """Return the list of values for the given key, in input order."""
        return [v for k, v in self._items if k == key]

    def items(self) -> t.Iterable[tuple[str, str]]:  # type: ignore[override]
        """Return all ``(key, value)`` pairs, including duplicates, in input
        order.
        """
        return list(self._items)

    def __contains__(self, key: object) -> bool:
        return any(k == key for k, _ in self._items)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._items!r})"


class _ImmutableHeaders(_ImmutableMultiValue):
    """An immutable, order-preserving, case-insensitive multi-value mapping
    for HTTP headers.
    """

    def __getitem__(self, key: str) -> str:
        key_lower = key.lower()
        for k, v in self._items:
            if k.lower() == key_lower:
                return v
        raise KeyError(key)

    def getlist(self, key: str) -> list[str]:
        """Return the list of values for the given header, in input order."""
        key_lower = key.lower()
        return [v for k, v in self._items if k.lower() == key_lower]

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        key_lower = key.lower()
        return any(k.lower() == key_lower for k, _ in self._items)


class RequestSnapshot:
    """A read-only snapshot of request data that is independent of the active
    request context.

    Captures only the allowed header and query fields, plus the matched route
    identity (endpoint and view args). Does not read the request body, and
    holds no references to the session, app, or original request.

    Create with :func:`snapshot_request`, and bind for use in background tasks
    with :func:`bind_snapshot`.

    .. versionadded:: 3.2
    """

    __slots__ = ("_headers", "_args", "_endpoint", "_view_args")

    def __init__(
        self,
        *,
        headers: t.Iterable[tuple[str, str]] = (),
        args: t.Iterable[tuple[str, str]] = (),
        endpoint: str | None = None,
        view_args: dict[str, t.Any] | None = None,
    ) -> None:
        self._headers: _ImmutableHeaders = _ImmutableHeaders(headers)
        self._args: _ImmutableMultiValue = _ImmutableMultiValue(args)
        self._endpoint = endpoint
        self._view_args: dict[str, t.Any] = dict(view_args) if view_args else {}

    @property
    def headers(self) -> _ImmutableHeaders:
        """The allowed request headers, as a read-only multi-value mapping.
        Header names are case-insensitive and values preserve input order.
        """
        return self._headers

    @property
    def args(self) -> _ImmutableMultiValue:
        """The allowed query arguments, as a read-only multi-value mapping.
        Values preserve input order.
        """
        return self._args

    @property
    def endpoint(self) -> str | None:
        """The endpoint that matched the request URL, or ``None`` if matching
        failed or has not been performed.
        """
        return self._endpoint

    @property
    def view_args(self) -> dict[str, t.Any]:
        """A copy of the view arguments that matched the request. Modifying
        the returned dict does not affect the snapshot.
        """
        return dict(self._view_args)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} endpoint={self._endpoint!r}"
            f" headers={len(self._headers)} args={len(self._args)}>"
        )


def snapshot_request(
    request: Request | None = None,
    *,
    headers: Iterable[str] | None = None,
    args: Iterable[str] | None = None,
) -> RequestSnapshot:
    """Create a read-only :class:`RequestSnapshot` from a request.

    Only the header and query fields named in ``headers`` and ``args`` are
    captured, along with the matched route identity. The request body is never
    read, and the snapshot holds no references to the session, app, or original
    request.

    :param request: The request to snapshot. If not given, the current request
        is used.
    :param headers: The names of request headers to capture. Matching is
        case-insensitive. If ``None``, no headers are captured.
    :param args: The names of query arguments to capture. If ``None``, no
        query arguments are captured.

    .. versionadded:: 3.2
    """
    if request is None:
        from .globals import request as _request_proxy

        request = _request_proxy._get_current_object()

    header_items: list[tuple[str, str]] = []
    if headers is not None:
        allowed = {h.lower() for h in headers}
        for key, value in request.headers:
            if key.lower() in allowed:
                header_items.append((key, value))

    arg_items: list[tuple[str, str]] = []
    if args is not None:
        allowed = set(args)
        for key, value in request.args.items(multi=True):
            if key in allowed:
                arg_items.append((key, value))

    endpoint = request.endpoint
    view_args = request.view_args

    return RequestSnapshot(
        headers=header_items,
        args=arg_items,
        endpoint=endpoint,
        view_args=view_args,
    )


_cv_snapshot: contextvars.ContextVar[RequestSnapshot] = contextvars.ContextVar(
    "flask.snapshot"
)


@contextmanager
def bind_snapshot(
    snapshot: RequestSnapshot,
) -> t.Iterator[RequestSnapshot]:
    """Bind a :class:`RequestSnapshot` so that the :data:`snapshot` proxy is
    available within the ``with`` block.

    Supports nesting: the previous snapshot is restored when the block exits,
    even if an exception is raised. Uses :mod:`contextvars`, so async tasks
    started within the block see the bound snapshot without affecting each
    other.

    :param snapshot: The snapshot to bind.

    .. versionadded:: 3.2
    """
    token = _cv_snapshot.set(snapshot)
    try:
        yield snapshot
    finally:
        _cv_snapshot.reset(token)


snapshot: RequestSnapshotProxy = LocalProxy(  # type: ignore[assignment]
    _cv_snapshot, unbound_message=_no_snapshot_msg
)