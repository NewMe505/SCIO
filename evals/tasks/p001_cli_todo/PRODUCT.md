# Todo CLI

Package: `todo_cli` · Stack: `python` (toolchain profile)

## Vision
A command-line todo list with tags and JSON export, kept in one JSON file.

## Users
- people who live in a terminal

## Features (build order; each criterion is a black-box test)
### P1: Walking skeleton: the app starts, answers --version, and one smoke test passes
- [ ] `python -m todo_cli --version` prints `0.1.0` and exits 0
- [ ] the smoke test passes under the profile's test command

### P2: Add and list todos
- [ ] `python -m todo_cli add "buy milk"` prints `added 1: buy milk` and exits 0; ids count up from 1
- [ ] `python -m todo_cli list` prints one line per todo, oldest first, as `<id>. <text>`
- [ ] todos are kept in the JSON file named by the TODO_FILE environment variable (default todo.json in the working directory), so they survive between runs

### P3: Tags and JSON export
- [ ] `python -m todo_cli add "pay rent" --tag home --tag urgent` stores both tags, and `list` ends that todo's line with ` #home #urgent`
- [ ] `python -m todo_cli export --json` prints a JSON array of objects with `id`, `text` and `tags` (a list of strings) and exits 0

## Quality bar (release gate)
- tests pass with coverage >= 80%
- lint clean
- the release gate passes on main

## Approved dependencies
- none beyond the standard library

## Runtime
- kind: cli
- module: todo_cli
- args: (none)
- health: /health

Fixed contract for the p001 product eval (M11): the hidden tests in `hidden/` check every criterion from outside.
