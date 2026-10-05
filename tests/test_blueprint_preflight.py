"""Tests for explicit blueprint registration preflight.

Run only these tests locally with:

    python -m pytest tests/test_blueprint_preflight.py -q
"""

from __future__ import annotations

import pytest

import flask
from flask import Blueprint


def _app_snapshot(app: flask.Flask) -> dict:
    return {
        "url_map": sorted(r.rule for r in app.url_map.iter_rules()),
        "view_functions": sorted(app.view_functions),
        "blueprints": sorted(app.blueprints),
        "cli": sorted(app.cli.commands),
    }


def test_same_registration_name_conflicts() -> None:
    """Two blueprints registered under the same name conflict."""
    app = flask.Flask(__name__)
    bp1 = Blueprint("bp", __name__)
    bp2 = Blueprint("bp", __name__)

    @bp1.route("/one")
    def one() -> str:
        return "one"

    @bp2.route("/two")
    def two() -> str:
        return "two"

    app.register_blueprint(bp1)
    before = _app_snapshot(app)

    plan = app.preflight_blueprint(bp2)

    assert not plan.ok
    assert any("name 'bp' is already registered" in c for c in plan.all_conflicts)
    assert _app_snapshot(app) == before


def test_same_blueprint_different_name_passes() -> None:
    """Mounting the same blueprint again under a different name (and
    prefix) is legal and produces a clean plan."""
    app = flask.Flask(__name__)
    bp = Blueprint("bp", __name__)

    @bp.route("/hello")
    def hello() -> str:
        return "hello"

    app.register_blueprint(bp, name="first", url_prefix="/first")
    before = _app_snapshot(app)

    plan = app.preflight_blueprint(bp, name="second", url_prefix="/second")

    assert plan.ok, plan.all_conflicts
    assert plan.name == "second"
    assert plan.endpoints == ["second.hello"]
    assert plan.url_rules == ["/second/hello"]
    assert _app_snapshot(app) == before

    # The plan is accurate: registering for real afterwards works.
    app.register_blueprint(bp, name="second", url_prefix="/second")
    assert "second.hello" in app.view_functions


def test_preflight_has_no_side_effects() -> None:
    """Preflight does not call deferred functions or modify the app."""
    app = flask.Flask(__name__)
    bp = Blueprint("bp", __name__, url_prefix="/bp")
    calls = []

    @bp.route("/x")
    def x() -> str:
        return "x"

    @bp.record
    def record_callback(state) -> None:
        calls.append(state)

    before = _app_snapshot(app)
    plan = app.preflight_blueprint(bp)

    assert calls == []
    assert len(plan.unknown_deferred) == 1
    assert "record_callback" in plan.unknown_deferred[0]
    assert plan.endpoints == ["bp.x"]
    assert plan.url_rules == ["/bp/x"]
    assert plan.ok
    assert _app_snapshot(app) == before


def test_view_function_overwrite_detected() -> None:
    """An endpoint that would overwrite a different existing view
    function is reported."""
    app = flask.Flask(__name__)
    bp1 = Blueprint("bp", __name__)
    bp2 = Blueprint("bp2", __name__)

    @bp1.route("/a")
    def view() -> str:
        return "a"

    @bp2.route("/b")
    def view() -> str:  # noqa: F811
        return "b"

    app.register_blueprint(bp1)
    before = _app_snapshot(app)

    # bp2 registered under bp1's name would map endpoint "bp.view" to a
    # different function.
    plan = app.preflight_blueprint(bp2, name="bp")

    assert not plan.ok
    assert any("name 'bp' is already registered" in c for c in plan.all_conflicts)
    assert any(
        "would overwrite the existing endpoint function: bp.view" in c
        for c in plan.all_conflicts
    )
    assert _app_snapshot(app) == before


def test_url_rule_conflict_detected() -> None:
    app = flask.Flask(__name__)

    @app.route("/dup")
    def dup() -> str:
        return "dup"

    bp = Blueprint("bp", __name__)

    @bp.route("/dup")
    def bp_dup() -> str:
        return "bp dup"

    before = _app_snapshot(app)
    plan = app.preflight_blueprint(bp)

    assert not plan.ok
    assert any("URL rule '/dup' already exists" in c for c in plan.all_conflicts)
    assert _app_snapshot(app) == before


def test_cli_name_conflict_detected() -> None:
    """CLI group/command names introduced by a blueprint are checked."""
    app = flask.Flask(__name__)
    bp1 = Blueprint("bp", __name__)
    bp2 = Blueprint("other", __name__)

    @bp1.cli.command("cmd")
    def cmd1() -> None:
        pass

    @bp2.cli.command("cmd")
    def cmd2() -> None:
        pass

    app.register_blueprint(bp1)
    assert "bp" in app.cli.commands
    before = _app_snapshot(app)

    # Default cli_group: the group takes the registered name.
    plan = app.preflight_blueprint(bp2, name="bp")
    assert not plan.ok
    assert any("CLI command 'bp' already exists" in c for c in plan.all_conflicts)

    # Explicit cli_group colliding with the existing group.
    plan2 = app.preflight_blueprint(bp2, cli_group="bp")
    assert not plan2.ok
    assert plan2.cli_names == ["bp"]
    assert any("CLI command 'bp' already exists" in c for c in plan2.all_conflicts)

    # A distinct name and group is fine.
    plan3 = app.preflight_blueprint(bp2)
    assert plan3.ok, plan3.all_conflicts
    assert plan3.cli_names == ["other"]

    assert _app_snapshot(app) == before


def test_nested_blueprint_name_prefixes() -> None:
    """Nested blueprints use the real dotted name prefix rules."""
    app = flask.Flask(__name__)
    child = Blueprint("child", __name__, url_prefix="/child")
    parent = Blueprint("parent", __name__, url_prefix="/parent")

    @child.route("/leaf")
    def leaf() -> str:
        return "leaf"

    parent.register_blueprint(child)

    plan = app.preflight_blueprint(parent)

    assert plan.ok, plan.all_conflicts
    assert len(plan.children) == 1
    child_plan = plan.children[0]
    assert child_plan.name == "parent.child"
    assert child_plan.endpoints == ["parent.child.leaf"]
    assert child_plan.url_rules == ["/parent/child/leaf"]

    # And the plan matches real registration.
    app.register_blueprint(parent)
    assert "parent.child.leaf" in app.view_functions


def test_nested_duplicate_name_conflicts() -> None:
    """Two nested blueprints resolving to the same dotted name conflict
    within a single registration."""
    app = flask.Flask(__name__)
    child1 = Blueprint("child", __name__)
    child2 = Blueprint("child", __name__)
    parent = Blueprint("parent", __name__)
    parent.register_blueprint(child1)
    parent.register_blueprint(child2)

    plan = app.preflight_blueprint(parent)

    assert not plan.ok
    assert any(
        "'parent.child' is introduced more than once" in c for c in plan.all_conflicts
    )


def test_normal_register_blueprint_unchanged() -> None:
    """register_blueprint itself behaves exactly as before."""
    app = flask.Flask(__name__)
    bp = Blueprint("bp", __name__, url_prefix="/bp")
    calls = []

    @bp.route("/x")
    def x() -> str:
        return "x"

    @bp.record
    def record_callback(state) -> None:
        calls.append(state)

    app.register_blueprint(bp)
    assert len(calls) == 1
    assert "bp.x" in app.view_functions
    assert any(r.rule == "/bp/x" for r in app.url_map.iter_rules())

    # Same name twice is still an error.
    with pytest.raises(ValueError, match="already registered"):
        app.register_blueprint(Blueprint("bp", __name__))
