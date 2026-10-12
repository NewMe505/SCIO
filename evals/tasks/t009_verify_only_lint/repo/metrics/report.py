"""One-line summaries for the CLI and the notebooks."""

from metrics.stats import mean, median


def format_report(values):
    """``mean=<m> median=<md>``, both rounded to ``metrics.stats.DECIMALS`` places."""
    return f"mean={round(mean(values), DECIMALS)} median={round(median(values), DECIMALS)}"
