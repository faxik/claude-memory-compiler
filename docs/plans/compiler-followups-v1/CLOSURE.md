# Sprint compiler-followups-v1 — CLOSURE

**Date closed:** 2026-05-18 (pre-flight only; never dispatched)
**Status:** DEFERRED — sprint apparatus dropped; T0 + T4 work moves to inline implementation; T1/T2/T3 deferred to a properly-scoped future sprint.

## Why this sprint did not run

The pre-flight planning phase produced 4 artifacts (brief, team, checklist, codesweep) that passed initial review well but accumulated rework cost over 2 rounds of adversarial review:

**Round 1 (5 FATAL):** Brief substrate inherited 3 wrong claims from the aspirational concept article `knowledge/concepts/claude-memory-compiler-setup.md` without re-verifying against the live tree. Plus a 20×-off budget claim. Score: 6/10. Patched M1-M8 + R1-R9.

**Round 2 (3 NEW FATAL):** Patches were authored from memory without running the new Verify commands against the live tree. NF1 (T0.C.0a verify returns 0 not ≥2), NF2 (T1.D.1 verify auto-passes today with 1 accidental match), NF3 (T2.B.1 verify auto-passes today with 19 baseline matches). Score: 4/10 — REGRESSED.

The adversary's diagnosis cut to the meta-pattern:

> Three rounds of the same bug class across two sprints — the right fix is **process** (skill-level), not yet-another-row-patch.

The pre-flight overhead (90 min orchestrator wall + ~$4.50 in 3 adversarial-review dispatches) had reached a meaningful fraction of the implementation budget (~3-4h for T0 + T4 inline). User decision: drop the sprint apparatus; implement T0 + T4 inline; defer T1/T2/T3 to a properly-derived future sprint.

## What ships from here (inline, not via this sprint)

- **T0 polish (6 items, ~3h):** log rotation, section-name preservation, classifier UNVERIFIED annotations + audit script, cost-budget circuit breaker, timespan check, flock cross-process test.
- **T4 lint pre-pass (4 sub-items, ~1h):** tools/lint_kb.py with frontmatter validation + dead-link detection + orphan detection + integration with scripts/lint.py.

Each lands as its own commit on `main` in the compiler repo. Tests written and run before each commit. No worktree workflow (compiler repo doesn't use them; this is a single-developer per-user knowledge base).

## What defers to a future sprint

- **T1 (provenance schema):** the `evidence:` block design + compile.py emit + backfill. Needs design-council pass to decide full-fidelity vs summary fidelity (CHECKPOINT-A Q2 was never resolved).
- **T2 (connections governance):** the per-run cap + naming convention prompt edits. Needs to interleave with T1's prompt edits — easier if T1 is stable first.
- **T3 (QA loop):** hooks/session-start.py wires query.py; query.py output writes to knowledge/qa/. Needs T1's `evidence:` schema for citation linking. CHECKPOINT-A Q4 trigger policy was never resolved.

Pre-populated FU entries in `followups.md`:
- FU-FU1-T0-C (classifier regex refinement after ≥7d Slice-1 telemetry)
- FU-FU1-COMPILE-COST (May-13 retro gap #3, separate sprint)
- FU-FU1-CONCEPT-DOC-STALE (process pattern — concept docs as aspirational, not state-of-record)

Adding to followups bucket on closure:
- **FU-FU1-DEFERRED-T1** — Provenance schema design + implementation. Re-derive from live state. Target: dedicated sprint.
- **FU-FU1-DEFERRED-T2** — Connections governance (per-run cap + naming). Interleaves with T1's prompt edits. Target: same sprint as T1 OR follow-on.
- **FU-FU1-DEFERRED-T3** — QA loop wiring. Depends on T1 schema. Target: after T1.
- **FU-FU1-CONCEPT-DOC-STALE-FIX** — Update `knowledge/concepts/claude-memory-compiler-setup.md` to reflect closed gaps (#1 partial, #4 closed by inline T0+T4, #5 closed by inline T4). Was originally T1.D in this sprint; moves to inline followup.

## Lessons for the autonomous-sprint skill

Two patches recommended (separate from THIS sprint's closure):

1. **§1b.5 mandatory SoT re-verification** — already added in this session (per round-1 R7 fix).
2. **§3a.5 mandatory Verify-command live-check** — RECOMMENDED ADDITION. Every new/edited Verify column in COMPLETION-CHECKLIST.md MUST be paste-executed against the live tree by the author before commit. Round-2 NF1/NF2/NF3 are textbook examples of why; round-1 F5 is the same pattern at brief-author time.

A future PR to the skill should add §3a.5 mirroring §1b.5's structure. Until then, manual discipline.

## Audit trail preserved

Workspace remains in `docs/plans/compiler-followups-v1/`:
- `00-problem-brief.md` (rewritten post-round-1; not perfect, see CLOSURE for residuals)
- `team-of-agents-v1.md` (post-round-1)
- `COMPLETION-CHECKLIST.md` (post-round-1)
- `followups.md` (with adversarial-review dispositions + 3 carry-forward FUs)
- `review/r1-judge.md` (the round-1 verdict that triggered the rewrite)
- `CLOSURE.md` (this file)
- `codesweep SW-68 compiler-followups-v1-acceptance` (archived; 67 rows pending → never executed)

Future sprint authors: this workspace is a worked example of the failure modes the §1b.5 + §3a.5 phases are designed to catch. Read before authoring a brief that cites concept articles or prior-sprint plans.
