# R1 Judge verdict — compiler-followups-v1 adversarial review

**Date:** 2026-05-18
**Authority:** supreme; binding on Orchestrator
**Spot-checks performed (all five):** F1, F2, F4, F6, S1.

---

## Spot-check results (judge-confirmed evidence)

| Check | Adversary claim | Defender claim | Independent finding |
|---|---|---|---|
| **F1** (`compile.py` two-pass) | No two-pass dedup; single `query()` + file-level skip only | CONCEDE | **CONFIRMED.** `grep -n "query(" compile.py` = 1 hit (line 132). Hash check at line 196 is only the file-level skip predicate (`prev.get("hash") != file_hash(log_path)`). Lines 53–63 still pump every existing article into every prompt. No two-pass, no per-article dedup, no cost retirement. |
| **F2** (connections empty) | Dir has 19 substantive articles, not 0-byte | CONCEDE | **CONFIRMED.** `find … -size 0` = 0; `ls … \| wc -l` = 19. Brief's "every file is 0 bytes" is factually wrong against live tree. |
| **F4** (FLUSH_ERROR count) | 0 hits today; 100 in 5d impossible | PARTIAL | **CONFIRMED 0 hits** for `FLUSH_ERROR.*exit_code=` regex (28823 lines in `flush.log`, 163 contain `FLUSH_ERROR`, ZERO match the same-line `exit_code=` regex). Slice 1 ships a structured format that breaks across lines — the regex is wrong for the current artifact. Defender is right that T0.C.1 is already an "if" guard with a deferral path; adversary is right that the regex itself is broken. Both partial-correct. |
| **F6** (cost math) | Mean compile far above $5/run threshold | CONCEDE | **CONFIRMED.** n=52, mean **$4.48**, max **$11.88**, total **$232.74**. T-FINAL.6 "< $5/run" trips on 26/52 historical compiles (50%). $5/day breaker = ~1 compile/day. $10 sprint cap = ~2 historical compiles. Three internal contradictions are real. |
| **S1** (waived artifact) | Skill-mandatory checkpoint artifact waived | DEFEND | **CONFIRMED DEFENDER.** `team-of-agents-v1.md:160-163` defers the `checkpoint-A-applied.json` artifact only until `tools/check-checkpoint-artifact.sh` exists. The canonical compliance path (user responds to CHECKPOINT-A in conversation) is preserved. This is tooling deferral, not skill violation. Defender's added clarification ("backfill from transcript if tool lands mid-sprint") is sound. |

All five spot-checks land where the defender placed them. No defender concessions are overstated; no defender defenses are unsupported.

---

## Verdict Summary

| ID | Adversary | Defender | **Ruling** | Severity |
|---|---|---|---|---|
| F1 | Two-pass claim false | CONCEDE | **UPHELD** — brief premise factually wrong | **FATAL** |
| F2 | Connections dir not empty | CONCEDE | **UPHELD** — inherited stale claim | **FATAL** |
| F3 | No pass-2 to extend | CONCEDE | **UPHELD** — Path A (extend single prompt) accepted | **FATAL** |
| F4 | T0.C cannot pass today | PARTIAL | **UPHELD as MAJOR** — regex broken, deferral path exists but T0.C.0 still demands a working script day-1 | **SERIOUS** (downgraded from FATAL) |
| F5 | grep `-E "a\|b"` bug | PARTIAL | **UPHELD on T1.A.2 + T0.A.3 + T3.B.2; dismissed on T2.A.3 + T3.A.1 (BRE alternation)** | **SERIOUS** |
| F6 | Cost math broken | CONCEDE | **UPHELD** — three contradictions confirmed against state.json | **FATAL** |
| S1 | Waived checkpoint artifact | DEFEND | **DEFENDED** — tooling deferral, compliance preserved | DISMISSED |
| S2 | T1↔T2 prompt-edit collision | PARTIAL | **UPHELD as integration risk** — call out in §7 | **SERIOUS** |
| S3 | R0 owns code rows | CONCEDE | **UPHELD** — Orchestrator cannot author code | **SERIOUS** |
| S4 | T-FINAL.3/4/5 collapsed into .2 | CONCEDE | **UPHELD** — per-check exit codes required | **SERIOUS** |
| S5 | "Modified this sprint" undefined | CONCEDE | **UPHELD** — strict-path fix accepted | **SERIOUS** |
| S6 | $5 cap inconsistent | CONCEDE | **UPHELD** — rolled into F6 | (folded into F6) |
| W1 | Cost-retirement claim | CONCEDE | **UPHELD** — folded into F1 | (folded) |
| W2 | No QA tier for LLM tracks | PARTIAL | **UPHELD** with dogfood-evidence requirement | RECOMMENDED |
| W3 | 100ms vs 1s mismatch | PARTIAL | **UPHELD** — drop the 100ms aspiration | RECOMMENDED |
| W4 | followups.md ambiguous | CONCEDE | **UPHELD** — stamp two H2 sections | RECOMMENDED |
| W5 | Lint canaries undefined | PARTIAL | **UPHELD** — enumerate 4 canaries | RECOMMENDED |
| W6 | claim_summary mismatch | DEFEND (partial CONCEDE) | **DEFENDED on claim_summary; UPHELD on the F5 regex bug (already covered)** | DISMISSED for the W6-specific claim |
| W7 | Concurrency math | PARTIAL | **UPHELD** — add explicit peak math to §7 | RECOMMENDED |
| N1 | Row count wrong | DEFEND | **DEFENDED** — math is correct (60 rows) | DISMISSED |
| N2 | Forward reference | PARTIAL | **UPHELD** — note sequencing | NITPICK |
| N3 | Term unspecified | (ack) | **DISMISSED** — unactionable | DISMISSED |
| N4 | Wave sequencing | DEFEND | **DEFENDED** — sequencing is explicit | DISMISSED |
| N5 | budget-meter.json bootstrap | CONCEDE | **UPHELD** — add bootstrap step | NITPICK |

**Tally:** 5 FATAL + 5 SERIOUS + 4 RECOMMENDED + 2 NITPICK + 5 DISMISSED.

---

## Mandatory Fixes (before CHECKPOINT-A)

The brief is currently built on **three factually-wrong claims** plus **one internally-inconsistent budget**. CHECKPOINT-A cannot surface to the user against a brief in this state — the user would be approving a fiction.

### M1. Rewrite the brief's "Problem statement" + "Known context"  *(addresses F1, F2, F3, W1)*

Edit `00-problem-brief.md`:
- **Problem-statement para 1:** strike "compile-cost retired by two-pass dedup … hash-equality idempotency gate." Replace with the defender's wording: *"The file-level hash check in `compile.py:main()` skips unchanged daily logs, but per-article compile cost still scales linearly with KB size (full `existing_articles_context` sent every compile). Cost reduction is OUT OF SCOPE for this sprint — see Out of scope deferral."*
- **Known-context bullet on connections:** strike "0-byte placeholders / every file is 0 bytes." Replace with: *"`knowledge/connections/` has 19 articles (75KB) but no governance — `compile.py` produces connections opportunistically without quality / format / per-run-count guard rails."*
- **Known-context bullet on Pass-2:** strike "Pass-2 prompt has Read/Write/Edit/Glob/Grep tool allowlist." Replace with: *"The compile prompt (single `query()` call) has Read/Write/Edit/Glob/Grep tool allowlist."*
- **Gap re-enumeration:** the May-13 retro had **5** structural gaps. Gap #3 (per-article compile cost) is explicitly deferred to a post-sprint follow-up.

**Why mandatory:** the user signs off on the brief at CHECKPOINT-A. Signing off on demonstrably false premises is a process bug; this is the same pattern as FATAL-1 in Slice 2 (synthetic mental model instead of source-of-truth check). Cannot ship.

### M2. Rewrite T2.A.1, T2.A.2, T2.A.4  *(addresses F2, F3)*

Edit `COMPLETION-CHECKLIST.md`:
- **T2.A.1** — change assertion from "0-byte placeholder files are deleted" to: *"the empty-dir guard in compile.py is removed if present OR replaced with a 'has-at-least-N-existing-concepts' precondition; verify by `grep -nE 'connections.*0|empty' scripts/compile.py` returning no surviving early-return."*
- **T2.A.2** — adopt Path A: *"compile.py's single prompt is extended with a `## Cross-concept connections` rules block instructing the LLM when to emit a connection article (predicate: 2+ existing concepts referenced + non-obvious relationship). One `query()` call total."*
- **T2.A.4** — rename test `test_connections_synthesis_respects_per_run_cap`. Verifies the per-run cap, not a no-longer-existent skip path.

**Why mandatory:** rows reference scaffolding (pass-2, empty placeholders) that doesn't exist. Worker R3 would either fabricate evidence to make the row pass or fail the row through no fault of their own.

### M3. Fix T-FINAL.6, T0.D.3, §6 budget math  *(addresses F6, S6)*

- **T-FINAL.6:** strike "< $5 for the run." Replace with: *"user signs off at CHECKPOINT-D that the compile produced sensible output AND that observed cost was within their tolerance; record cost in dollars, do not hardcode a threshold. Threshold-tuning is post-sprint after T0.D telemetry."*
- **T0.D.3 default cap:** raise from $5/day to **$15/day** (≥2 historical-mean retries/day). Rationale stated in the row: "historical mean compile $4.48; cap permits ≥2 retries/day before circuit-breaker fires."
- **§6 of team-of-agents:** restate as *"$25 total LLM spend across the sprint. Dogfood compile at CHECKPOINT-D is the only in-task LLM call (≈$5–$10 expected). Worker tasks R1–R5 are code/test edits, not LLM calls. The '4 Opus workers concurrent' figure refers to ORCHESTRATION concurrency, not in-task LLM spend."*

**Why mandatory:** internally inconsistent budgets cause one of three failure modes — (a) circuit-breaker trips on every run, (b) dogfood fails its own cost gate, (c) Wave 1 dispatch overshoots cap silently. All three poison the sprint signal.

### M4. Fix the broken `grep -E "a\|b"` rows  *(addresses F5)*

- **T1.A.2 verify:** `grep -E "(session_id|flushed_at|confidence)"` (parens or unescaped `|`).
- **T0.A.3 verify:** replace MD-table escapes — `[[ $(ls scripts/flush.log* | wc -l) -ge 1 ]]`.
- **T3.B.2 verify:** outer `\|` is a shell-pipe escape leak — use literal `|`.
- **COMPLETION-CHECKLIST.md preamble:** add the note *"Verify commands are bash-as-pasted, NOT markdown-table-escaped. Pipes are literal `|`; alternation in `grep -E` is `|`; alternation in BRE `grep` is `\|`."*

**Why mandatory:** verify commands that silently match nothing are worse than no verify — they auto-pass. Same failure shape as the F1 family.

### M5. Reassign T-FINAL.1-5 ownership from R0 to a new R-FINAL Sonnet role  *(addresses S3)*

- Add R-FINAL to §1 role catalogue: *"R-FINAL — Acceptance test author (Sonnet); `tools/*` Write + Bash; authors `tools/check_followups_v1.py` + 4 sub-checks + dogfood orchestration; owns T-FINAL.1–5."*
- T-FINAL.6 (dogfood user signoff) stays with R0.
- §7 worker-staggering math updated: R-FINAL is Sonnet, unmetered, no concurrency cap impact.

**Why mandatory:** R0 (Orchestrator) charter explicitly excludes code-writing. Leaving the rows on R0 either (a) blocks them indefinitely or (b) forces R0 to violate its charter.

### M6. Per-check exit codes for T-FINAL.2/3/4/5  *(addresses S4)*

`tools/check_followups_v1.py` returns separate non-zero exit codes per sub-check (`check_polish_items`, `check_structural_gaps`, `check_unit_tests`, `check_lint_clean`). Each T-FINAL row verifies with `--check <name>`. Granular fail-signal preserved.

**Why mandatory:** one binary exit code across four orthogonal acceptance dimensions destroys diagnostic value — a single failure hides the other three.

### M7. T0.C.0 explicit day-1 path  *(addresses F4)*

- Add sub-step 0 to T0.C.0: *"BEFORE writing `tools/check_classifier_groundedness.py`, sample the real flush.log line format — `python -c "import re; lines=open('scripts/flush.log').readlines(); print([l for l in lines[:30] if 'FLUSH_ERROR' in l])"`. Build regex against ground truth."*
- Make the exit-0 day-1 path explicit: *"if 0 grounded patterns found, exit 0 IFF every regex in `classifier.py::CLASSIFICATIONS` is annotated `# UNVERIFIED — speculative`."*
- T0.C.1 explicitly Blocking=N in the "Constraints" section of the brief (currently is N but easily promoted).

**Why mandatory:** today's `flush.log` has 0 matches for the encoded regex. Without an explicit day-1 path, the script will either fabricate a pass or block the sprint on telemetry that takes 7d to accumulate.

### M8. Stamp `followups.md` at sprint open  *(addresses W4)*

Two H2 sections, with FU-FU1-T0-C pre-populated as the known classifier-regex deferral.

**Why mandatory:** brief uses "followups" with two meanings (round-3 deferrals vs exec-time discoveries). Clarifying at sprint open prevents the same word collapsing into one bucket mid-sprint.

---

## Recommended Fixes (before CHECKPOINT-C — acceptance criteria signoff)

### R1. Add CHECKPOINT-D dogfood-evidence requirement *(W2)*
§5 convergence criteria: *"Per-track-with-LLM-call done ALSO requires CHECKPOINT-D dogfood to specifically exercise the new LLM path (T2 synthesis on a real recent daily; T3 query on a real recent session question). Recorded in `status/dogfood-evidence.md`."*

### R2. Rewrite T3.A.3 latency assertion *(W3)*
Drop the 100ms aspiration. Use: *"stays under 1s wall (Python startup + 3 fire-and-forget Popens of query.py)."*

### R3. Enumerate the 4 lint canaries *(W5)*
T-FINAL.3 acceptance line: explicit 4 canaries — (a) missing frontmatter, (b) malformed frontmatter, (c) dead wikilink, (d) orphan article.

### R4. Add prompt-edit interleave callout to §7 *(S2)*
*"R2 (T1.B.1, T1.B.3) and R3 (T2.A.2) both modify the compile-prompt text. Integrator R6 forward-merges T1 first, then T2. Re-run `pytest scripts/test_classifier.py scripts/test_dedup.py` after each integration. Conflict-resolution preference: T1 outer scaffold, T2 as `## Cross-concept connections` subsection."*

### R5. T1.B.2 strict path *(S5)*
Drop "modified during this sprint." After T1.C backfill, `tools/check_evidence_blocks.py knowledge/concepts/` exit 0 means every concept has `evidence:`.

### R6. Explicit peak-concurrency math in §7 *(W7)*
*"Wave 0 peak = R1 (1 Opus). Wave 1 peak = R2-R5 (4 Opus, at cap). AoT (R7) post-integration, serial. R6 integrators are Sonnet (unmetered)."*

### R7. §10 mid-sprint backfill clause *(S1 — defender's added clarification)*
*"If `tools/check-checkpoint-artifact.sh` lands mid-sprint, Orchestrator backfills `checkpoint-A-applied.json` from the conversation transcript before the next wave dispatches. No checkpoint compliance is skipped, only the artifact format is deferred."*

### R8. `status/budget-meter.json` bootstrap step *(N5)*
§7 Bootstrap "Step 0.5: Orchestrator initializes `status/budget-meter.json` with `{"total_spent_usd": 0.0, "created_at": now()}`."*

### R9. T-FINAL.4 forward-reference note *(N2)*
§7 sequencing: *"T0.A.4 (test_log_rotation) must ship before T-FINAL.4 runs."*

---

## Dismissed Findings

- **S1 (DEFENDER UPHELD):** the checkpoint-A artifact deferral is bounded tooling deferral, NOT skill violation. The canonical compliance path (user-in-conversation) is preserved. Defender's mid-sprint backfill clarification is sound and rolled into R7.
- **W6 (DEFENDER UPHELD):** `claim_summary` is correctly listed as optional in the row's assertion text. Adversary misread. The verify-regex bug W6 also raised is the same bug as F5/T1.A.2; fix once, count once.
- **N1 (DEFENDER UPHELD):** row count math (60 total) is correct. T0=19, T1=11, T2=6, T3=8, T4=10, T-FINAL=6. No fix.
- **N3:** adversary did not specify the disputed term. Unactionable.
- **N4 (DEFENDER UPHELD):** §7 wave sequencing IS explicit (Wave 0 → Wave 1 → Wave 2).

---

## Sprint Health Score: **6 / 10**

**Reasoning.** The sprint *shape* is sound — 5 tracks, 60 rows, 4-wave dispatch, Sonnet/Opus mix, codesweep-tracked acceptance — and the defender's concessions are concrete and surgical, not hand-wavy. But the *substrate* of the brief contains 3 factually wrong claims (compile-cost retired / connections empty / two-pass exists) plus 1 internally-inconsistent budget. Those are not paperwork issues; they redefine what the sprint is for and what "done" means. A user signing off at CHECKPOINT-A would be signing off on a fiction.

Score lands at **6** because:
- Sprint shape: sound (would be 8–9 alone).
- Brief substrate: 4 mandatory rewrites — substantial.
- All mandatory fixes are concrete, scoped to existing artifacts, and unblock Wave 0.
- The user can land at **8** with M1–M8 applied; CHECKPOINT-A then surfaces against a true brief.

**Recommendation:** *do not surface CHECKPOINT-A as-is.* Apply M1–M8 first. Then surface a revised brief. R1–R9 can land before CHECKPOINT-C.

---

## Process Reflection

The adversary's core hit (F1+F2+F3) is the same shape as the FATAL-1 bug class from Slice 2: **a synthetic mental model substituted for a check against the live codebase.** This time the synthetic source was `claude-memory-compiler-setup.md` (the concept article in the wiki) — a document that describes the system's *aspirational* architecture, not its *current* state. The brief inherited those aspirational claims verbatim:

- "two-pass dedup" — exists in the concept doc as a planned mechanism; never shipped.
- "0-byte placeholders" — was true at a moment in time; 19 substantive articles have since landed.
- "Pass-2 prompt" — the concept doc names a pass-2 phase; the code has one phase.

**Should adversarial review catch this?** Yes — and it did. F1, F2, F3 all fire on `wc -l`, `find -size 0`, and `grep -c 'query('` — three commands the brief author could have run before writing the brief. That they fired in adversarial review and not in brief authoring is the process gap.

**Recommended pre-flight step:** add a **"Source-of-Truth Re-Verification" phase** to the autonomous-sprint skill, BEFORE adversarial review fires. For each quotative claim in the brief that names a file path, a line count, a function name, a regex pattern, or a directory state, the brief author runs:

```
For each claim of form "X in <file> does Y":
  - Read or grep <file>
  - Confirm Y is present in the live tree
  - If not, flag as "STALE — needs re-derivation"
```

This is cheap (60s of `grep`/`ls`/`wc`) and would have caught F1, F2, F3, F6 all four at brief-author time. Adversarial review is currently doing the work of basic source-of-truth verification AND the work it's actually scoped for (architecture, contradictions, dependency drift). Move the SoT-verification upstream and adversarial review gets sharper at its real job — finding the structural problems (S3, S4, F4's regex-vs-format mismatch) that grep cannot find.

**This judge recommends:** add a `pre-adversary-sot-check.sh` to the autonomous-sprint skill that diffs every file path / line count / function name claim in the brief against the live codebase, before the adversary fires. Adversarial review then receives a brief that is already passing the basic-truth test.

The adversary did its job. The defender did its job. The brief was downstream of a process gap, not an author gap. Fix the process.
