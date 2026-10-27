# Todo API

Package: `todo_api` · Stack: `python` (toolchain profile)

## Vision
A small HTTP todo service with a YAML store.

## Users
- scripts and front ends that need a todo list over HTTP

## Features (build order; each criterion is a black-box test)
### P1: Health and version endpoint
- [ ] GET /health answers 200 with the JSON object {"status": "ok", "version": "0.1.0"}
- [ ] any other unknown path answers 404

### P2: Create and list todos
- [ ] POST /todos with the JSON body {"title": "Buy milk"} answers 201 with the new todo as JSON: {"id": <int>, "title": "Buy milk", "done": false}
- [ ] GET /todos answers 200 with a JSON array of every todo, including the one just created
- [ ] POST /todos with an empty or whitespace-only title answers 400
- [ ] todos are kept in a YAML file (pyyaml) named by the TODO_FILE environment variable, default .scio/sandbox/todos.yaml

## Quality bar (release gate)
- tests pass with coverage >= 80%
- lint clean
- the release gate passes on main

## Approved dependencies
- pyyaml

## Runtime
- kind: http
- module: todo_api
- args: serve --port {port}
- health: /health

Fixed contract for the p002 product eval (M11): the hidden tests in `hidden/` check every criterion over HTTP against the started server.
