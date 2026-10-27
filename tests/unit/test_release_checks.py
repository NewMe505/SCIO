"""The secret scan and the release files of the product quality bar (M11). Token-shaped test
values are assembled at runtime: a literal one in this file would trip GitHub's push
protection, and the scan must not depend on how a value was written."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scio.aci.sandbox.local import LocalSandbox
from scio.product.release_checks import (
    ALLOW_MARKER,
    RELEASE_FILES,
    RUN_END,
    RUN_START,
    entropy,
    missing_release_files,
    readme_report,
    readme_run_commands,
    release_report,
    scan_secrets,
    secrets_in,
)
from scio.product.scaffold import scaffold_for
from scio.state.models import ProductFeature, ProductSpec
from scio.toolchain import get_toolchain, python_toolchain

AWS = "AK" + "IA" + "Q3EGRZ7XN5VYT2KD"
GITHUB = "gh" + "p_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
ANTHROPIC = "sk-" + "ant-" + "api03-Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1Jh"
KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"
RANDOM = "q8Zr2Lm9Xv4Tn7Wb3Kc6Hs1"


def test_known_token_formats_are_found_by_line_without_the_value() -> None:
    text = "\n".join(
        [
            "ok = 1",
            f'AWS_KEY = "{AWS}"',
            f"token: {GITHUB}",
            f"export ANTHROPIC_API_KEY={ANTHROPIC}",
            KEY_HEADER,
            f'const apiKey = "{RANDOM}";',
        ]
    )
    found = secrets_in(text)
    assert found == [
        (2, "AWS access key id"),
        (3, "GitHub token"),
        (4, "Anthropic API key"),
        (5, "private key"),
        (6, "credential-like assignment"),
    ]
    assert all(AWS not in kind for _, kind in found)


def test_placeholders_hashes_and_allowed_fixtures_are_not_secrets() -> None:
    text = "\n".join(
        [
            'token = "replace-me-before-deploying"',  # a credential name, a readable value
            'password = "aaaaaaaaaaaaaaaaaaaa"',  # long, but no entropy
            '"integrity": "sha512-Zx8Yw7Vu6Ts5Rq4Po3Nm2Lk1JhGfEdCbA9876543210zyxwvut=="',
            f'SAMPLE_KEY = "{AWS}"  # {ALLOW_MARKER}: documented test fixture',
            "version = 0.1.0",
        ]
    )
    assert secrets_in(text) == []
    assert entropy("aaaa") == 0.0 and entropy(RANDOM) > 3.5


def _repo(tmp_path: Path, files: dict[str, str]) -> LocalSandbox:
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return LocalSandbox(tmp_path)


def test_the_scan_reads_tracked_files_only_and_the_report_fails_on_a_finding(
    tmp_path: Path,
) -> None:
    files = {name: "x\n" for name in RELEASE_FILES}
    files["app/config.py"] = f'AWS_KEY = "{AWS}"\n'
    sb = _repo(tmp_path, files)
    (tmp_path / "untracked.env").write_text(f"KEY={GITHUB}\n", encoding="utf-8")
    (tmp_path / "logo.bin").write_bytes(b"\xff\xfe\x00binary")
    subprocess.run(["git", "add", "logo.bin"], cwd=tmp_path, check=True)
    assert scan_secrets(sb) == ["app/config.py:1: AWS access key id"]
    lines, ok = release_report(sb, python_toolchain())
    assert not ok and lines[0].startswith("[secrets] FAILED: 1 likely secret(s)")
    assert lines[1] == "[release files] present: " + ", ".join(RELEASE_FILES)


def test_a_missing_release_file_fails_the_report(tmp_path: Path) -> None:
    sb = _repo(tmp_path, {"README.md": "# x\n", "LICENSE": "MIT\n"})
    assert missing_release_files(sb) == ["CHANGELOG.md", "Dockerfile"]
    lines, ok = release_report(sb, python_toolchain())
    assert not ok and lines == [
        "[secrets] clean",
        "[release files] FAILED: missing CHANGELOG.md, Dockerfile",
        f"[readme] FAILED: README.md has no run commands between {RUN_START} and {RUN_END}",
    ]


def _readme(*commands: str) -> str:
    return "# P\n\n## Run\n\n" + RUN_START + "\n```\n" + "\n".join(commands) + "\n```\n" + RUN_END


def test_the_readme_run_block_is_parsed_without_fences() -> None:
    assert readme_run_commands(_readme("python -m p --version", "python -m p list")) == [
        "python -m p --version",
        "python -m p list",
    ]
    assert readme_run_commands("# P\n\n```\npython -m p\n```\n") is None


def test_readme_commands_run_from_the_profile_words_only(tmp_path: Path) -> None:
    tc = python_toolchain()
    sb = LocalSandbox(tmp_path)
    readme = tmp_path / "README.md"
    readme.write_text(_readme('python -c "print(1)"'), encoding="utf-8")
    assert readme_report(sb, tc) == ("[readme] passed: 1 run command(s)", True)
    readme.write_text(_readme('python -c "import sys; sys.exit(3)"'), encoding="utf-8")
    text, ok = readme_report(sb, tc)
    assert not ok and text.startswith("[readme] FAILED: `python -c")
    assert "exited 3" in text
    readme.write_text(_readme("rm -rf build"), encoding="utf-8")
    text, ok = readme_report(sb, tc)
    assert not ok and "only python, pytest commands are run" in text


@pytest.mark.parametrize("stack", ["python", "node-ts"])
def test_every_scaffold_carries_the_run_block_and_its_ci_step(stack: str) -> None:
    spec = ProductSpec(
        name="shop_api",
        title="Shop",
        vision="v",
        users=["u"],
        features=[ProductFeature(id="P1", description="d", acceptance=["a"])],
        quality_bar=["q"],
        stack=stack,
    )
    files = scaffold_for(spec)
    assert readme_run_commands(files["README.md"]) == [
        get_toolchain(stack).skeleton_command("shop_api")
    ]
    steps = yaml.safe_load(files[".github/workflows/ci.yml"])["jobs"]["checks"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "README run commands")
    assert "README.md > readme-run.sh" in run and "bash -e readme-run.sh" in run
