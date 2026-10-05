Blueprint Registration Preflight Example
=========================================

Demonstrates how to use :meth:`~flask.Flask.plan_blueprint` to statically
plan a blueprint registration and list the endpoint, URL rule, and CLI name
conflicts it would introduce, without modifying the application and without
calling any user callbacks.

See the `Flask docs`_ about blueprints for more information.

.. _Flask docs: https://flask.palletsprojects.com/blueprints/


Install
-------

.. code-block:: text

    $ python3 -m venv .venv
    $ . .venv/bin/activate
    $ pip install -e .


Run
---

.. code-block:: text

    $ python preflight.py

The script registers a blueprint, then preflights registering a second
blueprint with the same name and a third with a different name. It prints
the conflicts the preflight detects.
