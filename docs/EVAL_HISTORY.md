# Eval history

One row per nightly suite run (live provider unless noted). Appended by `scripts/nightly_live.py`; full reports are in `evals/results/` (not committed).

| date (UTC) | suite | provider+config | k | pass@1 | pass^k | advisor rate | resolved | spend | capped |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 2026-10-11 03:37 | regression | anthropic+sonnet_opus_advisor | 3 | 0.93 | 0.80 | 0.00 | 0.00 | $2.85 of $6 | no |
| 2026-10-11 03:40 | capability | anthropic+sonnet_opus_advisor | 3 | 0.67 | 0.67 | 0.00 | 0.00 | $0.91 of $4 | no |
| 2026-10-12 01:42 | regression | anthropic+sonnet_opus_advisor | 3 | 1.00 | 1.00 | 0.00 | 0.00 | $2.76 of $6 | no |
| 2026-10-27 03:17 | product | claude_code | 1 | 0.50 | 0.50 | 0.50 | 1.00 | $4.77 | no |
| 2026-10-27 04:21 | product | claude_code | 1 | 0.50 | 0.50 | 0.00 | 0.00 | $2.47 | no |
| 2026-10-27 05:07 | product | claude_code | 1 | 1.00 | 1.00 | 1.00 | 1.00 | $3.51 | no |
| 2026-10-27 05:38 | product | claude_code | 1 | 0.50 | 0.50 | 0.00 | 0.00 | $3.87 | no |
