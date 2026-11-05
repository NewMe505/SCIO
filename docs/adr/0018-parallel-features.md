# ADR 0018: Build a milestone's independent features in parallel worktrees, merged by one integrator

- **Status:** Accepted (opt-in)
- **Date:** 2026-11-05
- **Issue:** #82
- **Supersedes:** ADR 0007 decision 1's "one feature at a time", for builds that opt in

## Context
ADR 0007 decision 1 runs a product's features one at a time through the task graph, because the product has one writer. Its stated cost: "a product build runs serially. A large product takes hours, not minutes." The graphcalc live run (issue #63) paid it: its second milestone took about two hours of SCIO compute for four features. Its later milestones (Graphs, Lists & Statistics, CAS) hold features that work in different modules. The owner asked for features to build in parallel when their files don't overlap (2026-11-05).

What "their files" are is not known before a feature runs. Each feature's own planner names its files, and S8 showed those are guesses. Nor does a file-level overlap say whether a feature needs another one's code: graphcalc's P7 used P6's history.

Measured before the design (pre-flight 8, a scratch probe on the Fake):
- Two task graphs ran in two threads at once, each in its own git worktree of one repository, sharing one SQLite checkpointer and one effect log. Wall time was 4.0 s against 3.9 s each, with no error.
- A worktree cannot check out a branch another worktree has, so a parallel feature starts from the milestone head's SHA.
- `prepare_repo` wrote `.git/info/exclude` by hand. That fails in a worktree, where `.git` is a file, and the excludes live in the common directory, outside the worktree's sandbox (I-161).
- The Fake's `ScriptedResponder` holds its current task's script, so two feature threads need one each (`PerThreadResponder`).
- A python product's environment is an interpreter that any tree can use. A node product's `node_modules` lives inside the tree.

## Decision
1. **Opt-in.** `SCIO_PARALLEL_FEATURES=N` (1 to 4) sets how many features may build at once. The default of 1 is today's serial loop, unchanged. A live run turns it on at a merge stop, never mid-milestone. Parallel builds run only on a local sandbox (the Docker backend has one container per product) and only when the product's environment is an interpreter a worktree can share (`sandbox.python`); otherwise the build runs serially and says nothing new.
2. **The plan says which features are independent.**
   - `MilestoneFeatureOutput.depends_on` lists the earlier features of the milestone that a feature needs built first.
   - None, the default and what an older plan holds, means the feature right before it, so a plan that says nothing is serial.
   - `[]` means independent. The milestone planner (prompt v2) gives it only for a feature that needs nothing from the others and works in different modules and files.
   - The normaliser keeps only earlier features of the same milestone.
   - `ProductBuildState.ready_batch(n)` takes the next unfinished feature, then each later one that has not run in this milestone and whose dependencies have finished, up to `n`. A retried feature leads a batch or runs alone.
3. **One writer per working tree, one integrator per milestone branch.**
   - Each feature of a batch runs the task graph in its own worktree under `<work dir>/worktrees/<thread>`. That is outside the product tree, so the product's `git add -A`, test discovery and lint never see it (I-155).
   - It works on its own `agent/...` branch from the milestone head's SHA, with its own thread, child budget, cost ledger, artifacts and tool context, and the shared product interpreter.
   - The orchestrator writes the repository's excludes and merge attributes once, into the common directory.
   - The orchestrator alone merges.
4. **The merge is the check.**
   - Finished features merge onto the milestone branch in plan order, each once: an effect whose probe is "the feature's head is in the milestone branch".
   - A fast-forward is the very tree the feature's verify passed. Any other merge runs the suite on the merged tree.
   - A conflict, or a suite that is not green, is undone (`merge --abort`, `reset --hard` to the head before). The run is recorded `failed` with `exit_reason: merge`, and the feature's next attempt runs from the new head.
   - CHANGELOG.md and NOTES.md, which every feature's documenter adds to, merge with git's union driver.
5. **Stops.**
   - A feature that halts gets the usual decision (retry, skip, stop), one per node run (guardrail 29): the first halted feature in plan order, after the others have merged. A halted feature after it keeps its worktree, and its paused thread is resumed by the next node run.
   - Every subgraph is checkpointed and every merge is an effect with a probe, so the node re-run that an answer starts repeats neither (I-065).
   - A harness failure in one feature ends its attempt, and the build stops after the others have merged, as a serial failure does (I-100).
   - Worktrees are removed only when the node returns, never before its stop.

## Consequences
- **Upside:** independent features take the time of the longest one, not the sum. The merge check means a wrong `depends_on` costs a retry, not a broken milestone branch.
- **Downside:**
  - N features spend their budgets at once, and on the Claude Code backend they run N sessions at once on one subscription.
  - The milestone branch gains merge commits.
  - A conflict throws away the losing feature's attempt.
  - `install_deps` in one feature installs into the environment the others share.
- **Not in scope:**
  - node products (a `node_modules` per worktree);
  - a Docker sandbox per feature;
  - predicting files at the milestone level (the merge decides).
- **Tests:** `tests/graph/test_parallel_features.py` (a disjoint pair, a textual conflict, a clean merge with a red suite, one halted while one passed, a crash mid-batch, N=1 serial); `tests/unit/test_product_progress.py` (`ready_batch`, the normaliser); `tests/unit/test_parallel_git.py` (I-161).
