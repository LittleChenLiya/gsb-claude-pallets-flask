"""Blueprint registration preflight example.

Run with ``python preflight.py``.
"""

from flask import Blueprint
from flask import Flask

app = Flask(__name__)

# A blueprint that is already registered.
existing = Blueprint("api", __name__)


@existing.route("/users")
def users():
    return "users"


app.register_blueprint(existing)

# A second blueprint with the same name -- this would conflict.
duplicate = Blueprint("api", __name__)


@duplicate.route("/items")
def items():
    return "items"


# A third blueprint with a different name -- this is legal.
other = Blueprint("other", __name__)


@other.route("/things")
def things():
    return "things"


def main() -> None:
    print("Preflighting duplicate (same name)...")
    plan = app.plan_blueprint(duplicate)
    if plan.ok:
        print("  OK")
    else:
        for conflict in plan.conflicts:
            print(f"  conflict: {conflict.kind} {conflict.name!r}")

    print("Preflighting other (different name)...")
    plan = app.plan_blueprint(other)
    if plan.ok:
        print("  OK")
        print(f"  endpoints: {plan.endpoints}")
        print(f"  rules: {plan.rules}")
    else:
        for conflict in plan.conflicts:
            print(f"  conflict: {conflict.kind} {conflict.name!r}")

    # The application was not modified by the preflight.
    print(f"Registered blueprints: {sorted(app.blueprints)}")


if __name__ == "__main__":
    main()
