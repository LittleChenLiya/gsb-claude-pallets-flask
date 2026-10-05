from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:  # pragma: no cover
    from .wrappers import Response


class NegotiationRecorder:
    """Records which request header **names** were used to select the content
    of the current response, then merges them into the response's ``Vary``
    header field.

    An instance is created for each request and is available as
    :data:`flask.negotiation`. View functions, ``after_request`` callbacks
    (including callbacks of nested blueprints), and error handlers all share
    the same instance for the current response. After all callbacks have run,
    Flask merges the recorded names into ``response.vary`` automatically.

    Only header **names** are registered, never header values, so the
    recorder is safe to use with headers that carry client-specific data.

    The merge defines the cache identity of the response: shared caches use
    ``Vary`` to decide which stored response matches a future request.
    Registering every header that influenced content selection keeps that
    identity correct, including for ``304 Not Modified`` and ``HEAD``
    responses, which carry the same ``Vary`` as the corresponding ``200``
    response.

    Merge rules:

    -   Header field names are compared case-insensitively and de-duplicated.
    -   Names already present in ``Vary`` are not added again.
    -   The order of names already present in ``Vary`` is never changed; new
        names are appended in the order they were recorded.
    -   ``*`` takes priority: if ``Vary`` already contains ``*`` it is left
        untouched, and if ``*`` is recorded the merged ``Vary`` becomes
        exactly ``*`` (it subsumes any other name).
    -   If nothing was recorded, the response is left completely unchanged.

    .. versionadded:: 3.2
    """

    def __init__(self) -> None:
        self._names: list[str] = []
        self._seen: set[str] = set()
        self._star: bool = False

    def record(self, field_name: str) -> None:
        """Register a request header field name that was used to select the
        response content, for example ``negotiation.record("Accept")``.

        Only the field name is stored, never the header's value. Registering
        the same name again (in any letter case) has no effect. Registering
        ``"*"`` marks the response as varying on more than just header names
        and takes priority over any other recorded name.

        :param field_name: The request header field name to register.

        .. versionadded:: 3.2
        """
        name = field_name.strip()

        if not name:
            raise ValueError("Cannot record an empty header field name.")

        if name == "*":
            self._star = True
            return

        key = name.lower()

        if key in self._seen:
            return

        self._seen.add(key)
        self._names.append(name)

    @property
    def field_names(self) -> tuple[str, ...]:
        """The recorded header field names, in registration order. This is
        ``("*",)`` if ``*`` was recorded. Values are never stored.

        .. versionadded:: 3.2
        """
        if self._star:
            return ("*",)

        return tuple(self._names)

    def apply(self, response: Response) -> bool:
        """Merge the recorded names into ``response.vary`` according to the
        merge rules described in the class docstring. Called automatically by
        Flask when finalizing the response.

        :param response: The response to merge the recorded names into.
        :return: ``True`` if the ``Vary`` header was changed.

        .. versionadded:: 3.2
        """
        vary = response.vary

        # An existing "*" already covers every header, keep it as is.
        if "*" in vary:
            return False

        # A recorded "*" subsumes any other name, existing or recorded.
        if self._star:
            if tuple(vary) != ("*",):
                response.vary = type(vary)(["*"])
                return True

            return False

        changed = False

        for name in self._names:
            if name not in vary:
                vary.add(name)
                changed = True

        return changed

    def __contains__(self, field_name: str) -> bool:
        if field_name.strip() == "*":
            return self._star

        return field_name.strip().lower() in self._seen

    def __len__(self) -> int:
        return len(self.field_names)

    def __iter__(self) -> t.Iterator[str]:
        return iter(self.field_names)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {list(self.field_names)!r}>"
