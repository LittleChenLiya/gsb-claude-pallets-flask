# Session Cookie Budget Demo

Demonstrates the optional `cookie_budget` policy on
`SecureCookieSessionInterface`. When the final `Set-Cookie` header
(signature, attributes, and escaping included) would exceed the budget,
the cookie is explicitly refused: no truncated or oversized cookie is
sent, the client's existing cookie is left untouched, and a diagnosable
error is logged. Without a budget the interface behaves as before.

Run from the repository root (local test-client requests only):

```
PYTHONPATH=src python examples/session_budget/demo.py
```
