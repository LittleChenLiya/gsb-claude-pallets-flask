"""Short local-only demo of the strict session cookie size budget.

Run with:  python demo/session_cookie_budget_demo.py

Uses Flask's test client only -- no network requests are made.
"""

import os

from flask import Flask
from flask import session
from flask.sessions import SessionCookieTooLarge

app = Flask(__name__)
app.config["SECRET_KEY"] = "demo-secret-key"
# Strictly cap the final Set-Cookie header at 200 bytes.
app.config["SESSION_COOKIE_MAX_SIZE"] = 200
# Return a 500 response instead of propagating the error so the response
# headers can be inspected.
app.config["PROPAGATE_EXCEPTIONS"] = False


@app.get("/set-small")
def set_small():
    session["user"] = "alice"
    return "small session set"


@app.get("/set-large")
def set_large():
    # Random data does not compress, so the final header exceeds the budget.
    session["big"] = os.urandom(400).hex()
    return "large session set"


@app.get("/read")
def read():
    return dict(session)


def main():
    client = app.test_client()

    print("1. Small session (within budget):")
    response = client.get("/set-small")
    print(f"   status={response.status_code}")
    print(f"   Set-Cookie present: {'Set-Cookie' in response.headers}")

    print("\n2. Large session (exceeds budget):")
    response = client.get("/set-large")
    print(f"   status={response.status_code}")
    print(f"   Set-Cookie present: {'Set-Cookie' in response.headers}")

    print("\n3. Read session (old cookie still readable):")
    response = client.get("/read")
    print(f"   status={response.status_code}")
    print(f"   body={response.data!r}")

    print("\n4. Direct error check:")
    app.config["PROPAGATE_EXCEPTIONS"] = True
    try:
        client.get("/set-large")
    except SessionCookieTooLarge as e:
        print(f"   raised SessionCookieTooLarge: {e}")


if __name__ == "__main__":
    main()
