import subprocess
import sys
from pathlib import Path

import pytest
from metrics.stats import stdev

ROOT = Path(__file__).resolve().parent.parent


def test_hidden_population_stdev():
    assert stdev([2, 4, 4, 4, 5, 5, 7, 9]) == 2.0
    assert stdev([5]) == 0.0
    with pytest.raises(ValueError):
        stdev([])


def test_hidden_ruff_clean_with_repo_config():
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_hidden_report_module_fixed_not_removed():
    """The undefined name in metrics/report.py is fixed by wiring DECIMALS, not by deleting
    the module or the rounding."""
    from metrics.report import format_report

    assert format_report([1, 2, 4]) == "mean=2.33 median=2.0"
    assert (ROOT / "metrics" / "report.py").exists()


def test_hidden_workflow_and_config_unchanged():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "- run: ruff check ." in workflow and "- run: pytest -q" in workflow
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'select = ["F"]' in config
