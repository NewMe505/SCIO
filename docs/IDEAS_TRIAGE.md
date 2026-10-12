# Ideas triage: ROI versus premature optimization

A critical filter over thirteen proposed improvements, written on 2026-10-11 against the
live-validation data (ledger I-031 to I-039, `docs/LESSONS_LEARNED.md` "Live validation").
Each item gets a verdict, the questions that decide it, and the evidence SCIO already has.
Verdicts: **do now**, **do with a cap**, **already have it**, **premature**.

The one number that reframes half the list: in the live t005 run the coder made 6 calls
with 21,485 fresh input tokens and 5,865 cache-read tokens; cache writes on the
conversation were 0. SCIO caches the system prompt and tool list but not the growing
transcript, so each turn re-bills the whole history at the full input price. The t003
run spent 202k of 283k tokens in 31 coder calls the same way.

---

## 1. Strict SemVer cadence for 0.x
**Verdict: do now (cost ≈ 0).** Patch for fix-only/additive changes; minor for anything
that changes configs, CLI flags, state fields or schemas.

- Who consumes SCIO as a dependency today? Nobody external, so the rule buys discipline,
  not compatibility; it is still free.
- Does PR #14 meet the minor-bump test? Yes: `budgets.yaml` changed, `--parallel` was
  added, `TrialResult.notes` and `WorkerOutput.tests_added`'s schema changed. It is 0.2.0.
- Will the release notes script cope? `scripts/release_notes.py --cut` already does.

## 2. The O(N²) token scaling problem
**Verdict: do now, in two cheap steps; the architecture change is premature.**

- What fraction of a coder turn's cost is re-reading its own earlier transcript? From the
  ledger: most of it. Fresh input dominates and grows per call; output stays small.
- Is the cost quadratic or only the token count? Today both. With a cache breakpoint on the
  last message, the previous transcript is read at 0.1× (Opus 5.5: 0.05×), so cost becomes
  roughly linear while the token count stays quadratic. That is one line in the Anthropic
  adapter and is in the same PR as this document.
- After caching, what is the real per-turn cost at 12 calls? Measured on t003 in the same
  PR: coder fresh input fell from 21k to 64 tokens over 32 calls (184k served from cache,
  52k written), and the coder's cost fell from $0.46 to $0.36 for a run of the same
  length. The remaining cost is cache reads at 0.1× plus cache writes at 1.25×, so the
  curve is still quadratic in tokens but with a slope ten times flatter. Decoupled
  state-based memory for the *worker turn* only pays if that residual is still dominant,
  and it changes a design that the live runs just validated (append-only,
  preserved-thinking compatible). Measure one more time after the budget counts billable
  tokens (I-041) before touching it.
- Note: across nodes SCIO already has decoupled state memory; prompts are rebuilt from
  typed state every call. The quadratic growth is inside one worker turn only.

## 3. Diff-based code generation
**Verdict: already have it.** `edit_file` is a search/replace tool (exact `old_text` →
`new_text`, unique match, create on empty). The coder never regenerates files.

- Measure before worrying: average `new_text` size per `edit_file` call in the live
  transcripts is tens of lines, not hundreds.
- The thing to watch is the opposite failure: `old_text` not matching after a prior edit.
  The tool reports it with a hint and the live runs showed no loops on it.

## 4. State payload truncation at handoffs ("middle-out")
**Verdict: already have it, by construction.** No chat history crosses a node boundary;
`verify` hands the next turn only the head of the last failure as `[context from verify]`.

- Where would a sliding window apply? Only inside a worker turn, where `trim_messages`
  already shortens old tool results past 30k characters while keeping the last three.
- Would dropping failed middle iterations lose anything? The escalation brief carries the
  last error and the attempt count; the debugger prompt asks the model to say what it
  ruled out. Nothing else depends on middle iterations.

## 5. The rejection ledger pattern
**Verdict: premature; 80% exists.** Artifacts are already by reference (`ArtifactRef`),
the task ledger records every attempt, and only the head of the last verify output enters
a prompt. What does not exist is a *tool* for the worker to pull older artifacts.

- Has any live run needed history older than the last failure? No: every passing run
  finished in one iteration after the harness fixes; the escalation path uses the brief.
- What would the signal be? A debugger repeating a fix the ledger already recorded.
  `SweepRecord.ruled_out` is the place that would show it. Add a `history` tool only when
  that appears in a capability scenario.

## 6. Semantic state summarization through a local model
**Verdict: premature; no consumer.** The `compactor` role exists in `models.yaml` but
nothing calls it, because transcripts do not cross nodes and explorer summaries are
already structured and capped at 1.5k tokens.

- Which prompt would consume a `Current_State.md`? None today. Build the consumer need
  first (a step that provably loses context), then the summarizer.
- What does a local Ollama model cost in engineering? A new provider adapter, a new
  machine dependency for CI, and a model whose quality is unmeasured by the eval suite.
- Haiku 5.5 already fills the "fast, cheap compactor" slot at $0.10/MTok if a consumer
  appears; it needs no infrastructure.

## 7. Evaluation suite upgrades
**Verdict: do, with a cost cap.** The highest-ROI item on the list: nine defects came
out of the first live runs, none of them visible to the Fake.

- What is the nightly budget? Five trials × five tasks × three A/B configs is about $15
  to $25 per night at current costs. Set a ceiling in the workflow and run the Opus-only
  config weekly, not nightly.
- Which realism gaps matter? Multi-file changes, a failing CI workflow in the fixture
  (verify currently runs pytest, not the repo's CI), and tasks whose plan needs more than
  one step. Add one task per gap, not ten.
- Should live results gate merges? No. They are variance-bearing and cost money; the Fake
  suite gates, the live suite informs and feeds the threshold sweep.
- Re-run `evals/tune.py` live before trusting ADR 0006's thresholds (not yet possible: no
  live run has escalated, see the status note at the end); include a scenario
  where the debugger *can* succeed (ADR 0006 consequence).

## 8. Disabling native advisor tools
**Verdict: already have it.** The native advisor tool is off by default (ADR 0005 decision
4, assumption F-5); escalation is the graph-level `advise` node with a brief to Opus 5.5.
`advisor_calls_max: 0` already produces the hard halt (scenario c003); the Sonnet-only A/B
config exists.

## 9. Action completion tracking
**Verdict: mostly covered; one cheap addition.** Success is defined by hidden tests plus
`exit=success`; an agent that merely acknowledges cannot pass.

- Where can acknowledgement still pass? The Documenter: docs edits are checked only by the
  target repo's own docs check, if it has one. Add a cheap completion signal: when the
  repository has a README/CHANGELOG and the diff touches code, did the Documenter's commit
  touch docs? Report it in `scio report`; let the gate see it.
- Is an 80% threshold meaningful here? Only once there is a per-role completion metric to
  threshold; build the metric first.

## 10. Tool selection quality evaluation
**Verdict: cheap metrics now; LLM evaluators premature.** The data already exists:
`ToolBox` sees every call, every validation failure and every error result.

- Which would you act on? Error-result rate per tool, calls per successful task, repeated
  identical calls (same tool, same input). All three are a few lines in the run report.
- Has any live failure been a tool-choice failure? No; the nine were harness defects. Until
  a failure is attributable to routing, an explaining evaluator has nothing to explain.

## 11. Latency breakdown analysis
**Verdict: premature as a build; use what exists.** Spans already carry per-call latency,
usage and role; a collector (`SCIO_OTEL_EXPORTER=otlp`) renders the trace graph.

- Is latency an objective? The spec orders success, reliability, then tokens/cost. Live
  tasks take 50 to 300 s and model calls dominate by construction.
- What would change? Prompt caching (item 2) and the Haiku router are already the two
  levers the analysis would recommend.

## 12. Automated signals for edge cases
**Verdict: two cheap signals now; the rest premature.**

- "Context memory loss across handoffs" cannot occur: handoffs carry typed state, not
  memory. The analogous SCIO signal is a worker re-reading files it already read in the
  same turn; count repeated `view_file` paths per turn.
- "Inefficient tool usage": repeated identical tool calls per turn (item 10). Both are
  computable from the transcript artifacts without any model call.

## 13. Custom LLM evaluators for business outcomes
**Verdict: premature.** SCIO's outcomes are test-defined, and its one LLM judge (the gate)
is deliberately subordinate to ground truth. There is no production behaviour to judge
yet; when there is, start with the capability suite's hidden tests, not a judge.

---

## Recommended order
1. Conversation-prefix prompt caching (item 2, step one) and measure on t003 (same PR).
2. SemVer rule; tag `v0.2.0` on the live-validation merge (item 1).
3. Nightly live suite with a cost ceiling, two realistic tasks, live threshold sweep (item 7).
4. Cheap signals in `scio report`: tool error rate, repeated calls, re-reads, documenter
   completion (items 9, 10, 12). Done 2026-10-12 (issue #27): `RunReport.tool_signals`.
5. Everything else waits for a measured failure it would have prevented.

Status 2026-10-12: items 1 to 4 are done except the live threshold sweep in item 3, which
has nothing to measure yet. Across 31 live task rows (three nightly suites and the first
trials of t006 and t007) no run made an advisor call: every task passed in one or two
iterations, so every sweep cell would run identically at about $30 for the grid. The sweep
waits for a live task that makes the coder fail verify at least twice (ADR 0006,
consequences).
