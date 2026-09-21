"""Composable request body replay middleware.

This module provides :class:`RequestBodyReplayMiddleware`, a WSGI
middleware that sits between the application factory and the WSGI
entry point. It reads the request body exactly once, spools it to
memory (falling back to a temporary file on disk past a configurable
threshold), and replaces ``wsgi.input`` with the buffered stream so
that downstream consumers (``request.get_data()``, ``request.json``,
``request.form``) all see consistent content.

Bodies larger than a per-route limit are rejected with a deterministic
413 ``RequestEntityTooLarge`` raised while the body is being read, and
temporary resources are released when the client disconnects or the
response finishes.
"""

from __future__ import annotations

import tempfile
import typing as t
from io import BytesIO

from werkzeug.exceptions import ClientDisconnected
from werkzeug.exceptions import RequestEntityTooLarge

if t.TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse
    from _typeshed.wsgi import WSGIApplication
    from _typeshed.wsgi import WSGIEnvironment

#: Default maximum number of body bytes held in memory before spooling
#: to a temporary file on disk.
DEFAULT_MEMORY_THRESHOLD = 512 * 1024

#: Size of each chunk read from the original ``wsgi.input`` stream.
DEFAULT_CHUNK_SIZE = 64 * 1024


class _ErrorStream:
    """A ``wsgi.input`` replacement that re-raises a stored error.

    Used when buffering the body failed (limit exceeded or client
    disconnect) so that the wrapped Flask application raises the error
    through its normal exception handling and logging when the body is
    consumed.
    """

    def __init__(self, error: Exception) -> None:
        self._error = error

    def read(self, size: int = -1) -> bytes:
        raise self._error

    def readline(self, size: int = -1) -> bytes:
        raise self._error

    def readlines(self, hint: int = -1) -> list[bytes]:
        raise self._error

    def __iter__(self) -> t.Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        raise self._error

    def close(self) -> None:
        pass


class _ClosingIterable:
    """Wrap the application response iterable to close the spooled
    body once the response is fully consumed or closed.
    """

    def __init__(self, iterable: t.Iterable[bytes], spool: t.IO[bytes]) -> None:
        self._iterable = iterable
        self._spool = spool

    def __iter__(self) -> t.Iterator[bytes]:
        try:
            yield from self._iterable
        finally:
            self.close()

    def close(self) -> None:
        try:
            close = getattr(self._iterable, "close", None)
            if close is not None:
                close()
        finally:
            self._spool.close()


def _has_body(environ: WSGIEnvironment) -> bool:
    """Check whether the request is expected to carry a body."""
    if environ.get("wsgi.input_terminated"):
        return True

    transfer_encoding = environ.get("HTTP_TRANSFER_ENCODING", "")
    if "chunked" in transfer_encoding.lower():
        return True

    content_length = environ.get("CONTENT_LENGTH") or ""
    try:
        return int(content_length) > 0
    except ValueError:
        return False


class RequestBodyReplayMiddleware:
    """WSGI middleware that buffers the request body exactly once and
    replays it to the wrapped application.

    :param app: The WSGI application to wrap, typically
        ``app.wsgi_app`` of a Flask application.
    :param max_bytes: Default maximum accepted body size in bytes.
        ``None`` means no limit. Reading past the limit raises
        :exc:`~werkzeug.exceptions.RequestEntityTooLarge` (413) while
        the body is consumed.
    :param memory_threshold: Default number of bytes kept in memory
        before spooling to a temporary file on disk.
    :param routes: Per-route overrides mapping a URL path to either a
        ``max_bytes`` int or a dict with ``max_bytes`` and
        ``memory_threshold`` keys. An exact ``PATH_INFO`` match wins,
        otherwise the longest matching path prefix is used.
    :param chunk_size: Read chunk size used while buffering.

    Each request gets its own buffer; no state is shared between
    concurrent requests. Temporary files and streams are closed when
    the response finishes, when the limit is exceeded, or when the
    client disconnects mid-read.

    .. code-block:: python

        def create_app():
            app = Flask(__name__)
            app.wsgi_app = RequestBodyReplayMiddleware(
                app.wsgi_app,
                max_bytes=1024 * 1024,
                routes={"/upload": {"max_bytes": 10 * 1024 * 1024}},
            )
            return app
    """

    def __init__(
        self,
        app: WSGIApplication,
        max_bytes: int | None = None,
        memory_threshold: int = DEFAULT_MEMORY_THRESHOLD,
        routes: dict[str, int | dict[str, int | None]] | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.memory_threshold = memory_threshold
        self.chunk_size = chunk_size
        self.routes: dict[str, tuple[int | None, int]] = {}

        for path, config in (routes or {}).items():
            if isinstance(config, dict):
                route_max = config.get("max_bytes", max_bytes)
                route_threshold = config.get("memory_threshold", memory_threshold)
                self.routes[path] = (
                    route_max,
                    route_threshold if route_threshold else memory_threshold,
                )
            else:
                self.routes[path] = (config, memory_threshold)

    def _limits_for(self, path: str) -> tuple[int | None, int]:
        """Resolve ``(max_bytes, memory_threshold)`` for a path."""
        if path in self.routes:
            return self.routes[path]

        best: str | None = None
        for route in self.routes:
            if path.startswith(route) and (best is None or len(route) > len(best)):
                best = route

        if best is not None:
            return self.routes[best]

        return self.max_bytes, self.memory_threshold

    def _spool_body(
        self, environ: WSGIEnvironment, max_bytes: int | None, threshold: int
    ) -> t.IO[bytes]:
        """Read ``wsgi.input`` once into a memory/disk spool."""
        stream = environ["wsgi.input"]
        spool: t.IO[bytes] = BytesIO()
        on_disk = False
        total = 0

        try:
            while True:
                chunk = stream.read(self.chunk_size)
                if not chunk:
                    break

                total += len(chunk)

                if max_bytes is not None and total > max_bytes:
                    raise RequestEntityTooLarge(
                        description=(
                            f"The request body exceeded the limit of {max_bytes} bytes."
                        )
                    )

                if not on_disk and total > threshold:
                    tmp = tempfile.TemporaryFile(prefix="flask-body-")
                    tmp.write(spool.getvalue())  # type: ignore[attr-defined]
                    spool.close()
                    spool = tmp
                    on_disk = True

                spool.write(chunk)
        except BaseException:
            spool.close()
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
            raise

        spool.seek(0)
        return spool

    def __call__(
        self, environ: WSGIEnvironment, start_response: StartResponse
    ) -> t.Iterable[bytes]:
        if not _has_body(environ):
            return self.app(environ, start_response)

        max_bytes, threshold = self._limits_for(environ.get("PATH_INFO", ""))

        try:
            spool = self._spool_body(environ, max_bytes, threshold)
        except RequestEntityTooLarge as e:
            environ["wsgi.input"] = _ErrorStream(e)
            return self.app(environ, start_response)
        except (ClientDisconnected, ConnectionError, OSError) as e:
            error = e if isinstance(e, ClientDisconnected) else ClientDisconnected()
            environ["wsgi.input"] = _ErrorStream(error)
            return self.app(environ, start_response)

        environ["wsgi.input"] = spool

        try:
            response = self.app(environ, start_response)
        except BaseException:
            spool.close()
            raise

        return _ClosingIterable(response, spool)
