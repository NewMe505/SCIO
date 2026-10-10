# Research basis

The evidence behind SCIO's design decisions, gathered in October 2026. Items marked *(industry)* come from company engineering blogs, not peer-reviewed papers. Numbers were measured on the models and benchmarks named in each source. Re-validate them on SCIO's own evals before relying on them.

## Topology: one writer, parallel only for read-only work
| Finding | Decision | Source |
|---|---|---|
| On sequential tasks, every multi-agent variant got worse (−39% to −70%). On parallel tasks, centralized coordination helped (+80.9%). Independent agents amplified errors 17.2×; centralized ones 4.4×. | One writer; fan out only read-only work; a central orchestrator checks results | Kim et al., *Towards a Science of Scaling Agent Systems* (2025) — https://arxiv.org/abs/2512.08296 · https://research.google/blog/towards-a-science-of-scaling-agent-systems-when-and-why-agent-systems-work/ |
| Parallel subagents make clashing hidden decisions. A clean-context code reviewer works well. | Code written in one thread; Reviewer gets fresh context | Cognition *(industry)* — https://cognition.ai/blog/dont-build-multi-agents · https://cognition.com/blog/multi-agents-working |
| Multi-agent setups use ~15× the tokens of chat, and token use explains ~80% of performance variance. Most coding is a poor fit for parallel agents. | Fan-out must justify its cost; effort tiers | Anthropic *(industry)* — https://www.anthropic.com/engineering/multi-agent-research-system |
| Prefer fixed workflows; add autonomy only where it measurably helps. | Mostly fixed edges | Anthropic *(industry)* — https://www.anthropic.com/engineering/building-effective-agents |
| A fixed localize → patch → validate pipeline beat open-source agents of its time at ~$0.68 per issue. | Fixed graph stages | Xia et al., *Agentless* (FSE 2025) — https://arxiv.org/abs/2407.01489 |

## Failure modes and verification
| Finding | Decision | Source |
|---|---|---|
| 14 failure modes across 1,642 traces. Most common: step repetition (15.7%), reasoning-action mismatch (13.2%), unaware of termination conditions (12.4%), incorrect verification (9.1%). A check that the high-level objective was met gave **+15.6%**. | Opus completion gate; repetition and stall detectors; explicit exit conditions | Cemri et al., *Why Do Multi-Agent LLM Systems Fail?* — https://arxiv.org/abs/2503.13657 |
| Self-correction without external feedback doesn't improve reasoning. | Tests, lint and types are ground truth | Huang et al. (2023) — https://arxiv.org/abs/2310.01798 |
| Multiagent debate improves reasoning, but at equal compute it gains little over self-consistency. | No debate nodes; sample N patches and pick by test results | Du et al. (MIT, ICML 2024) — https://arxiv.org/abs/2305.14325 · https://aclanthology.org/2026.acl-srw.1/ |
| Task and progress ledgers; replan when stalled. | stall_count in state routes to Opus replan | Microsoft Magentic-One — https://www.microsoft.com/en-us/research/articles/magentic-one-a-generalist-multi-agent-system-for-solving-complex-tasks/ |

## Tools and context
| Finding | Decision | Source |
|---|---|---|
| A purpose-built agent-computer interface beats raw shell: windowed viewer, terse search, lint-gated edits. | Build the tool interface before adding agents | Yang et al., *SWE-agent* (NeurIPS 2024) — https://arxiv.org/abs/2405.15793 |
| Consolidated tools, a concise response mode (~⅓ the tokens), actionable error messages, absolute paths. | Tool design rules | Anthropic *(industry)* — https://www.anthropic.com/engineering/writing-tools-for-agents |
| Use the smallest set of high-value tokens; load just in time; compact; summarize subagent output. | Context layer | Anthropic *(industry)* — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents |
| Rebuild context per call from durable state; static prefix first for caching. | Prompt assembly order | Google ADK — https://developers.googleblog.com/en/architecting-efficient-context-aware-multi-agent-framework-for-production/ |
| For large inputs, query the context programmatically instead of loading it all. | Tool-based repo search | Zhang, Kraska, Khattab (MIT CSAIL), *Recursive Language Models* — https://arxiv.org/abs/2512.24601 |
| Initializer writes a feature list with every item failing, a progress file and git commits; then one feature at a time. | State backbone | Anthropic *(industry)* — https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents |

## Model tiering and escalation
| Finding | Decision | Source |
|---|---|---|
| A Sonnet executor with an Opus advisor: +2.7 points on SWE-bench Multilingual, −11.9% cost. Advisor replies are 400–700 tokens; `max_uses` caps calls. (Measured on Sonnet 4.6; not a fully controlled comparison.) | Sonnet 5 worker + Opus 5.5 advisor, capped | Anthropic *(industry)* — https://claude.com/blog/the-advisor-strategy · https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool |
| Pairing a strong model with a much weaker primary model fails. | Keep the gap small (Sonnet, not Haiku, writes code) | Cognition *(industry)* |
| Cascades: escalate when a scorer rejects the cheap answer. Disagreement among cheap samples signals hard tasks. | Escalation triggers 4–5 | FrugalGPT — https://arxiv.org/abs/2305.05176 · MoT cascade — https://arxiv.org/abs/2310.03094 · RouteLLM — https://arxiv.org/abs/2406.18665 |
| Replan only when execution feedback contradicts the plan. | Advisor verdict `replan` | Plan-and-Act (ICML 2025) — https://arxiv.org/abs/2503.09572 |

## Reliability, evals, security
| Finding | Decision | Source |
|---|---|---|
| Checkpointer + thread_id make runs durable; side effects go in tasks; a resumed node re-runs from the start. | Postgres saver; idempotent @task; no side effects before `interrupt()` | https://docs.langchain.com/oss/python/langgraph/durable-execution · https://docs.langchain.com/oss/python/langgraph/interrupts |
| Structured outputs: schema limits; check `stop_reason`. | Wire schema + local validation + repair (max 2) | https://platform.claude.com/docs/en/build-with-claude/structured-outputs · https://python.useinstructor.com/concepts/validation |
| Prompt caching (reads cost 0.1×); clearing context breaks the cache. | Stable prefix; token-threshold compaction | https://platform.claude.com/docs/en/build-with-claude/prompt-caching · https://platform.claude.com/docs/en/build-with-claude/context-editing |
| OTel GenAI semantic conventions (still in Development status). | Tracing layer; pin the version | https://github.com/open-telemetry/semantic-conventions-genai |
| SWE-bench Verified is no longer a reliable signal. | Private eval suite | https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/ |
| pass^k exposes unreliability that pass@1 hides. | n≥5 trials per task | τ-bench — https://arxiv.org/abs/2406.12045 |
| Separate capability and regression evals; grade outcomes; calibrate judges. | Eval design | Anthropic *(industry)* — https://anthropic.com/engineering/demystifying-evals-for-ai-agents |
| Lethal trifecta; excessive agency. | Capability check at compile time; human gates; sandbox with no network | https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/ · https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications/ · https://arxiv.org/abs/2506.08837 |

## Open questions / unverified
- Is Opus 5.5 → Sonnet 5 a supported pairing for the native advisor tool? Check the advisor-tool docs.
- Is the Temporal LangGraph plugin production-ready? It is experimental as of this writing.
- Several numbers in secondary summaries (debate gains, RouteLLM savings, sandbox cold-start times) were not checked against primary sources.
