# Make CI green again

The `ci` workflow in `.github/workflows/ci.yml` fails on `main`. Both of its
check steps are red: `ruff check .` with the repository's own configuration in
`pyproject.toml`, and `pytest`. Fix the code so the workflow passes. Do not edit
the workflow or the ruff configuration to make the failures go away.

Acceptance criteria:
- [ ] `ruff check .` reports no violations under the rules selected in `pyproject.toml`
- [ ] `tests/test_ledger.py` passes: `Account.withdraw` reduces the balance
- [ ] `.github/workflows/ci.yml` and `pyproject.toml` are unchanged
