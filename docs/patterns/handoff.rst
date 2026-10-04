Background Work Handoff
=======================

Background jobs often only need a few form values and some uploaded
files.  They must not be handed the live :data:`~flask.request`: once
the response is sent the request context is popped and Werkzeug closes
or deletes the temporary files that back ``request.files``.  Reading
them from another thread at that point fails or returns garbage.

Call :func:`~flask.stage_handoff` **while the request is active**.  It
copies the selected form fields and uploads into a private, controlled
staging directory and returns a :class:`~flask.RequestHandoff`:

- the caller names the form fields, the file fields, the per-file byte
  limit, and the total byte limit;
- client filenames are recorded as metadata only -- the destination
  files have server-generated names, so names such as
  ``"../../../etc/passwd"`` cannot escape the staging directory;
- uploads are streamed to disk in chunks and every chunk is checked
  against both limits, so an oversized request never fills the disk;
- the handoff object holds plain strings and paths.  It never
  references the request, the request context, or an open upload
  handle.  It is therefore safe to pass to a background thread.

A handoff is published only when **all** copies succeed.  Any failure
-- a missing field, a missing file, a limit, or a stream error --
removes the whole staging directory and raises a subclass of
:class:`~flask.HandoffError`; no object is returned.

Lifecycle
---------

1. **Stage during the request.**  The view calls
   :func:`~flask.stage_handoff` and hands the result to the worker.

   .. code-block:: python

       import threading

       from flask import RequestHandoff, stage_handoff


       def worker(handoff: RequestHandoff) -> None:
           try:
               title = handoff.form["title"]
               report = handoff.get("report")
               process(report.read_bytes(), title)
           finally:
               handoff.close()  # deletes the staging directory


       @app.post("/jobs")
       def create_job():
           handoff = stage_handoff(
               form_fields=("title",),
               file_fields=("report",),
               max_file_size=5 * 1024 * 1024,
               max_total_size=10 * 1024 * 1024,
           )
           threading.Thread(target=worker, args=(handoff,)).start()
           return "", 202

2. **Read after the request ends.**  When the response is sent the
   context tears down and Werkzeug upload temporaries disappear.  The
   handoff is unaffected: its bytes live in its own directory and its
   values are ordinary strings.

3. **Close to clean up.**  :meth:`~flask.RequestHandoff.close` deletes
   the staging directory.  It is idempotent: calling it twice, or
   calling it after the directory was removed, has no effect.  The
   object can also be used as a context manager.  Close from the worker
   in a ``finally`` block so files do not outlive the work.  If a
   worker can crash without closing, point ``parent_dir`` at a directory
   managed by an external temp-file reaper (for example a systemd
   ``tmpfiles.d`` rule).

Errors
------

All staging errors derive from :class:`~flask.HandoffError`:

- :class:`~flask.HandoffFieldsError` -- a named form field was absent
  or empty (raised before any directory is created);
- :class:`~flask.HandoffFileError` -- a named file field was absent or
  had no filename;
- :class:`~flask.HandoffLimitError` -- the per-file or total byte limit
  was exceeded;
- :class:`~flask.HandoffCopyError` -- the upload stream raised an
  ``OSError`` midway through copying.

For the limit and copy errors, any files already copied are deleted
before the exception propagates.

Security and resource notes
---------------------------

- Set the limits according to what the worker can handle.  Both limits
  are enforced while streaming, with at most one 64 KiB chunk written
  past a limit before the copy aborts.
- The original filename is available as
  ``handoff.get("field").filename`` for display or validation, but
  never trust it as a path.  Treat it like any other user input.
- Do not share one handoff between writers.  Multiple readers are safe;
  :meth:`~flask.StagedFile.open` opens the regular file fresh each time.
