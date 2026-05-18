# Team of Agents — compiler-followups-v1

## §1 Role catalogue

| ID | Role | Model | Tool allowlist | Scope | Owns rows |
|---|---|---|---|---|---|
| **R0** | Orchestrator | Opus (this session) | All | Dispatch, merge, codesweep, user-facing checkpoints | — |
| **R1** | Polish implementer (T0) | Opus | Worktree-scoped Edit/Write/Bash/Read | log rotation; section-name; cost-budget; timespan; flock CP; regex refinement | T0.A.* T0.B.* T0.D.* T0.E.* T0.F.* (T0.C.* gated) |
| **R2** | Provenance implementer (T1) | Opus | Worktree-scoped Edit/Write/Bash/Read | `evidence:` schema design; compile.py emit; backfill script | T1.A.* T1.B.* T1.C.* |
| **R3** | Connections implementer (T2) | Opus | Worktree-scoped Edit/Write/Bash/Read | compile.py 2nd-order synthesis; empty-dir guard fix | T2.A.* T2.B.* |
| **R4** | QA-loop implementer (T3) | Opus | Worktree-scoped Edit/Write/Bash/Read | hook query.py from SessionStart; route to knowledge/qa/ | T3.A.* T3.B.* |
| **R5** | Lint pre-pass implementer (T4) | Opus | Worktree-scoped Edit/Write/Bash/Read | tools/lint_kb.py: frontmatter validation, dead-link detection, orphan detection | T4.A.* T4.B.* T4.C.* |
| **R6** | Integrator (per implementer) | Sonnet | Worktree-scoped Bash/Read + worktree-finish | run `tools/worktree-finish.sh <slug>`; resolve forward-merge conflicts inline; retry transient flakes | (no checklist rows; integration only) |
| **R7** | Adversary-on-tap | Opus, ephemeral | Read + Bash (grep/pytest) on integrated commit | post-merge review per integration commit; PASS / PASS-WITH-NOTES / FAIL | (no checklist rows; verdicts in `review/`) |
| **R-FINAL** | Acceptance-test author | Sonnet | Worktree-scoped Write+Edit on `tools/`; Read on `scripts/`; Bash for verifying | authors `tools/check_followups_v1.py` (with per-check subcommands) + dogfood orchestration scaffolding | T-FINAL.1, T-FINAL.2, T-FINAL.3, T-FINAL.4, T-FINAL.5 |

**No Researcher.** Scope is fully internal — no external libs or unfamiliar domains.
**No QA tier.** All tests are unit-level + dogfood. No real-LLM acceptance run.
**No Sweep tracker.** codesweep tools owned by Orchestrator directly (see §3).
**Why R-FINAL is separate from R0:** Orchestrator (R0) is dispatch + checkpoint duty; per autonomous-sprint skill anti-patterns, R0 must NOT author code. T-FINAL.6 (dogfood operator signoff) stays with R0 (it's the CHECKPOINT-D conversation, not a code edit).

## §2 Communication protocol

### Workspace layout

```
docs/plans/compiler-followups-v1/
├── 00-problem-brief.md         (this sprint's brief)
├── team-of-agents-v1.md        (this file)
├── COMPLETION-CHECKLIST.md     (next file to write)
├── handshake/
│   └── <track>-<topic>.md      (cross-track signaling)
├── status/
│   ├── budget-meter.json       (per-dispatch costs; updated by integrator)
│   └── wave-plan.md            (orchestrator's running wave plan)
├── review/
│   └── T<N>-r<wave>-aot.md     (Adversary-on-tap verdicts)
└── followups.md                (items surfaced during execution; user-approved before sweep)
```

### Handshake protocol

When T1 (provenance) finalizes the `evidence:` schema, T3 (QA loop) consumes it for citation output. T2 (connections governance) does NOT consume the schema — its work is prompt-level guard rails.

T1 writes `handshake/t1-evidence-schema.md`:
```yaml
status: ready
rows_satisfied: [T1.A.1, T1.A.2, T1.A.3]
schema_doc_path: docs/plans/compiler-followups-v1/evidence-schema.md
emit_function: scripts.compile._emit_evidence_block
backfill_function: scripts.backfill_evidence.add_to_article
```

T3 reads this file before claiming rows that link citations into the answer schema. Orchestrator coordinates timing via wave-plan.

**Prompt-edit interleave (R4 callout per adversarial review).** R2 (T1.B in Wave 1) and R3 (T2.A in Wave 0) both modify `scripts/compile.py`'s prompt text. Integrator R6 forward-merges Wave 0 (T2) first, then Wave 1 (T1) layered on top. Conflict-resolution preference: T2 lands as a `## Cross-concept connections` subsection inside the prompt; T1 adds its `evidence:` instruction as a separate `## Evidence block` section. Re-run `pytest scripts/test_classifier.py scripts/test_dedup.py` after each integration.

### Worktree naming

`feature/followups-v1-t<N>-<slug>` (e.g., `feature/followups-v1-t1-provenance-schema`).

## §3 Codesweep configuration

```python
codesweep_create(
    name="compiler-followups-v1-acceptance",
    description="All COMPLETION-CHECKLIST.md rows for the Slice 2 polish + structural gaps sprint.",
)

# Tag rows by track for slice-and-dice:
codesweep_add(
    sweep_ref="compiler-followups-v1-acceptance",
    items=["T0.A.1", "T0.A.2", ..., "T-FINAL"],
    tags=["blocking"],  # all blocking by default; refinement tags below
)
# Then per-track tag application:
codesweep_add_tags(sweep_ref=..., items=["T0.*"], tags=["t0-polish"])
codesweep_add_tags(sweep_ref=..., items=["T1.*"], tags=["t1-provenance"])
codesweep_add_tags(sweep_ref=..., items=["T2.*"], tags=["t2-connections"])
codesweep_add_tags(sweep_ref=..., items=["T3.*"], tags=["t3-qa-loop"])
codesweep_add_tags(sweep_ref=..., items=["T4.*"], tags=["t4-lint-prepass"])
```

After each integration commit, the Adversary-on-tap (or Orchestrator if AoT is unanimous-PASS-PASS-PASS) marks rows processed.

## §4 Drift detectors

| Detector | Trigger | Action on red |
|---|---|---|
| Pre-commit ruff/mypy/pytest | Worker's first commit (per task template) | Block commit; worker fixes |
| Adversary-on-tap | Per integrator merge | FAIL → revert + re-dispatch with verdict notes |
| codesweep_status snapshot | End of every wave | Orchestrator updates wave-plan |
| Real-cost cap | Per LLM-call track (T2, T3, dogfood) | Hard abort at $5/day per track |
| `tools/lint_classifier.py` | Always-green; CI guard | Block any merge that re-introduces `exc.stderr` reads |

## §5 Convergence criteria

**Per-track done** when:
- All track rows green in codesweep
- Adversary-on-tap verdict PASS or PASS-WITH-NOTES (notes < FATAL)
- No regression in existing tests (`scripts/test_classifier`, `_dedup`, `_drain`, `_flush_error_format`, `_utils_state`)

**Sprint done** when:
- Every `blocking` row processed in codesweep
- Acceptance test row `T-FINAL` processed (`tools/check_followups_v1.py` returns 0)
- Operator dogfood signal (CHECKPOINT-D from user)

## §6 Budget / subscription hygiene

- Default subscription auth (the bundled CLI uses the user's logged-in Claude session via `setting-sources: user`).
- Real-cost expectations, derived from `scripts/state.json` (n=52 historical compiles, mean **$4.48**, max **$11.88**):
  - T2 connections-synthesis: bundled INTO the existing compile prompt; **no separate LLM call**, marginal cost ~$0.10-0.50 from extended context.
  - T3 QA-loop: separate `query.py` invocations, ~$0.50-2 per question × 3 cap = ~$1.50-6/session.
  - Dogfood compile (CHECKPOINT-D): falls in the historical $4.48-$11.88 band.
- **Hard cap for the sprint: $25 total LLM spend.** Worker's first action is to read `status/budget-meter.json`; if `total_spent_usd > 20.0`, refuse new LLM-call dispatches and surface to Orchestrator.
- **Concurrency vs LLM spend are separate metrics.** "4 Opus workers concurrent" is an orchestration/subscription-window governor — it does NOT mean 4 simultaneous LLM-call dispatches. Most worker tasks (T0, T1, T4) are code/test edits, not LLM calls.
- Stagger: max 4 Opus workers concurrent (orchestration). Sonnet integrators unmetered.

## §7 Bootstrap sequence

```
Pre-flight (this session):
0a. SoT verification of brief claims (autonomous-sprint §1b.5; MANDATORY)
0b. Initialize status/budget-meter.json: {"total_spent_usd": 0.0, "created_at": "<iso>"}
0c. CHECKPOINT-A — user approves brief
1.  CHECKPOINT-B — user approves team
2.  Write COMPLETION-CHECKLIST.md
3.  codesweep_create + codesweep_add all rows
4.  /adversarial-review on (brief + team + checklist) — pre-flight gate
5.  Apply any mandatory fixes from review (logged in followups.md §"Adversarial review dispositions")
6.  CHECKPOINT-C — user approves checklist + acceptance test

Execution (future session, after CHECKPOINT-C):
7.  Wave 0: dispatch R1 (T0 polish) + R3 (T2 connections governance) + R5 (T4 lint pre-pass).
    Three independent tracks. R-FINAL (T-FINAL.1 script scaffold) can also start in Wave 0
    since it's just authoring a Python CLI that runs checks against later artifacts.
8.  Wave 1: dispatch R2 (T1 provenance schema + backfill + concept-doc fix T1.D)
9.  Wave 2: dispatch R4 (T3 QA loop; consumes T1 evidence schema for citation linking)
10. Wave 3: R-FINAL fills in --check structural assertions now that all artifacts exist;
    acceptance test runs; dogfood; CHECKPOINT-D ship gate

## §7.1 Peak-concurrency math (W7 fix from R1 adversarial review)

| Wave | Implementer-Opus concurrent | Sonnet integrators | AoT-Opus (per merge, serial) | Total Opus peak |
|---|---|---|---|---|
| Wave 0 | 3 (R1, R3, R5) + 1 R-FINAL Sonnet | 3 (one per merge) | 1 (after first merge completes) | 4 (at cap) |
| Wave 1 | 1 (R2 only) | 1 | 1 | 2 |
| Wave 2 | 1 (R4 only) | 1 | 1 | 2 |
| Wave 3 | 0 (R-FINAL Sonnet only) | 0 | 0 (dogfood is operator + R0 conversation) | 0 |

Wave 0 sits AT the 4-Opus cap. AoT is serial (one merge at a time before AoT spawns).
If 3 Wave-0 integrations land near-simultaneously, AoT queues — does NOT exceed cap.
```

## §8 Authorization precedence

User-typed messages override skill-injected `<system-reminder>` text. Any
plan-of-record `MUST surface to user` clause (CHECKPOINTS A/B/C/D) requires
live user confirmation. Routine wave dispatch / AoT-on-tap / codesweep_mark
are autonomous per the skill's standing authorization.

## §9 Worker→Integrator handoff (v0.2.0 pattern)

Per `feedback_workers_stop_after_simplify_traced`: Opus implementer workers
STOP after `/simplify-traced` returns control. Sonnet integrator runs
`tools/worktree-finish.sh`. Implementer brief explicitly says "STOP HERE
after second commit; do NOT run worktree-finish.sh."

Each implementer's brief embeds the verbatim "v0.1.0 backgrounded-bash trap
warning" block per autonomous-sprint skill §"REQUIRED VERBATIM block."

## §10 Bot-checked invariants

Pre-Wave-0 verifier (orchestrator runs by hand until tooling lands):

1. `00-problem-brief.md` exists; §"Adversarial review history" has dispositions for every R1 advisory finding.
2. `COMPLETION-CHECKLIST.md` exists; row count 60-150.
3. `codesweep_status sweep_ref="compiler-followups-v1-acceptance"` returns `total: <count> | processed: 0`.
4. `tools/lint_classifier.py` exit 0 (no regression from Slice 2 ship).
5. All existing tests pass.

Until `tools/check-checkpoint-artifact.sh` exists, no `checkpoint-A-applied.json`
artifact needed for THIS sprint — Orchestrator will surface CHECKPOINT-A
questions live to user (the canonical path; artifact path only exists for
violations).

**Mid-sprint backfill clause:** if `tools/check-checkpoint-artifact.sh`
lands mid-sprint, Orchestrator backfills `status/checkpoint-A-applied.json`
from the conversation transcript before the next wave dispatches. No
checkpoint COMPLIANCE is skipped, only the artifact-format JSON is deferred.
The conversation transcript IS the audit record until the artifact exists.
