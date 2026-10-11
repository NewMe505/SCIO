"""The five ACI tools. Each takes a sandbox and a validated input model and returns text.

Design rules (Anthropic tool-writing guidance + SWE-agent): consolidated tools, a concise
mode, actionable error messages, absolute paths, bounded output with a hint.
"""

from __future__ import annotations

import ast
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from scio.aci.sandbox.base import PathEscapeError, Sandbox
from scio.aci.truncate import truncate

VIEW_WINDOW = 100


class ToolError(Exception):
    """A tool failure the model should see (returned as an error tool_result)."""


# ---------- inputs ----------


class RepoSearchInput(BaseModel):
    query: str = Field(description="Regular expression to search for")
    mode: Literal["concise", "detailed"] = Field(
        default="concise",
        description=(
            "concise: files and line numbers only (about 1/3 the tokens); detailed: matching lines"
        ),
    )
    path: str | None = Field(
        default=None, description="Absolute directory or file to search; default: repo root"
    )
    glob: str | None = Field(default=None, description="Only files matching this glob, e.g. '*.py'")
    max_results: int = Field(default=50, ge=1, le=500)


class ViewFileInput(BaseModel):
    path: str = Field(description="Absolute path of the file")
    start_line: int = Field(default=1, ge=1, description="First line of the 100-line window")


class EditFileInput(BaseModel):
    path: str = Field(description="Absolute path of the file to edit or create")
    old_text: str = Field(
        description="Exact text to replace (must match once). Empty string creates a new file"
    )
    new_text: str = Field(description="Replacement text, or the full content of a new file")


class RunTestsInput(BaseModel):
    filter: str | None = Field(
        default=None, description="pytest -k expression; narrow to the feature under work"
    )
    path: str | None = Field(
        default=None, description="Absolute test file or directory; default: whole repo"
    )
    timeout_s: int | None = Field(default=None, ge=1, le=1800)


class GitCheckpointInput(BaseModel):
    message: str = Field(description="Conventional Commit message, e.g. 'feat: add slugify (F1)'")


# ---------- helpers ----------


def _abs(sandbox: Sandbox, path: str | None) -> str:
    if path is None:
        return sandbox.root
    try:
        return sandbox.resolve(path)
    except PathEscapeError as exc:
        raise ToolError(f"{exc}. Use absolute paths under {sandbox.root}") from exc


def _host_root(sandbox: Sandbox) -> Path | None:
    hr = getattr(sandbox, "host_root", None)
    return Path(hr) if hr is not None else None


# ---------- repo_search ----------


def _python_search(
    host_root: Path, sandbox_root: str, query: str, path: str, glob: str | None, limit: int
) -> list[tuple[str, int, str]]:
    rx = re.compile(query)
    rel = path.removeprefix(sandbox_root).lstrip("/\\")
    base = host_root / rel if rel else host_root
    files = [base] if base.is_file() else sorted(p for p in base.rglob(glob or "*") if p.is_file())
    hits: list[tuple[str, int, str]] = []
    for f in files:
        if ".git" in f.parts:
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                hits.append((f"{sandbox_root}/{f.relative_to(host_root).as_posix()}", i, line))
                if len(hits) >= limit:
                    return hits
    return hits


def repo_search(sandbox: Sandbox, inp: RepoSearchInput) -> str:
    base = _abs(sandbox, inp.path)
    limit = inp.max_results
    hits: list[tuple[str, int, str]] = []
    argv = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-count", str(limit)]
    if inp.glob:
        argv += ["--glob", inp.glob]
    argv += ["-e", inp.query, base]
    r = sandbox.exec(argv, timeout_s=60)
    if r.exit_code in (0, 1):  # 1 = no matches
        for line in r.stdout.splitlines():
            m = re.match(r"^(?P<file>.+?):(?P<line>\d+):(?P<text>.*)$", line)
            if m:
                hits.append((m.group("file"), int(m.group("line")), m.group("text")))
    elif r.exit_code == 2 and "regex parse error" in r.stderr:
        raise ToolError(f"invalid regular expression: {r.stderr.strip()[:200]}")
    else:
        host = _host_root(sandbox)
        if host is None or (shutil.which("rg") is not None and r.exit_code != 127):
            raise ToolError(f"search failed (exit {r.exit_code}): {r.stderr.strip()[:300]}")
        try:
            hits = _python_search(host, sandbox.root, inp.query, base, inp.glob, limit)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from exc
    hits = hits[:limit]
    if not hits:
        return f"no matches for {inp.query!r} under {base}"
    if inp.mode == "concise":
        by_file: dict[str, list[int]] = defaultdict(list)
        for f, n, _ in hits:
            by_file[f].append(n)
        body = "\n".join(f"{f}: lines {', '.join(map(str, ns))}" for f, ns in by_file.items())
    else:
        body = "\n".join(f"{f}:{n}: {t.strip()[:200]}" for f, n, t in hits)
    out = f"{len(hits)} match(es){' (capped)' if len(hits) >= limit else ''}:\n{body}"
    return truncate(out, hint="add path= or glob=, use mode='concise', or a more specific query")


# ---------- view_file ----------


def view_file(sandbox: Sandbox, inp: ViewFileInput) -> str:
    path = _abs(sandbox, inp.path)
    if not sandbox.exists(path):
        raise ToolError(f"file not found: {path}. Use repo_search to locate it")
    try:
        lines = sandbox.read_text(path).splitlines()
    except UnicodeDecodeError as exc:
        raise ToolError(f"not a text file: {path}") from exc
    except OSError as exc:  # directories raise IsADirectoryError or PermissionError (I-032)
        raise ToolError(
            f"not a readable file: {path} ({type(exc).__name__}); "
            "pass a file path, or use repo_search to list matches in a directory"
        ) from exc
    total = len(lines)
    if total == 0:
        return f"{path}: empty file"
    start = inp.start_line
    if start > total:
        raise ToolError(f"start_line {start} is past the end ({total} lines)")
    end = min(start + VIEW_WINDOW - 1, total)
    width = len(str(end))
    body = "\n".join(f"{i:>{width}}| {lines[i - 1]}" for i in range(start, end + 1))
    footer = f"[{path}: lines {start}-{end} of {total}"
    footer += f"; next: start_line={end + 1}]" if end < total else "; end of file]"
    return f"{body}\n{footer}"


# ---------- edit_file ----------


def _check_python(content: str, path: str) -> None:
    try:
        ast.parse(content, filename=path)
    except SyntaxError as exc:
        raise ToolError(f"edit rejected: syntax error at {path}:{exc.lineno}: {exc.msg}") from exc


def _lint(sandbox: Sandbox, path: str) -> str | None:
    """Return lint output if ruff finds errors; None if clean or ruff is unavailable."""
    r = sandbox.exec(
        [
            "ruff",
            "check",
            "--no-cache",
            "--select",
            "E9,F63,F7,F82,F821",
            "--output-format",
            "concise",
            path,
        ],
        timeout_s=60,
    )
    if r.exit_code == 0 or r.exit_code == 127:
        return None
    if r.exit_code == 1:
        return r.stdout.strip() or r.stderr.strip()
    return None  # ruff misconfiguration is not the agent's problem


def edit_file(sandbox: Sandbox, inp: EditFileInput) -> str:
    path = _abs(sandbox, inp.path)
    exists = sandbox.exists(path)
    if inp.old_text == "":
        if exists:
            raise ToolError(f"{path} exists; pass old_text to edit it, or choose a new path")
        before, after = None, inp.new_text
    else:
        if not exists:
            raise ToolError(f"file not found: {path}")
        before = sandbox.read_text(path)
        n = before.count(inp.old_text)
        if n == 0:
            raise ToolError("old_text not found; view_file the current content and copy it exactly")
        if n > 1:
            raise ToolError(
                f"old_text is ambiguous ({n} matches); include more surrounding context"
            )
        after = before.replace(inp.old_text, inp.new_text, 1)
    if path.endswith(".py"):
        _check_python(after, path)
    sandbox.write_text(path, after)
    if path.endswith(".py"):
        problems = _lint(sandbox, path)
        if problems:
            if before is None:
                sandbox.write_text(path, "")  # cannot delete through the protocol; leave empty
            else:
                sandbox.write_text(path, before)
            shown = truncate(problems, max_chars=1500, hint="fix the first error")
            raise ToolError(f"edit rejected by lint (reverted):\n{shown}")
    added = after.count("\n") - (before.count("\n") if before else 0)
    verb = "created" if before is None else "edited"
    return f"{verb} {path} ({added:+d} lines)"


# ---------- run_tests ----------

_FAIL_SECTION_RE = re.compile(r"^=+ (FAILURES|ERRORS) =+$", re.MULTILINE)
_SUMMARY_RE = re.compile(r"^=+ .*(passed|failed|error|no tests ran).* =+$", re.MULTILINE)


def failures_only(output: str) -> str:
    """Keep the FAILURES/ERRORS sections and the summary line; drop passing noise."""
    m = _FAIL_SECTION_RE.search(output)
    body = output[m.start() :] if m else output
    lines = [ln for ln in body.splitlines() if " PASSED" not in ln and not ln.startswith("PASSED")]
    return "\n".join(lines).strip()


def run_tests(sandbox: Sandbox, inp: RunTestsInput) -> str:
    argv = [
        "python",
        "-m",
        "pytest",
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        "-rfE",
        "--no-header",
    ]
    if inp.filter:
        argv += ["-k", inp.filter]
    if inp.path:
        argv.append(_abs(sandbox, inp.path))
    timeout = inp.timeout_s or sandbox.limits.timeout_s
    r = sandbox.exec(argv, timeout_s=timeout)
    if r.timed_out:
        raise ToolError(f"tests timed out after {timeout}s; narrow with filter= or path=")
    summary = _SUMMARY_RE.findall(r.stdout)
    last_line = next((ln for ln in reversed(r.stdout.splitlines()) if ln.strip()), "")
    if r.exit_code == 0:
        return f"all tests passed: {last_line}"
    if r.exit_code == 5:
        scope = f" for filter {inp.filter!r}" if inp.filter else ""
        return f"no tests collected{scope}: {last_line}"
    if r.exit_code in (2, 3, 4) and not summary:
        shown = truncate(r.output, max_chars=2000, hint="fix collection errors first")
        raise ToolError(f"pytest could not run (exit {r.exit_code}):\n{shown}")
    body = failures_only(r.stdout) or r.output
    return truncate(body, hint="use filter= to run only the failing test(s)")


# ---------- git_checkpoint ----------


def _git(sandbox: Sandbox, *args: str, timeout_s: int = 60) -> str:
    r = sandbox.exec(["git", *args], timeout_s=timeout_s)
    if not r.ok:
        raise ToolError(
            f"git {' '.join(args)} failed: {r.stderr.strip()[:300] or r.stdout.strip()[:300]}"
        )
    return r.stdout.strip()


def head_sha(sandbox: Sandbox) -> str:
    return _git(sandbox, "rev-parse", "HEAD")


def find_commit(sandbox: Sandbox, marker: str) -> str | None:
    """SHA of the most recent commit whose message contains ``marker``, if any."""
    r = sandbox.exec(
        ["git", "log", "--all", "-n", "1", "--format=%H", f"--grep={marker}", "--fixed-strings"],
        timeout_s=60,
    )
    sha = r.stdout.strip()
    return sha or None


def git_checkpoint(sandbox: Sandbox, inp: GitCheckpointInput) -> str:
    """Stage everything and commit. Returns the commit SHA (existing HEAD if nothing changed)."""
    _git(sandbox, "add", "-A")
    status = _git(sandbox, "status", "--porcelain")
    if not status:
        return f"nothing to commit; HEAD is {head_sha(sandbox)}"
    _git(
        sandbox,
        "-c",
        "user.name=scio",
        "-c",
        "user.email=scio@users.noreply.github.com",
        "commit",
        "-q",
        "-m",
        inp.message,
    )
    return f"committed {head_sha(sandbox)}"


def git_push(sandbox: Sandbox, branch: str, *, default_branch: str) -> str:
    """Harness-only (not exposed to the model). Never the default branch, never forced."""
    if branch == default_branch:
        raise ToolError(f"refusing to push to the default branch {default_branch!r}")
    return _git(sandbox, "push", "-u", "origin", f"{branch}:{branch}", timeout_s=180)
