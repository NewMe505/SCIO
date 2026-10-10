# Let the greeting CLI emit JSON

Users want machine-readable output from the command line tool, but the tool's
entry point and argument handling are not documented. Find them and add a flag.

Acceptance criteria:
- [ ] `tool.cli.main(['--json', 'Ada'])` prints a JSON object with keys `name` and `greeting`
- [ ] Without the flag the existing plain-text behaviour is unchanged
