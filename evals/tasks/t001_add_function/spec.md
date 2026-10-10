# Add `slugify(text: str) -> str` to `textutils/__init__.py`

Acceptance criteria:
- [ ] Lowercases, replaces runs of non-alphanumerics with a single `-`, strips leading/trailing `-`.
- [ ] `slugify("Hello, World!") == "hello-world"`.
- [ ] Unit tests in `tests/test_slugify.py` cover the three rules.
