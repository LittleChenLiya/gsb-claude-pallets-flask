"""Stage selected form fields and uploaded files for background work.

Background jobs started from a view must not touch the live
:class:`~flask.Request`: the request context and Werkzeug's temporary
spooled uploads are torn down when the response is sent.  This module
copies the chosen values into a controlled temporary area *during* the
request and returns a plain :class:`RequestHandoff`.  The handoff keeps
no reference to the request, its context, or any open upload handle, so
it stays readable from a worker thread after the request has ended.

The caller chooses the field names, the per-file size limit, and the
total size limit.  Uploaded filenames are stored as metadata only; they
never participate in the destination path, so a malicious filename
cannot escape the staging directory.  A handoff is published only when
every copy succeeds; on failure the whole staging directory is removed.

See :func:`stage_handoff` for the entry point and
:doc:`/patterns/handoff` for the lifecycle notes.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import typing as t
from dataclasses import dataclass

if t.TYPE_CHECKING:  # pragma: no cover
    from werkzeug.datastructures import FileStorage
    from werkzeug.wrappers import Request


class HandoffError(Exception):
    """Base class for errors raised while staging a request handoff."""


class HandoffFieldsError(HandoffError):
    """A required form field was missing or empty.

    .. versionadded:: 3.2
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Missing required field(s): {', '.join(missing)}")


class HandoffFileError(HandoffError):
    """A required uploaded file was missing.

    .. versionadded:: 3.2
    """

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"Missing uploaded file(s): {', '.join(missing)}")


class HandoffLimitError(HandoffError):
    """A size limit was exceeded while copying an upload.

    .. versionadded:: 3.2
    """

    def __init__(self, limit: int, total: int, *, per_file: bool) -> None:
        self.limit = limit
        self.total = total
        self.per_file = per_file
        kind = "file" if per_file else "total"
        super().__init__(f"{kind} size limit of {limit} bytes exceeded")


class HandoffCopyError(HandoffError):
    """Copying an upload failed because of an underlying I/O error.

    .. versionadded:: 3.2
    """

    def __init__(self, field: str, filename: str | None, original: OSError) -> None:
        self.field = field
        self.filename = filename
        self.original = original
        super().__init__(f"Could not copy upload {field!r}: {original}")


@dataclass(frozen=True, slots=True)
class StagedFile:
    """One uploaded file copied into the staging area.

    :attr:`filename` is the client-provided name recorded as metadata
    only.  The bytes live at :attr:`path`, a server-controlled name that
    never derives from :attr:`filename`.

    .. versionadded:: 3.2
    """

    field: str
    filename: str
    path: str
    size: int

    def open(self) -> t.IO[bytes]:
        """Open the staged file in binary mode for reading."""
        return open(self.path, "rb")

    def read_bytes(self) -> bytes:
        """Return the full staged file contents."""
        with self.open() as f:
            return f.read()


class RequestHandoff:
    """Context-independent snapshot of selected request data.

    Instances are created by :func:`stage_handoff` and hold plain
    strings and paths inside a private staging directory.  They never
    reference the request, the request context, or an open upload
    stream, so they can be passed to a background thread and used after
    the request ends.

    Call :meth:`close` (or use the object as a context manager) once the
    background work is done to delete the staging directory.  Closing is
    idempotent.

    .. versionadded:: 3.2
    """

    def __init__(
        self,
        form: dict[str, str],
        files: list[StagedFile],
        directory: str,
        *,
        max_file_size: int,
        max_total_size: int,
    ) -> None:
        self._form = form
        self._files = files
        self._directory = directory
        self.max_file_size = max_file_size
        self.max_total_size = max_total_size

    @property
    def form(self) -> dict[str, str]:
        """The selected form field values."""
        return self._form

    @property
    def files(self) -> list[StagedFile]:
        """The staged uploaded files."""
        return self._files

    def get(self, name: str) -> StagedFile | None:
        """Return the staged file for a field name, or ``None``."""
        for staged in self._files:
            if staged.field == name:
                return staged
        return None

    @property
    def directory(self) -> str:
        """Path of the controlled staging directory."""
        return self._directory

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called or the dir is gone."""
        return self._directory == "" or not os.path.isdir(self._directory)

    @property
    def total_size(self) -> int:
        """Total number of staged upload bytes."""
        return sum(staged.size for staged in self._files)

    def close(self) -> None:
        """Remove the staging directory and all staged files.

        Safe to call more than once, and safe to call after the
        directory was removed out from under the object.
        """
        directory = self._directory
        self._directory = ""
        if directory:
            shutil.rmtree(directory, ignore_errors=True)

    def __enter__(self) -> RequestHandoff:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} directory={self._directory or '(closed)'}"
            f" fields={list(self._form)} files={[f.field for f in self._files]}>"
        )


def _copy_limited(
    source: FileStorage,
    dest_path: str,
    total_so_far: int,
    max_file_size: int,
    max_total_size: int,
) -> int:
    """Stream one upload to disk, enforcing both byte limits.

    Returns the number of bytes written.  Raises
    :class:`HandoffLimitError` as soon as a limit would be exceeded and
    :class:`HandoffCopyError` if the source stream fails mid-read.
    """
    written = 0
    try:
        with open(dest_path, "wb") as dest:
            while True:
                chunk = source.stream.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_file_size:
                    raise HandoffLimitError(max_file_size, written, per_file=True)
                if total_so_far + written > max_total_size:
                    raise HandoffLimitError(
                        max_total_size, total_so_far + written, per_file=False
                    )
                dest.write(chunk)
    except HandoffError:
        raise
    except OSError as e:
        raise HandoffCopyError(source.name or "", source.filename, e) from e
    return written


def stage_handoff(
    req: Request | None = None,
    *,
    form_fields: t.Collection[str] = (),
    file_fields: t.Collection[str] = (),
    max_file_size: int,
    max_total_size: int,
    parent_dir: str | os.PathLike[str] | None = None,
) -> RequestHandoff:
    """Copy selected request data into a controlled staging directory.

    This must be called while a request context is active.  Only the
    named form fields and file fields are copied; everything else in
    the request is ignored.

    :param req: request to stage.  Defaults to the current
        :data:`~flask.request`.
    :param form_fields: names of form fields whose string values should
        be captured.
    :param file_fields: names of file uploads that must be present.
    :param max_file_size: maximum size in bytes allowed for one file.
    :param max_total_size: maximum total upload size in bytes.
    :param parent_dir: directory under which the private staging
        directory is created.  Defaults to the system temporary
        directory.
    :raises HandoffFieldsError: a form field is missing.
    :raises HandoffFileError: a file field is missing.
    :raises HandoffLimitError: a per-file or total byte limit is
        exceeded.
    :raises HandoffCopyError: reading an upload fails midway.

    On any error the staging directory is fully removed and no handoff
    is returned.

    .. versionadded:: 3.2
    """
    if max_file_size < 0 or max_total_size < 0:
        raise ValueError("size limits must be non-negative")
    if req is None:
        from .globals import request

        req = request._get_current_object()

    # Validate plain fields first, before touching the filesystem.
    values: dict[str, str] = {}
    missing_fields: list[str] = []
    for name in form_fields:
        value = req.form.get(name)
        if value is None or value == "":
            missing_fields.append(name)
        else:
            values[name] = value
    if missing_fields:
        raise HandoffFieldsError(missing_fields)

    missing_files = [
        name
        for name in file_fields
        if req.files.get(name) is None or not req.files[name].filename
    ]
    if missing_files:
        raise HandoffFileError(missing_files)

    directory = tempfile.mkdtemp(prefix="flask-handoff-", dir=parent_dir)
    staged_files: list[StagedFile] = []
    total = 0
    try:
        for index, name in enumerate(file_fields):
            upload = req.files[name]
            # Server-controlled name: the client filename is metadata only.
            safe_name = f"{index:04d}_{os.getpid()}"
            dest_path = os.path.join(directory, safe_name)
            size = _copy_limited(
                upload,
                dest_path,
                total,
                max_file_size,
                max_total_size,
            )
            total += size
            staged_files.append(
                StagedFile(
                    field=name,
                    filename=upload.filename,  # type: ignore[arg-type]
                    path=dest_path,
                    size=size,
                )
            )
    except BaseException:
        # Limits, mid-copy stream failures, or anything else: never leave
        # a partial staging area behind.
        shutil.rmtree(directory, ignore_errors=True)
        raise

    return RequestHandoff(
        values,
        staged_files,
        directory,
        max_file_size=max_file_size,
        max_total_size=max_total_size,
    )
