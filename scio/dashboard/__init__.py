"""The live, read-only dashboard (``scio dashboard``, issue #68, ADR 0017).

:mod:`scio.dashboard.contract` is what every layer speaks; :mod:`scio.dashboard.data` reads the
workspaces' checkpoint DBs, cost ledgers and ``progress.json`` without writing anything;
:mod:`scio.dashboard.app` serves them as JSON and a WebSocket; ``static/`` is the page. The
server needs the optional ``dashboard`` extra (FastAPI and uvicorn); the contract and the data
layer need only SCIO's core dependencies.
"""
