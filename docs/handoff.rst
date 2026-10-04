Handoff of Uploaded Data
========================

Background work often needs only part of the data submitted in a request: a
few form fields and one or two uploaded files. Passing the live
:class:`~flask.Request` to a worker thread is unsafe -- the request context is
popped when the response returns, and the uploaded file handles are closed
with it.

:meth:`Request.handoff <flask.Request.handoff>` solves this by copying the
selected form fields and uploaded files into a private temporary directory
and returning a :class:`~flask.RequestHandoff` that is detached from the
request. The handoff holds no reference to the request, the request context,
or the original file handles, so it can safely be read from a background
thread after the request has ended.

.. code-block:: python

    import threading
    from flask import Flask, request

    app = Flask(__name__)

    @app.post("/upload")
    def upload():
        handoff = request.handoff(
            ["description", "report"],
            max_file_size=1024 * 1024,
            max_total_size=10 * 1024 * 1024,
        )

        def work():
            description = handoff["description"]
            with handoff.file("report").open() as f:
                data = f.read()
            # process data ...
            handoff.close()

        threading.Thread(target=work).start()
        return "accepted"

Lifecycle
---------

A handoff goes through four stages:

1. **Request parsing.** The client submits a ``multipart/form-data`` request.
   Werkzeug parses it into form fields and uploaded files, held by the
   request for the duration of the request context.

2. **Explicit handoff.** The view calls :meth:`Request.handoff
   <flask.Request.handoff>` with the field names it needs. A private temporary
   directory is created. Each selected field value is copied into the handoff's
   field mapping. Each selected uploaded file is copied byte-for-byte into the
   temporary directory under a generated name.

   Limits are enforced while copying: ``max_file_size`` caps the size of a
   single file, ``max_total_size`` caps the combined size of all copied files.
   If a limit is exceeded, or if reading an uploaded file fails,
   :class:`~flask.HandoffError` is raised, the partially copied directory is
   removed, and no handoff object is published. The handoff is only returned
   once every copy has succeeded.

3. **Detached use.** The request context is popped and the response returns.
   The handoff remains valid because it owns its own copies on disk and holds
   no reference to the request. A background thread can read the field values
   and open the copied files for as long as it likes.

4. **Close.** When the background work is done, the caller calls
   :meth:`~flask.RequestHandoff.close` to remove the temporary directory.
   Closing is idempotent -- calling it more than once has no effect. The
   handoff also works as a context manager, which closes it automatically.

.. code-block:: python

    with request.handoff(["report"]) as handoff:
        with handoff.file("report").open() as f:
            data = f.read()
    # the temporary directory is removed here

Security notes
--------------

- **Filenames are metadata only.** The original filename is recorded on the
  :class:`~flask.HandoffFile` but is never used as or part of the target path.
  Target paths are generated names inside the private temporary directory. A
  filename containing ``../`` or an absolute path cannot escape the temporary
  area.

- **Only selected fields are copied.** Fields not listed in ``fields`` are
  ignored and never leave the request. Fields that are listed but not present
  in the request are simply absent from the handoff.

- **Limits are enforced during the copy**, not after. A file that would exceed
  the per-file or total limit is detected while reading, so no oversized copy
  is written.

- **The handoff does not outlive its usefulness.** Call
  :meth:`~flask.RequestHandoff.close` as soon as the background work is done.
  The temporary directory is created with the system's default permissions for
  private temporary directories.

API reference
-------------

.. autoclass:: flask.RequestHandoff
    :members:

.. autoclass:: flask.HandoffFile
    :members:

.. autoclass:: flask.HandoffError
    :members:

.. automethod:: flask.handoff_request
