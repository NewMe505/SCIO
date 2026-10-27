"""The secret scan and the release files of the product quality bar (M11). Token-shaped test
values are assembled at runtime: a literal one in this file would trip GitHub's push
protection, and the scan must not depend on how a value was written."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scio.aci.sandbox.local import LocalSandbox
from scio.product.release_checks import (
    ALLOW_MARKER,
    RELEASE_FILES,
    entropy,
    missing_release_files,
    release_report,
    scan_secrets,
    secrets_in,
)

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
    lines, ok = release_report(sb)
    assert not ok and lines[0].startswith("[secrets] FAILED: 1 likely secret(s)")
    assert lines[1] == "[release files] present: " + ", ".join(RELEASE_FILES)


def test_a_missing_release_file_fails_the_report(tmp_path: Path) -> None:
    sb = _repo(tmp_path, {"README.md": "# x\n", "LICENSE": "MIT\n"})
    assert missing_release_files(sb) == ["CHANGELOG.md", "Dockerfile"]
    lines, ok = release_report(sb)
    assert not ok and lines == [
        "[secrets] clean",
        "[release files] FAILED: missing CHANGELOG.md, Dockerfile",
    ]
