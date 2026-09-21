from __future__ import annotations

import collections.abc as cabc
import io
import tempfile
import typing as t

from werkzeug.exceptions import ClientDisconnected
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.wsgi import get_content_length

if t.TYPE_CHECKING:  # pragma: no cover
    from _typeshed.wsgi import StartResponse
    from _typeshed.wsgi import WSGIApplication
    from _typeshed.wsgi import WSGIEnvironment


class BodyReplayStream:
    """A read-once wrapper around a WSGI input stream that spools everything
    it reads into a replayable buffer.

    The wrapped stream is consumed at most once. Data that has been read is
    kept in memory up to `memory_threshold` bytes, then rolls over to a
    temporary file on disk. Calling :meth:`seek` with an offset within the
    buffered data replays it without touching the wrapped stream again.

    If more than `max_bytes` bytes are read,
    :class:`~werkzeug.exceptions.RequestEntityTooLarge` is raised
    immediately, during the read. If the wrapped stream ends before
    `content_length` bytes have been read, or raises while being read,
    :class:`~werkzeug.exceptions.ClientDisconnected` is raised instead. In
    either case the temporary file and the wrapped stream are released right
    away.

    Each instance owns its buffer, so concurrent requests never share state.

    :param stream: The WSGI input stream to wrap.
    :param max_bytes: Maximum number of body bytes allowed, or `None` for
        no limit.
    :param memory_threshold: Spool to a temporary file once more than this
        many bytes are buffered.
    :param temp_dir: Directory for temporary files, or `None` for the
        system default.
    :param content_length: Expected body length used to detect a client
        disconnect, or `None` if unknown (for example chunked requests).

    .. versionadded:: 3.2
    """

    def __init__(
        self,
        stream: t.IO[bytes],
        *,
        max_bytes: int | None = None,
        memory_threshold: int = 512 * 1024,
        temp_dir: str | None = None,
        content_length: int | None = None,
    ) -> None:
        self._raw = stream
        self._max_bytes = max_bytes
        self._content_length = content_length
        self._spool = tempfile.SpooledTemporaryFile(
            max_size=memory_threshold, mode="w+b", dir=temp_dir
        )
        self._pos = 0
        self._buffered = 0
        self._eof = False
        self._failed: Exception | None = None
        self._closed = False

    def _check_state(self) -> None:
        if self._failed is not None:
            raise self._failed

        if self._closed:
            raise ValueError("I/O operation on closed stream.")

    def _release(self) -> None:
        if self._closed:
            return

        self._closed = True

        try:
            self._spool.close()
        finally:
            close = getattr(self._raw, "close", None)

            if close is not None:
                close()

    def _fail(self, exc: Exception) -> t.NoReturn:
        # Release the temporary file and wrapped stream immediately, then
        # report the error. The same exception is re-raised by later reads.
        self._failed = exc
        self._release()
        raise exc

    def _fill(self, size: int) -> bytes:
        """Read up to `size` new bytes from the wrapped stream, spool them,
        and return them.
        """
        if self._eof or size <= 0:
            return b""

        if self._max_bytes is not None:
            # Read at most one byte past the limit so an oversized body is
            # rejected as soon as it is seen, without consuming the rest.
            allowance = self._max_bytes + 1 - self._buffered

            if allowance <= 0:
                self._fail(RequestEntityTooLarge())

            size = min(size, allowance)

        try:
            chunk = self._raw.read(size)
        except Exception:
            self._fail(ClientDisconnected())

        if not chunk:
            self._eof = True

            if (
                self._content_length is not None
                and self._buffered < self._content_length
            ):
                self._fail(ClientDisconnected())

            return b""

        self._spool.seek(self._buffered)
        self._spool.write(chunk)
        self._buffered += len(chunk)
        self._pos += len(chunk)

        if self._max_bytes is not None and self._buffered > self._max_bytes:
            self._fail(RequestEntityTooLarge())

        return chunk

    def _read_buffered(self, size: int) -> bytes:
        """Serve up to `size` bytes (all remaining if negative) from the
        spooled buffer.
        """
        available = self._buffered - self._pos

        if available <= 0:
            return b""

        if size < 0 or size > available:
            size = available

        self._spool.seek(self._pos)
        data = self._spool.read(size)
        self._pos += len(data)
        return data

    def read(self, size: int = -1) -> bytes:
        self._check_state()

        if size is None or size < 0:
            parts = [self._read_buffered(-1)]

            while True:
                chunk = self._fill(65536)

                if not chunk:
                    break

                parts.append(chunk)

            return b"".join(parts)

        parts = [self._read_buffered(size)]
        remaining = size - len(parts[0])

        while remaining > 0:
            chunk = self._fill(remaining)

            if not chunk:
                break

            parts.append(chunk)
            remaining -= len(chunk)

        return b"".join(parts)

    def readline(self, size: int = -1) -> bytes:
        self._check_state()

        if size == 0:
            return b""

        limit = size if size is not None and size > 0 else None
        out = bytearray()

        while limit is None or len(out) < limit:
            step = 8192 if limit is None else min(8192, limit - len(out))
            start = self._pos
            chunk = self.read(step)

            if not chunk:
                break

            index = chunk.find(b"\n")

            if index >= 0:
                keep = index + 1
                out += chunk[:keep]
                # Rewind past the unread part of the chunk so it can be
                # replayed by the next read.
                self.seek(start + keep)
                break

            out += chunk

        return bytes(out)

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines = []
        total = 0

        while True:
            line = self.readline()

            if not line:
                break

            lines.append(line)
            total += len(line)

            if hint is not None and hint > 0 and total >= hint:
                break

        return lines

    def __iter__(self) -> BodyReplayStream:
        return self

    def __next__(self) -> bytes:
        line = self.readline()

        if not line:
            raise StopIteration

        return line

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        """Reposition within the buffered data to replay it. Seeking past
        what has been read so far is not possible.
        """
        self._check_state()

        if whence == io.SEEK_SET:
            pos = offset
        elif whence == io.SEEK_CUR:
            pos = self._pos + offset
        elif whence == io.SEEK_END:
            if not self._eof:
                raise OSError("cannot seek from the end before the stream is exhausted")

            pos = self._buffered + offset
        else:
            raise ValueError(f"invalid whence: {whence}")

        if not 0 <= pos <= self._buffered:
            raise OSError("cannot seek outside the buffered request body")

        self._pos = pos
        return pos

    def tell(self) -> int:
        self._check_state()
        return self._pos

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Release the temporary file and the wrapped stream."""
        self._release()


class BodyReplayMiddleware:
    """WSGI middleware that replaces `wsgi.input` with a
    :class:`BodyReplayStream` for each request.

    Apply it between the application factory and the WSGI entry point::

        app = create_app()
        app.wsgi_app = BodyReplayMiddleware(
            app.wsgi_app,
            max_bytes=1024 * 1024,
            route_limits={"/uploads": 100 * 1024 * 1024},
        )

    The raw client stream is consumed at most once per request, while
    downstream `request.get_data()`, `request.get_json()`, and
    `request.form` keep their usual caching behavior and see consistent
    content. Bodies larger than the per-route or default limit fail with a
    413 error raised while the body is being read, so it is processed by
    Flask's regular exception handling and logging. Every request gets its
    own buffer; nothing is shared between concurrent requests. The buffer,
    including any temporary file, is released when the request finishes or
    the client disconnects.

    :param app: The WSGI application to wrap, usually `app.wsgi_app`.
    :param max_bytes: Default maximum request body size in bytes, or
        `None` for no limit.
    :param route_limits: A mapping of path prefixes to maximum body sizes.
        The longest matching prefix wins and overrides `max_bytes` for
        that route. A `None` value disables the limit for that prefix.
    :param memory_threshold: Buffer each body in memory up to this many
        bytes, then fall back to a temporary file on disk.
    :param temp_dir: Directory for temporary files, or `None` for the
        system default.

    .. versionadded:: 3.2
    """

    def __init__(
        self,
        app: WSGIApplication,
        *,
        max_bytes: int | None = None,
        route_limits: dict[str, int | None] | None = None,
        memory_threshold: int = 512 * 1024,
        temp_dir: str | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.memory_threshold = memory_threshold
        self.temp_dir = temp_dir
        self._route_limits = sorted(
            (route_limits or {}).items(), key=lambda item: len(item[0]), reverse=True
        )

    def max_bytes_for(self, path: str) -> int | None:
        """Return the body size limit for the given path. The longest
        matching prefix from `route_limits` wins, otherwise the default
        `max_bytes` is used.
        """
        for prefix, limit in self._route_limits:
            if path.startswith(prefix):
                return limit

        return self.max_bytes

    def __call__(
        self, environ: WSGIEnvironment, start_response: StartResponse
    ) -> cabc.Iterable[bytes]:
        stream = environ.get("wsgi.input")

        if stream is None:
            return self.app(environ, start_response)

        replay = BodyReplayStream(
            stream,
            max_bytes=self.max_bytes_for(environ.get("PATH_INFO", "")),
            memory_threshold=self.memory_threshold,
            temp_dir=self.temp_dir,
            content_length=get_content_length(environ),
        )
        environ["wsgi.input"] = replay

        try:
            response = self.app(environ, start_response)
        except BaseException:
            replay.close()
            raise

        return _ClosingIterable(response, replay)


class _ClosingIterable:
    """Wrap the WSGI response iterable so the replay stream is released once
    the response has been fully sent or closed.
    """

    __slots__ = ("_iterable", "_stream")

    def __init__(
        self, iterable: cabc.Iterable[bytes], stream: BodyReplayStream
    ) -> None:
        self._iterable = iterable
        self._stream = stream

    def __iter__(self) -> cabc.Iterator[bytes]:
        try:
            yield from self._iterable
        finally:
            self._stream.close()

    def close(self) -> None:
        try:
            close = getattr(self._iterable, "close", None)

            if close is not None:
                close()
        finally:
            self._stream.close()
