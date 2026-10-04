"""Explicit handoff of selected form data and uploaded files out of a request.

A view can call :meth:`Request.handoff <flask.Request.handoff>` (or
:func:`handoff_request`) to copy the form fields and uploaded files it needs
into a private temporary directory. The resulting :class:`RequestHandoff`
holds no reference to the request, the request context, or the original file
handles, so it is safe to use from a background thread after the request
context has been popped.

Only the fields named by the caller are copied. Uploaded files are copied
byte-for-byte into the temporary area; the original filename is retained as
metadata only and never influences the target path. Limits on the size of a
single file and on the total size of all copied files are enforced while
copying. If anything fails, the partially copied area is removed and no
handoff object is published.

The handoff owns the temporary area. Call :meth:`~RequestHandoff.close` when
the background work is done to remove it. Closing is idempotent.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import typing as t
from dataclasses import dataclass
from types import MappingProxyType

if t.TYPE_CHECKING:  # pragma: no cover
    from werkzeug.wrappers import Request

__all__ = ["HandoffError", "HandoffFile", "RequestHandoff", "handoff_request"]

#: Default prefix for the private temporary directory created by a handoff.
_DEFAULT_PREFIX = "flask-handoff-"

#: Chunk size used while copying an uploaded file, in bytes.
_COPY_CHUNK_SIZE = 1024 * 1024


class HandoffError(RuntimeError):
    """Raised when a request handoff cannot be completed.

    This is raised if a copied file exceeds the per-file or total size limit,
    or if reading an uploaded file fails while copying. When this is raised,
    no :class:`RequestHandoff` is published and the temporary area is removed.
    """


@dataclass(frozen=True)
class HandoffFile:
    """Metadata and read access to one uploaded file copied out of a request.

    The original filename is retained as metadata only. The file's path on
    disk is a generated name that never incorporates the filename, so a
    malicious filename (for example one containing ``../``) cannot influence
    the filesystem.
    """

    #: The form field name the file was uploaded under.
    name: str

    #: The original filename from the upload, if any. Metadata only.
    filename: str | None

    #: The content type from the upload, if any.
    content_type: str | None

    #: The number of bytes copied to :attr:`path`.
    content_length: int

    #: The absolute path of the copied file in the handoff's temporary area.
    path: str

    def open(self, mode: str = "rb") -> t.IO[t.Any]:
        """Open the copied file. The caller owns the returned file handle."""
        return open(self.path, mode)


class RequestHandoff:
    """A detached copy of selected form fields and uploaded files.

    Create instances with :meth:`Request.handoff <flask.Request.handoff>` or
    :func:`handoff_request`, not directly.

    The handoff owns a private temporary directory. It holds no reference to
    the request, the request context, or the original file handles, so it can
    safely be read from a background thread after the request context has been
    popped.

    Call :meth:`close` when done to remove the temporary files. Closing is
    idempotent. The object also works as a context manager::

        with request.handoff(["description", "report"]) as handoff:
            ...
        # the temporary area is removed here
    """

    def __init__(
        self,
        directory: str,
        fields: dict[str, str],
        files: dict[str, list[HandoffFile]],
    ) -> None:
        self._directory = os.path.abspath(directory)
        self._fields = dict(fields)
        self._files: dict[str, tuple[HandoffFile, ...]] = {
            name: tuple(filelist) for name, filelist in files.items()
        }
        self._closed = False

    @property
    def closed(self) -> bool:
        """``True`` after :meth:`close` has been called."""
        return self._closed

    @property
    def directory(self) -> str:
        """The absolute path of the private temporary directory."""
        return self._directory

    @property
    def fields(self) -> t.Mapping[str, str]:
        """A read-only mapping of the selected form field values."""
        return MappingProxyType(self._fields)

    @property
    def files(self) -> t.Mapping[str, HandoffFile]:
        """A read-only mapping of the first copied file for each field name.

        Use :meth:`filelist` to access every file uploaded under a field name
        (a field may accept multiple files).
        """
        return MappingProxyType(
            {name: filelist[0] for name, filelist in self._files.items()}
        )

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the selected form field value for ``key``, or ``default``."""
        return self._fields.get(key, default)

    def __getitem__(self, key: str) -> str:
        return self._fields[key]

    def __contains__(self, key: object) -> bool:
        return key in self._fields

    def keys(self) -> t.KeysView[str]:
        return self._fields.keys()

    def values(self) -> t.ValuesView[str]:
        return self._fields.values()

    def items(self) -> t.ItemsView[str, str]:
        return self._fields.items()

    def file(self, name: str) -> HandoffFile:
        """Return the first copied file uploaded under the field ``name``.

        :raises KeyError: if no file was copied under that name.
        :raises ValueError: if the handoff has been closed.
        """
        if self._closed:
            raise ValueError(f"{self!r} has been closed")

        return self._files[name][0]

    def filelist(self, name: str) -> tuple[HandoffFile, ...]:
        """Return every copied file uploaded under the field ``name``.

        :raises KeyError: if no file was copied under that name.
        :raises ValueError: if the handoff has been closed.
        """
        if self._closed:
            raise ValueError(f"{self!r} has been closed")

        return self._files[name]

    def close(self) -> None:
        """Remove the temporary directory.

        Safe to call more than once; subsequent calls have no effect.
        """
        if self._closed:
            return

        self._closed = True
        shutil.rmtree(self._directory, ignore_errors=True)

    def __enter__(self) -> RequestHandoff:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: t.Any,
    ) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return (
            f"<{type(self).__name__} {state} fields={sorted(self._fields)!r}"
            f" files={sorted(self._files)!r}>"
        )


def handoff_request(
    request: Request,
    fields: t.Iterable[str] | None = None,
    *,
    max_file_size: int | None = None,
    max_total_size: int | None = None,
    directory: str | None = None,
) -> RequestHandoff:
    """Copy selected form fields and uploaded files out of ``request``.

    This is the functional equivalent of :meth:`Request.handoff
    <flask.Request.handoff>`. See that method for the full description.

    :param request: the current request to copy data out of.
    :param fields: the form field names to copy. Fields not listed are
        ignored. If ``None``, every form field and file is copied.
    :param max_file_size: the maximum size in bytes of a single copied file.
        ``None`` means no limit.
    :param max_total_size: the maximum total size in bytes of all copied
        files. ``None`` means no limit.
    :param directory: a base directory in which to create the private
        temporary area. If ``None``, the system default is used.
    :raises HandoffError: if a size limit is exceeded or copying fails.
    """
    if fields is None:
        names = list(dict.fromkeys([*request.form.keys(), *request.files.keys()]))
    else:
        names = list(fields)

    if max_file_size is not None and max_file_size < 0:
        raise ValueError("max_file_size must be >= 0")

    if max_total_size is not None and max_total_size < 0:
        raise ValueError("max_total_size must be >= 0")

    area = tempfile.mkdtemp(prefix=_DEFAULT_PREFIX, dir=directory)
    handoff_fields: dict[str, str] = {}
    handoff_files: dict[str, list[HandoffFile]] = {}
    total_size = 0
    file_index = 0

    try:
        for name in names:
            uploads = request.files.getlist(name)

            if uploads:
                for storage in uploads:
                    target_name = f"upload-{file_index:06d}"
                    file_index += 1
                    target_path = os.path.join(area, target_name)
                    size = _copy_upload(
                        storage,
                        target_path,
                        max_file_size=max_file_size,
                        max_total_size=max_total_size,
                        total_size=total_size,
                    )
                    total_size += size
                    handoff_files.setdefault(name, []).append(
                        HandoffFile(
                            name=name,
                            filename=storage.filename,
                            content_type=storage.content_type,
                            content_length=size,
                            path=target_path,
                        )
                    )
            elif name in request.form:
                handoff_fields[name] = request.form[name]
            # A named field that is not present in the request is simply
            # absent from the handoff.
    except BaseException:
        # Copying failed. Remove the partially copied area and do not publish
        # a handoff object.
        shutil.rmtree(area, ignore_errors=True)
        raise

    return RequestHandoff(area, handoff_fields, handoff_files)


def _copy_upload(
    storage: t.Any,
    target_path: str,
    *,
    max_file_size: int | None,
    max_total_size: int | None,
    total_size: int,
) -> int:
    """Copy one uploaded file to ``target_path`` and return its size.

    Enforces the size limits while copying. On failure the partial target is
    removed and the exception propagates.
    """
    stream = storage.stream

    # The stream is normally positioned at the start after parsing, but seek
    # to be safe. Upload streams are always seekable; ignore if not.
    try:
        stream.seek(0)
    except (AttributeError, OSError):
        pass

    size = 0

    try:
        with open(target_path, "wb") as out:
            while True:
                try:
                    chunk = stream.read(_COPY_CHUNK_SIZE)
                except OSError as e:
                    raise HandoffError(
                        f"Failed to copy file {storage.filename!r} for field"
                        f" {storage.name!r}: {e}"
                    ) from e

                if not chunk:
                    break

                size += len(chunk)

                if max_file_size is not None and size > max_file_size:
                    raise HandoffError(
                        f"File {storage.filename!r} for field {storage.name!r} is"
                        f" {size} bytes, exceeding the per-file limit of"
                        f" {max_file_size} bytes."
                    )

                if max_total_size is not None and total_size + size > max_total_size:
                    raise HandoffError(
                        f"Copying file {storage.filename!r} for field"
                        f" {storage.name!r} would bring the total to"
                        f" {total_size + size} bytes, exceeding the total limit"
                        f" of {max_total_size} bytes."
                    )

                out.write(chunk)
    except BaseException:
        try:
            os.remove(target_path)
        except OSError:
            pass

        raise

    return size
