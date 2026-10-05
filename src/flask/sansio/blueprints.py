from __future__ import annotations

import os
import typing as t
from collections import defaultdict
from functools import update_wrapper

from .. import typing as ft
from .scaffold import _endpoint_from_view_func
from .scaffold import _sentinel
from .scaffold import Scaffold
from .scaffold import setupmethod

if t.TYPE_CHECKING:  # pragma: no cover
    from .app import App

DeferredSetupFunction = t.Callable[["BlueprintSetupState"], None]
T_after_request = t.TypeVar("T_after_request", bound=ft.AfterRequestCallable[t.Any])
T_before_request = t.TypeVar("T_before_request", bound=ft.BeforeRequestCallable)
T_error_handler = t.TypeVar("T_error_handler", bound=ft.ErrorHandlerCallable)
T_teardown = t.TypeVar("T_teardown", bound=ft.TeardownCallable)
T_template_context_processor = t.TypeVar(
    "T_template_context_processor", bound=ft.TemplateContextProcessorCallable
)
T_template_filter = t.TypeVar("T_template_filter", bound=ft.TemplateFilterCallable)
T_template_global = t.TypeVar("T_template_global", bound=ft.TemplateGlobalCallable)
T_template_test = t.TypeVar("T_template_test", bound=ft.TemplateTestCallable)
T_url_defaults = t.TypeVar("T_url_defaults", bound=ft.URLDefaultCallable)
T_url_value_preprocessor = t.TypeVar(
    "T_url_value_preprocessor", bound=ft.URLValuePreprocessorCallable
)


class BlueprintSetupState:
    """Temporary holder object for registering a blueprint with the
    application.  An instance of this class is created by the
    :meth:`~flask.Blueprint.make_setup_state` method and later passed
    to all register callback functions.
    """

    def __init__(
        self,
        blueprint: Blueprint,
        app: App,
        options: t.Any,
        first_registration: bool,
    ) -> None:
        #: a reference to the current application
        self.app = app

        #: a reference to the blueprint that created this setup state.
        self.blueprint = blueprint

        #: a dictionary with all options that were passed to the
        #: :meth:`~flask.Flask.register_blueprint` method.
        self.options = options

        #: as blueprints can be registered multiple times with the
        #: application and not everything wants to be registered
        #: multiple times on it, this attribute can be used to figure
        #: out if the blueprint was registered in the past already.
        self.first_registration = first_registration

        subdomain = self.options.get("subdomain")
        if subdomain is None:
            subdomain = self.blueprint.subdomain

        #: The subdomain that the blueprint should be active for, ``None``
        #: otherwise.
        self.subdomain = subdomain

        url_prefix = self.options.get("url_prefix")
        if url_prefix is None:
            url_prefix = self.blueprint.url_prefix
        #: The prefix that should be used for all URLs defined on the
        #: blueprint.
        self.url_prefix = url_prefix

        self.name = self.options.get("name", blueprint.name)
        self.name_prefix = self.options.get("name_prefix", "")

        #: A dictionary with URL defaults that is added to each and every
        #: URL that was defined with the blueprint.
        self.url_defaults = dict(self.blueprint.url_values_defaults)
        self.url_defaults.update(self.options.get("url_defaults", ()))

    def add_url_rule(
        self,
        rule: str,
        endpoint: str | None = None,
        view_func: ft.RouteCallable | None = None,
        **options: t.Any,
    ) -> None:
        """A helper method to register a rule (and optionally a view function)
        to the application.  The endpoint is automatically prefixed with the
        blueprint's name.
        """
        if self.url_prefix is not None:
            if rule:
                rule = "/".join((self.url_prefix.rstrip("/"), rule.lstrip("/")))
            else:
                rule = self.url_prefix
        options.setdefault("subdomain", self.subdomain)
        if endpoint is None:
            endpoint = _endpoint_from_view_func(view_func)  # type: ignore
        defaults = self.url_defaults
        if "defaults" in options:
            defaults = dict(defaults, **options.pop("defaults"))

        self.app.add_url_rule(
            rule,
            f"{self.name_prefix}.{self.name}.{endpoint}".lstrip("."),
            view_func,
            defaults=defaults,
            **options,
        )


class Blueprint(Scaffold):
    """Represents a blueprint, a collection of routes and other
    app-related functions that can be registered on a real application
    later.

    A blueprint is an object that allows defining application functions
    without requiring an application object ahead of time. It uses the
    same decorators as :class:`~flask.Flask`, but defers the need for an
    application by recording them for later registration.

    Decorating a function with a blueprint creates a deferred function
    that is called with :class:`~flask.blueprints.BlueprintSetupState`
    when the blueprint is registered on an application.

    See :doc:`/blueprints` for more information.

    :param name: The name of the blueprint. Will be prepended to each
        endpoint name.
    :param import_name: The name of the blueprint package, usually
        ``__name__``. This helps locate the ``root_path`` for the
        blueprint.
    :param static_folder: A folder with static files that should be
        served by the blueprint's static route. The path is relative to
        the blueprint's root path. Blueprint static files are disabled
        by default.
    :param static_url_path: The url to serve static files from.
        Defaults to ``static_folder``. If the blueprint does not have
        a ``url_prefix``, the app's static route will take precedence,
        and the blueprint's static files won't be accessible.
    :param template_folder: A folder with templates that should be added
        to the app's template search path. The path is relative to the
        blueprint's root path. Blueprint templates are disabled by
        default. Blueprint templates have a lower precedence than those
        in the app's templates folder.
    :param url_prefix: A path to prepend to all of the blueprint's URLs,
        to make them distinct from the rest of the app's routes.
    :param subdomain: A subdomain that blueprint routes will match on by
        default.
    :param url_defaults: A dict of default values that blueprint routes
        will receive by default.
    :param root_path: By default, the blueprint will automatically set
        this based on ``import_name``. In certain situations this
        automatic detection can fail, so the path can be specified
        manually instead.

    .. versionchanged:: 1.1.0
        Blueprints have a ``cli`` group to register nested CLI commands.
        The ``cli_group`` parameter controls the name of the group under
        the ``flask`` command.

    .. versionadded:: 0.7
    """

    _got_registered_once = False

    def __init__(
        self,
        name: str,
        import_name: str,
        static_folder: str | os.PathLike[str] | None = None,
        static_url_path: str | None = None,
        template_folder: str | os.PathLike[str] | None = None,
        url_prefix: str | None = None,
        subdomain: str | None = None,
        url_defaults: dict[str, t.Any] | None = None,
        root_path: str | None = None,
        cli_group: str | None = _sentinel,  # type: ignore[assignment]
    ):
        super().__init__(
            import_name=import_name,
            static_folder=static_folder,
            static_url_path=static_url_path,
            template_folder=template_folder,
            root_path=root_path,
        )

        if not name:
            raise ValueError("'name' may not be empty.")

        if "." in name:
            raise ValueError("'name' may not contain a dot '.' character.")

        self.name = name
        self.url_prefix = url_prefix
        self.subdomain = subdomain
        self.deferred_functions: list[DeferredSetupFunction] = []

        if url_defaults is None:
            url_defaults = {}

        self.url_values_defaults = url_defaults
        self.cli_group = cli_group
        self._blueprints: list[tuple[Blueprint, dict[str, t.Any]]] = []

    def _check_setup_finished(self, f_name: str) -> None:
        if self._got_registered_once:
            raise AssertionError(
                f"The setup method '{f_name}' can no longer be called on the blueprint"
                f" '{self.name}'. It has already been registered at least once, any"
                " changes will not be applied consistently.\n"
                "Make sure all imports, decorators, functions, etc. needed to set up"
                " the blueprint are done before registering it."
            )

    @setupmethod
    def record(self, func: DeferredSetupFunction) -> None:
        """Registers a function that is called when the blueprint is
        registered on the application.  This function is called with the
        state as argument as returned by the :meth:`make_setup_state`
        method.
        """
        self.deferred_functions.append(func)

    @setupmethod
    def record_once(self, func: DeferredSetupFunction) -> None:
        """Works like :meth:`record` but wraps the function in another
        function that will ensure the function is only called once.  If the
        blueprint is registered a second time on the application, the
        function passed is not called.
        """

        def wrapper(state: BlueprintSetupState) -> None:
            if state.first_registration:
                func(state)

        self.record(update_wrapper(wrapper, func))

    def make_setup_state(
        self, app: App, options: dict[str, t.Any], first_registration: bool = False
    ) -> BlueprintSetupState:
        """Creates an instance of :meth:`~flask.blueprints.BlueprintSetupState`
        object that is later passed to the register callback functions.
        Subclasses can override this to return a subclass of the setup state.
        """
        return BlueprintSetupState(self, app, options, first_registration)

    @setupmethod
    def register_blueprint(self, blueprint: Blueprint, **options: t.Any) -> None:
        """Register a :class:`~flask.Blueprint` on this blueprint. Keyword
        arguments passed to this method will override the defaults set
        on the blueprint.

        .. versionchanged:: 2.0.1
            The ``name`` option can be used to change the (pre-dotted)
            name the blueprint is registered with. This allows the same
            blueprint to be registered multiple times with unique names
            for ``url_for``.

        .. versionadded:: 2.0
        """
        if blueprint is self:
            raise ValueError("Cannot register a blueprint on itself")
        self._blueprints.append((blueprint, options))

    def register(self, app: App, options: dict[str, t.Any]) -> None:
        """Called by :meth:`Flask.register_blueprint` to register all
        views and callbacks registered on the blueprint with the
        application. Creates a :class:`.BlueprintSetupState` and calls
        each :meth:`record` callback with it.

        :param app: The application this blueprint is being registered
            with.
        :param options: Keyword arguments forwarded from
            :meth:`~Flask.register_blueprint`.

        .. versionchanged:: 2.3
            Nested blueprints now correctly apply subdomains.

        .. versionchanged:: 2.1
            Registering the same blueprint with the same name multiple
            times is an error.

        .. versionchanged:: 2.0.1
            Nested blueprints are registered with their dotted name.
            This allows different blueprints with the same name to be
            nested at different locations.

        .. versionchanged:: 2.0.1
            The ``name`` option can be used to change the (pre-dotted)
            name the blueprint is registered with. This allows the same
            blueprint to be registered multiple times with unique names
            for ``url_for``.
        """
        name_prefix = options.get("name_prefix", "")
        self_name = options.get("name", self.name)
        name = f"{name_prefix}.{self_name}".lstrip(".")

        if name in app.blueprints:
            bp_desc = "this" if app.blueprints[name] is self else "a different"
            existing_at = f" '{name}'" if self_name != name else ""

            raise ValueError(
                f"The name '{self_name}' is already registered for"
                f" {bp_desc} blueprint{existing_at}. Use 'name=' to"
                f" provide a unique name."
            )

        first_bp_registration = not any(bp is self for bp in app.blueprints.values())
        first_name_registration = name not in app.blueprints

        app.blueprints[name] = self
        self._got_registered_once = True
        state = self.make_setup_state(app, options, first_bp_registration)

        if self.has_static_folder:
            state.add_url_rule(
                f"{self.static_url_path}/<path:filename>",
                view_func=self.send_static_file,  # type: ignore[attr-defined]
                endpoint="static",
            )

        # Merge blueprint data into parent.
        if first_bp_registration or first_name_registration:
            self._merge_blueprint_funcs(app, name)

        for deferred in self.deferred_functions:
            deferred(state)

        cli_resolved_group = options.get("cli_group", self.cli_group)

        if self.cli.commands:
            if cli_resolved_group is None:
                app.cli.commands.update(self.cli.commands)
            elif cli_resolved_group is _sentinel:
                self.cli.name = name
                app.cli.add_command(self.cli)
            else:
                self.cli.name = cli_resolved_group
                app.cli.add_command(self.cli)

        for blueprint, bp_options in self._blueprints:
            bp_options = bp_options.copy()
            bp_url_prefix = bp_options.get("url_prefix")
            bp_subdomain = bp_options.get("subdomain")

            if bp_subdomain is None:
                bp_subdomain = blueprint.subdomain

            if state.subdomain is not None and bp_subdomain is not None:
                bp_options["subdomain"] = bp_subdomain + "." + state.subdomain
            elif bp_subdomain is not None:
                bp_options["subdomain"] = bp_subdomain
            elif state.subdomain is not None:
                bp_options["subdomain"] = state.subdomain

            if bp_url_prefix is None:
                bp_url_prefix = blueprint.url_prefix

            if state.url_prefix is not None and bp_url_prefix is not None:
                bp_options["url_prefix"] = (
                    state.url_prefix.rstrip("/") + "/" + bp_url_prefix.lstrip("/")
                )
            elif bp_url_prefix is not None:
                bp_options["url_prefix"] = bp_url_prefix
            elif state.url_prefix is not None:
                bp_options["url_prefix"] = state.url_prefix

            bp_options["name_prefix"] = name
            blueprint.register(app, bp_options)

    def _merge_blueprint_funcs(self, app: App, name: str) -> None:
        def extend(
            bp_dict: dict[ft.AppOrBlueprintKey, list[t.Any]],
            parent_dict: dict[ft.AppOrBlueprintKey, list[t.Any]],
        ) -> None:
            for key, values in bp_dict.items():
                key = name if key is None else f"{name}.{key}"
                parent_dict[key].extend(values)

        for key, value in self.error_handler_spec.items():
            key = name if key is None else f"{name}.{key}"
            value = defaultdict(
                dict,
                {
                    code: {exc_class: func for exc_class, func in code_values.items()}
                    for code, code_values in value.items()
                },
            )
            app.error_handler_spec[key] = value

        for endpoint, func in self.view_functions.items():
            app.view_functions[endpoint] = func

        extend(self.before_request_funcs, app.before_request_funcs)
        extend(self.after_request_funcs, app.after_request_funcs)
        extend(
            self.teardown_request_funcs,
            app.teardown_request_funcs,
        )
        extend(self.url_default_functions, app.url_default_functions)
        extend(self.url_value_preprocessors, app.url_value_preprocessors)
        extend(self.template_context_processors, app.template_context_processors)

    @setupmethod
    def add_url_rule(
        self,
        rule: str,
        endpoint: str | None = None,
        view_func: ft.RouteCallable | None = None,
        provide_automatic_options: bool | None = None,
        **options: t.Any,
    ) -> None:
        """Register a URL rule with the blueprint. See :meth:`.Flask.add_url_rule` for
        full documentation.

        The URL rule is prefixed with the blueprint's URL prefix. The endpoint name,
        used with :func:`url_for`, is prefixed with the blueprint's name.
        """
        if endpoint and "." in endpoint:
            raise ValueError("'endpoint' may not contain a dot '.' character.")

        if view_func and hasattr(view_func, "__name__") and "." in view_func.__name__:
            raise ValueError("'view_func' name may not contain a dot '.' character.")

        self.record(
            lambda s: s.add_url_rule(
                rule,
                endpoint,
                view_func,
                provide_automatic_options=provide_automatic_options,
                **options,
            )
        )

    @t.overload
    def app_template_filter(self, name: T_template_filter) -> T_template_filter: ...
    @t.overload
    def app_template_filter(
        self, name: str | None = None
    ) -> t.Callable[[T_template_filter], T_template_filter]: ...
    @setupmethod
    def app_template_filter(
        self, name: T_template_filter | str | None = None
    ) -> T_template_filter | t.Callable[[T_template_filter], T_template_filter]:
        """Decorate a function to register it as a custom Jinja filter. The name
        is optional. The decorator may be used without parentheses.

        The :meth:`add_app_template_filter` method may be used to register a
        function later rather than decorating.

        The filter is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.template_filter`.

        :param name: The name to register the filter as. If not given, uses the
            function's name.
        """
        if callable(name):
            self.add_app_template_filter(name)
            return name

        def decorator(f: T_template_filter) -> T_template_filter:
            self.add_app_template_filter(f, name=name)
            return f

        return decorator

    @setupmethod
    def add_app_template_filter(
        self, f: ft.TemplateFilterCallable, name: str | None = None
    ) -> None:
        """Register a function to use as a custom Jinja filter.

        The :meth:`app_template_filter` decorator can be used to register a
        function by decorating instead.

        The filter is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.add_template_filter`.

        :param f: The function to register.
        :param name: The name to register the filter as. If not given, uses the
            function's name.
        """

        def register_template_filter(state: BlueprintSetupState) -> None:
            state.app.add_template_filter(f, name=name)

        self.record_once(register_template_filter)

    @t.overload
    def app_template_test(self, name: T_template_test) -> T_template_test: ...
    @t.overload
    def app_template_test(
        self, name: str | None = None
    ) -> t.Callable[[T_template_test], T_template_test]: ...
    @setupmethod
    def app_template_test(
        self, name: T_template_test | str | None = None
    ) -> T_template_test | t.Callable[[T_template_test], T_template_test]:
        """Decorate a function to register it as a custom Jinja test. The name
        is optional. The decorator may be used without parentheses.

        The :meth:`add_app_template_test` method may be used to register a
        function later rather than decorating.

        The test is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.template_test`.

        :param name: The name to register the filter as. If not given, uses the
            function's name.

        .. versionadded:: 0.10
        """
        if callable(name):
            self.add_app_template_test(name)
            return name

        def decorator(f: T_template_test) -> T_template_test:
            self.add_app_template_test(f, name=name)
            return f

        return decorator

    @setupmethod
    def add_app_template_test(
        self, f: ft.TemplateTestCallable, name: str | None = None
    ) -> None:
        """Register a function to use as a custom Jinja test.

        The :meth:`app_template_test` decorator can be used to register a
        function by decorating instead.

        The test is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.add_template_test`.

        :param f: The function to register.
        :param name: The name to register the test as. If not given, uses the
            function's name.

        .. versionadded:: 0.10
        """

        def register_template_test(state: BlueprintSetupState) -> None:
            state.app.add_template_test(f, name=name)

        self.record_once(register_template_test)

    @t.overload
    def app_template_global(self, name: T_template_global) -> T_template_global: ...
    @t.overload
    def app_template_global(
        self, name: str | None = None
    ) -> t.Callable[[T_template_global], T_template_global]: ...
    @setupmethod
    def app_template_global(
        self, name: T_template_global | str | None = None
    ) -> T_template_global | t.Callable[[T_template_global], T_template_global]:
        """Decorate a function to register it as a custom Jinja global. The name
        is optional. The decorator may be used without parentheses.

        The :meth:`add_app_template_global` method may be used to register a
        function later rather than decorating.

        The global is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.template_global`.

        :param name: The name to register the global as. If not given, uses the
            function's name.

        .. versionadded:: 0.10
        """
        if callable(name):
            self.add_app_template_global(name)
            return name

        def decorator(f: T_template_global) -> T_template_global:
            self.add_app_template_global(f, name=name)
            return f

        return decorator

    @setupmethod
    def add_app_template_global(
        self, f: ft.TemplateGlobalCallable, name: str | None = None
    ) -> None:
        """Register a function to use as a custom Jinja global.

        The :meth:`app_template_global` decorator can be used to register a function
        by decorating instead.

        The global is available in all templates, not only those under this
        blueprint. Equivalent to :meth:`.Flask.add_template_global`.

        :param f: The function to register.
        :param name: The name to register the global as. If not given, uses the
            function's name.

        .. versionadded:: 0.10
        """

        def register_template_global(state: BlueprintSetupState) -> None:
            state.app.add_template_global(f, name=name)

        self.record_once(register_template_global)

    @setupmethod
    def before_app_request(self, f: T_before_request) -> T_before_request:
        """Like :meth:`before_request`, but before every request, not only those handled
        by the blueprint. Equivalent to :meth:`.Flask.before_request`.
        """
        self.record_once(
            lambda s: s.app.before_request_funcs.setdefault(None, []).append(f)
        )
        return f

    @setupmethod
    def after_app_request(self, f: T_after_request) -> T_after_request:
        """Like :meth:`after_request`, but after every request, not only those handled
        by the blueprint. Equivalent to :meth:`.Flask.after_request`.
        """
        self.record_once(
            lambda s: s.app.after_request_funcs.setdefault(None, []).append(f)
        )
        return f

    @setupmethod
    def teardown_app_request(self, f: T_teardown) -> T_teardown:
        """Like :meth:`teardown_request`, but after every request, not only those
        handled by the blueprint. Equivalent to :meth:`.Flask.teardown_request`.
        """
        self.record_once(
            lambda s: s.app.teardown_request_funcs.setdefault(None, []).append(f)
        )
        return f

    @setupmethod
    def app_context_processor(
        self, f: T_template_context_processor
    ) -> T_template_context_processor:
        """Like :meth:`context_processor`, but for templates rendered by every view, not
        only by the blueprint. Equivalent to :meth:`.Flask.context_processor`.
        """
        self.record_once(
            lambda s: s.app.template_context_processors.setdefault(None, []).append(f)
        )
        return f

    @setupmethod
    def app_errorhandler(
        self, code: type[Exception] | int
    ) -> t.Callable[[T_error_handler], T_error_handler]:
        """Like :meth:`errorhandler`, but for every request, not only those handled by
        the blueprint. Equivalent to :meth:`.Flask.errorhandler`.
        """

        def decorator(f: T_error_handler) -> T_error_handler:
            def from_blueprint(state: BlueprintSetupState) -> None:
                state.app.errorhandler(code)(f)

            self.record_once(from_blueprint)
            return f

        return decorator

    @setupmethod
    def app_url_value_preprocessor(
        self, f: T_url_value_preprocessor
    ) -> T_url_value_preprocessor:
        """Like :meth:`url_value_preprocessor`, but for every request, not only those
        handled by the blueprint. Equivalent to :meth:`.Flask.url_value_preprocessor`.
        """
        self.record_once(
            lambda s: s.app.url_value_preprocessors.setdefault(None, []).append(f)
        )
        return f

    @setupmethod
    def app_url_defaults(self, f: T_url_defaults) -> T_url_defaults:
        """Like :meth:`url_defaults`, but for every request, not only those handled by
        the blueprint. Equivalent to :meth:`.Flask.url_defaults`.
        """
        self.record_once(
            lambda s: s.app.url_default_functions.setdefault(None, []).append(f)
        )
        return f


class BlueprintConflict:
    """A conflict that registering a blueprint would introduce.

    Returned by :meth:`BlueprintRegistrationPlan.conflicts`. Each conflict
    describes a single item -- a blueprint name, endpoint, URL rule, or CLI
    name -- that would collide with existing application state or with
    another item in the same registration.

    :param kind: The kind of conflict. One of ``"blueprint"``,
        ``"endpoint"``, ``"url_rule"``, or ``"cli"``.
    :param name: The conflicting item (blueprint name, endpoint, rule, or CLI
        name).
    :param message: A human-readable description of the conflict.
    :param blueprint: The blueprint that would introduce the conflict, if
        known.
    """

    def __init__(
        self,
        kind: str,
        name: str,
        message: str,
        blueprint: Blueprint | None = None,
    ) -> None:
        self.kind = kind
        self.name = name
        self.message = message
        self.blueprint = blueprint

    def __repr__(self) -> str:
        return f"<BlueprintConflict {self.kind!r} {self.name!r}: {self.message}>"


class _RecordingApp:
    """A stand-in for the application that records ``add_url_rule`` calls
    instead of executing them.

    This lets the preflight follow the exact logic of
    :meth:`BlueprintSetupState.add_url_rule` (URL prefix joining, endpoint
    prefixing, defaults merging) without touching the application's URL map
    or view functions.
    """

    def __init__(self) -> None:
        self.url_rules: list[
            tuple[str, str, t.Any, dict[str, t.Any]]
        ] = []

    def add_url_rule(
        self,
        rule: str,
        endpoint: str,
        view_func: ft.RouteCallable | None = None,
        **options: t.Any,
    ) -> None:
        self.url_rules.append((rule, endpoint, view_func, options))


class BlueprintRegistrationPlan:
    """The result of statically planning a blueprint registration.

    Create a plan with :meth:`~flask.sansio.app.App.plan_blueprint` (or
    :meth:`~flask.Flask.plan_blueprint`). The plan walks the blueprint tree
    and statically determines the endpoint, URL rule, and CLI name conflicts
    that registering the blueprint would introduce, without calling any user
    callbacks and without modifying the application.

    The plan does not register the blueprint. Use
    :meth:`~flask.Flask.register_blueprint` to perform the actual
    registration once the conflicts have been resolved.

    :param app: The application the blueprint would be registered on.
    :param blueprint: The blueprint to plan.
    :param options: Keyword arguments that would be passed to
        :meth:`~flask.Flask.register_blueprint`.

    .. versionadded:: 3.2
    """

    def __init__(
        self,
        app: App,
        blueprint: Blueprint,
        options: dict[str, t.Any],
    ) -> None:
        self._app = app
        self.blueprint = blueprint
        self.options = options

        #: A list of :class:`BlueprintConflict` objects describing the
        #: conflicts the registration would introduce.
        self.conflicts: list[BlueprintConflict] = []

        #: A list of deferred setup functions that could not be interpreted
        #: statically and would be called during real registration. The
        #: preflight does not call these functions.
        self.unknown: list[DeferredSetupFunction] = []

        #: The endpoints the registration would add, in order.
        self.endpoints: list[str] = []

        #: The URL rules the registration would add, in order.
        self.rules: list[str] = []

        #: The CLI names the registration would add, in order.
        self.cli_names: list[str] = []

        self._seen_names: set[str] = set()
        self._seen_endpoints: dict[str, t.Any] = {}
        self._seen_rules: set[str] = set()
        self._seen_cli: set[str] = set()

        self._plan(blueprint, options)

    @property
    def ok(self) -> bool:
        """``True`` if the registration would not introduce any conflicts."""
        return not self.conflicts

    #: An alias for :attr:`ok`.
    is_valid = ok

    def __repr__(self) -> str:
        status = "ok" if self.ok else f"{len(self.conflicts)} conflict(s)"
        return f"<BlueprintRegistrationPlan {self.blueprint.name!r}: {status}>"

    def _plan(
        self,
        blueprint: Blueprint,
        options: dict[str, t.Any],
        name_prefix: str = "",
        parent_url_prefix: str | None = None,
        parent_subdomain: str | None = None,
    ) -> None:
        # Compute the dotted registration name, mirroring Blueprint.register.
        self_name = options.get("name", blueprint.name)
        name = f"{name_prefix}.{self_name}".lstrip(".")

        # Compute the effective URL prefix, mirroring the nested-blueprint
        # merge in Blueprint.register.
        bp_url_prefix = options.get("url_prefix")
        if bp_url_prefix is None:
            bp_url_prefix = blueprint.url_prefix

        if parent_url_prefix is not None and bp_url_prefix is not None:
            url_prefix = (
                parent_url_prefix.rstrip("/") + "/" + bp_url_prefix.lstrip("/")
            )
        elif bp_url_prefix is not None:
            url_prefix = bp_url_prefix
        elif parent_url_prefix is not None:
            url_prefix = parent_url_prefix
        else:
            url_prefix = None

        # Compute the effective subdomain, mirroring the nested-blueprint
        # merge in Blueprint.register.
        bp_subdomain = options.get("subdomain")
        if bp_subdomain is None:
            bp_subdomain = blueprint.subdomain

        if parent_subdomain is not None and bp_subdomain is not None:
            subdomain = bp_subdomain + "." + parent_subdomain
        elif bp_subdomain is not None:
            subdomain = bp_subdomain
        elif parent_subdomain is not None:
            subdomain = parent_subdomain
        else:
            subdomain = None

        # A blueprint name conflict is a hard error in real registration.
        if name in self._app.blueprints:
            self.conflicts.append(
                BlueprintConflict(
                    "blueprint",
                    name,
                    f"The blueprint name {name!r} is already registered for a"
                    " different blueprint. Use 'name=' to provide a unique"
                    " name.",
                    blueprint,
                )
            )
        elif name in self._seen_names:
            self.conflicts.append(
                BlueprintConflict(
                    "blueprint",
                    name,
                    f"The blueprint name {name!r} would be registered more"
                    " than once.",
                    blueprint,
                )
            )
        self._seen_names.add(name)

        # Create a setup state with a recording stand-in for the app, so the
        # state's real rule/endpoint logic is followed without mutating the
        # application.
        recording_app = _RecordingApp()
        merged_options = dict(options)
        merged_options["url_prefix"] = url_prefix
        merged_options["subdomain"] = subdomain
        merged_options["name_prefix"] = name_prefix
        first_registration = not any(
            bp is blueprint for bp in self._app.blueprints.values()
        )
        state = blueprint.make_setup_state(
            t.cast("App", recording_app), merged_options, first_registration
        )

        # The static route is always registered when a static folder is set.
        if blueprint.has_static_folder:
            state.add_url_rule(
                f"{blueprint.static_url_path}/<path:filename>",
                view_func=blueprint.send_static_file,  # type: ignore[attr-defined]
                endpoint="static",
            )

        # Interpret deferred functions. The lambdas created by
        # Blueprint.add_url_rule can be interpreted statically; anything else
        # (user callbacks, record_once wrappers, etc.) is marked unknown
        # rather than called.
        for deferred in blueprint.deferred_functions:
            if not self._interpret(deferred, state):
                self.unknown.append(deferred)

        # Check the recorded URL rules for endpoint and rule conflicts.
        for rule, endpoint, view_func, _rule_options in recording_app.url_rules:
            self.endpoints.append(endpoint)
            self.rules.append(rule)

            existing_func = self._app.view_functions.get(endpoint)
            if existing_func is not None and existing_func != view_func:
                self.conflicts.append(
                    BlueprintConflict(
                        "endpoint",
                        endpoint,
                        f"The endpoint {endpoint!r} is already registered to a"
                        " different view function.",
                        blueprint,
                    )
                )
            elif (
                endpoint in self._seen_endpoints
                and self._seen_endpoints[endpoint] != view_func
            ):
                self.conflicts.append(
                    BlueprintConflict(
                        "endpoint",
                        endpoint,
                        f"The endpoint {endpoint!r} would be registered to more"
                        " than one view function.",
                        blueprint,
                    )
                )
            self._seen_endpoints[endpoint] = view_func

            # A URL rule path collision is not a hard error (werkzeug allows
            # duplicate rules), but two rules with the same path in the same
            # registration would shadow each other. Only flag duplicates
            # within this registration -- re-registering the same blueprint
            # with a different name is legal and would otherwise be flagged.
            if rule in self._seen_rules:
                self.conflicts.append(
                    BlueprintConflict(
                        "url_rule",
                        rule,
                        f"The URL rule {rule!r} would be registered more than"
                        " once.",
                        blueprint,
                    )
                )
            self._seen_rules.add(rule)

        # Check CLI name conflicts, mirroring Blueprint.register.
        cli_group = options.get("cli_group", blueprint.cli_group)

        if blueprint.cli.commands:
            if cli_group is None:
                # Commands are merged directly into the app's CLI group.
                for cmd_name in blueprint.cli.commands:
                    self.cli_names.append(cmd_name)
                    if (
                        cmd_name in self._app.cli.commands
                        or cmd_name in self._seen_cli
                    ):
                        self.conflicts.append(
                            BlueprintConflict(
                                "cli",
                                cmd_name,
                                f"The CLI command {cmd_name!r} is already"
                                " registered.",
                                blueprint,
                            )
                        )
                    self._seen_cli.add(cmd_name)
            elif cli_group is _sentinel:
                # The blueprint's CLI group is added under its dotted name.
                self.cli_names.append(name)
                if name in self._app.cli.commands or name in self._seen_cli:
                    self.conflicts.append(
                        BlueprintConflict(
                            "cli",
                            name,
                            f"The CLI group {name!r} is already registered.",
                            blueprint,
                        )
                    )
                self._seen_cli.add(name)
            else:
                # The blueprint's CLI group is added under a custom name.
                self.cli_names.append(cli_group)
                if (
                    cli_group in self._app.cli.commands
                    or cli_group in self._seen_cli
                ):
                    self.conflicts.append(
                        BlueprintConflict(
                            "cli",
                            cli_group,
                            f"The CLI group {cli_group!r} is already"
                            " registered.",
                            blueprint,
                        )
                    )
                self._seen_cli.add(cli_group)

        # Recurse into nested blueprints, mirroring Blueprint.register.
        for child, child_options in blueprint._blueprints:
            child_options = dict(child_options)
            self._plan(
                child,
                child_options,
                name_prefix=name,
                parent_url_prefix=url_prefix,
                parent_subdomain=subdomain,
            )

    def _interpret(
        self,
        deferred: DeferredSetupFunction,
        state: BlueprintSetupState,
    ) -> bool:
        """Try to interpret a deferred setup function as a URL rule.

        The lambdas created by :meth:`Blueprint.add_url_rule` close over the
        same five variables, so they can be interpreted statically by
        reading their closure cells. Any other function (a user callback, a
        ``record_once`` wrapper, etc.) cannot be interpreted and is marked
        unknown rather than called.

        Returns ``True`` if the function was interpreted (and the rule was
        recorded on the state), ``False`` otherwise.
        """
        try:
            code = deferred.__code__
            closure = deferred.__closure__
        except AttributeError:
            return False

        if closure is None:
            return False

        # The lambda created by Blueprint.add_url_rule closes over these
        # five variables. co_freevars is ordered alphabetically, so extract
        # the values by name rather than by position.
        freevars = code.co_freevars
        if set(freevars) != {
            "rule",
            "endpoint",
            "view_func",
            "provide_automatic_options",
            "options",
        }:
            return False

        try:
            values = {
                name: cell.cell_contents
                for name, cell in zip(freevars, closure)
            }
        except ValueError:
            return False

        state.add_url_rule(
            values["rule"],
            values["endpoint"],
            values["view_func"],
            provide_automatic_options=values["provide_automatic_options"],
            **values["options"],
        )
        return True
