# Add a population standard deviation to metrics.stats

`metrics.stats` offers `mean` and `median`. Add `stdev(values)`, the population
standard deviation (divide by `n`, not `n - 1`), which raises `ValueError` on an
empty sequence like the other helpers do.

Acceptance criteria:
- [ ] `stdev([2, 4, 4, 4, 5, 5, 7, 9]) == 2.0`, and `stdev([])` raises `ValueError`
- [ ] `stdev` is covered by tests in `tests/test_stats.py`
- [ ] The repository's CI workflow (`ruff check .` with the repository configuration, then `pytest`) passes
