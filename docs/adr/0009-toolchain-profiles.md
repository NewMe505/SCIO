# ADR 0009: Toolchain profiles put every stack-specific command behind one protocol

- **Status:** Accepted
- **Date:** 2026-10-12
- **Milestone:** M6 Foundations (Product Mode, ADR 0007)

## Context
Through 0.4.0 the task graph assumed Python everywhere: `run_tests` built a `python -m pytest` argv, `edit_file` and `verify` ran `ruff check --select E9,F63,F7,F82,F821`, the error signature parsed Python tracebacks, test paths were `tests/` and `test_*.py`, cache excludes were `__pycache__` and friends, and the prompts told the models to report "pytest node ids or -k keywords". Product Mode needs the same graph to build in other stacks (M10 adds `node-ts`), and the survey in `docs/PRODUCT_MODE.md` listed seven modules with these literals. A research pass before this milestone found every occurrence (issue #39): two command builders, one lint builder, one script runner, five prompt strings, two file-count helpers, the signature parser, the drift and impact file rules, and two cache lists.

## Decision
1. **One protocol, `scio.toolchain.Toolchain`.** It names what the graph needs and nothing more: source and test path rules, cache excludes and dependency files, the test command and the meaning of its exit codes, failure-only filtering, path-selector semantics, the lint command and how to read its exit codes, the script runner, the error-signature parser, the removed-public-symbol detector, and the prompt phrases that tell a model how to name tests. Profiles are frozen dataclasses; the protocol exposes read-only properties.
2. **Data in `configs/profiles/<name>.yaml`, parsers in code.** The `python` profile's YAML holds the exact pre-M6 argv, exit codes, excludes and phrases; `scio/toolchain/python.py` holds the pytest output filter, the traceback signature and the `def`/`class` regex. A new stack is a YAML file plus a small parser module.
3. **No behaviour change for Python.** `tests/unit/test_toolchain.py` asserts the profile reproduces the old literals byte for byte (test argv, lint argv, the `.git/info/exclude` text, the cache tuple), and the regression suite must stay at pass^5 = 1.00 with no fixture or script change.
4. **The graph never names a tool.** Every call site takes the toolchain from `RunContext.toolchain` (set from `Settings.toolchain`, default `python`); module-level functions used by tests (`run_tests`, `edit_file`, `assess`, `is_path_selector`, `advance`) accept an optional toolchain and default to the Python profile. `test_no_stack_specific_command_outside_the_toolchain_package` scans `scio/` for command tokens (`"pytest",`, `"ruff",`, `"npm",` ...) and cache literals outside `scio/toolchain/` and fails on any hit.
5. **Prompt text comes from the profile too.** `selector_help` ("pytest node ids like tests/test_x.py::test_name, or -k keywords") and `filter_help` are profile data, passed into the coder, planner and step projections and verify's messages, so a `node-ts` profile can say "vitest -t names" without touching the graph.

## Consequences
- The sandbox keeps two Python-interpreter facts that are about SCIO's own runtime, not the target stack: `LocalSandbox` rewrites argv `python` to `sys.executable`, and sets `PYTHONUTF8=1` and `PIP_NO_INDEX=1` offline. They stay where they are; a product build on another stack still runs SCIO's helper scripts with Python.
- `evals/solvers.CACHE_PATTERNS` and `scio/loop/impact.DEPENDENCY_FILES` remain as names (tests import them) but are now read from the profile.
- Detection of a repository's stack was M10's job and is done (ADR 0014): `SCIO_TOOLCHAIN` accepts `auto` (the default, detection from marker files), `python` and `node-ts`.
- Supersedes assumption F-8 ("target repositories are Python"): targets are whatever a profile exists for. PROMPT.md's non-goal "No UI" is narrowed to SCIO itself (ADR 0007).
