# Todo Web

Package: `todo_web` · Stack: `python` (toolchain profile)

## Vision
A todo list in the browser: one page with the list and an add form, kept in one JSON file.

## Users
- people who keep a short todo list open in a browser tab

## Features (build order; each criterion is a black-box test)
### P1: Todo page lists todos
- [ ] GET / answers 200 with an HTML page whose heading is "Todos", with a form (a text input named title and a button labelled Add) and a list with id todos holding one li per todo, oldest first
- [ ] GET /health answers 200 with the JSON object {"status": "ok", "version": "0.1.0"}; any other unknown path answers 404
- [ ] todos are kept in the JSON file named by the TODO_FILE environment variable, default .scio/sandbox/todos.json

### P2: Add todos from the page
- [ ] submitting the form posts title to /todos (application/x-www-form-urlencoded); the todo is stored and the server answers 303 back to /, where the new todo is the last li of the list
- [ ] an empty or whitespace-only title answers 400 with the page showing "title is required" in an element with class error, and adds nothing

## Quality bar (release gate)
- tests pass with coverage >= 80%
- lint clean
- the release gate passes on main

## Approved dependencies
- none beyond the standard library

## Runtime
- kind: http
- module: todo_web
- args: serve --port {port}
- health: /health

Fixed contract for the p003 product eval (M11): the hidden tests in `hidden/` drive the page in a real headless browser (Playwright, Chromium).
