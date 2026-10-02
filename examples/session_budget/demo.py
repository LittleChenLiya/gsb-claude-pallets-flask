"""Short demo of the session cookie budget policy.

Uses only the Flask test client, so all requests are local and no server
or network access is needed. Run from the repository root with:

    PYTHONPATH=src python examples/session_budget/demo.py
"""

import hashlib
import logging

from flask import Flask
from flask import session
from flask.sessions import SecureCookieSessionInterface

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)
app.secret_key = "demo-only-fake-key"
# Refuse any session whose final Set-Cookie header would exceed 512 bytes.
app.session_interface = SecureCookieSessionInterface(cookie_budget=512)


@app.get("/set/<value>")
def set_value(value):
    session["data"] = value
    return "stored\n"


@app.get("/get")
def get_value():
    return session.get("data", "<empty>") + "\n"


client = app.test_client()

# 1. A small session fits the budget and sets a cookie.
response = client.get("/set/hello")
print("small session Set-Cookie:", response.headers.get("Set-Cookie"))
print("read back:", client.get("/get").get_data(as_text=True).strip())

# 2. A large session exceeds the budget: no new cookie is sent, the
#    refusal is logged as an error, and the old cookie keeps working.
big_value = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(50))
response = client.get("/set/" + big_value)
print("oversized session Set-Cookie:", response.headers.get("Set-Cookie"))
print("old cookie still readable:", client.get("/get").get_data(as_text=True).strip())
