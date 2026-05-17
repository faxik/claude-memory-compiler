# Reviewer Feedback — design-council, 2026-05-17 (compiler-resilience)

**Reviewer:** fresh Opus dispatch, NOT one of the prior agents (architects A/B/C, adversary, judge).
**Workspace:** `/home/faxik/tools/claude-memory-compiler/docs/plans/design-council-compiler-resilience/`
**Skill under review:** `/home/faxik/.claude/skills/design-council/SKILL.md`
**Protocol:** `/home/faxik/.claude/skills/_shared/self-grading.md`
**User's original prompt:** *"Since it didn't converge, do design council given your current knowledge."* — invoked after an adversarial-review round exposed 2 NEW FATALs against a round-2 patched plan.

---

## 1. Contract Adherence

| Promise (SKILL.md) | Status | Evidence |
|---|---|---|
| Phase 0 problem brief written before architects launch | **DELIVERED** | `00-problem-brief.md` (127 lines) has all 6 required sections (Problem Statement, Constraints, Success Criteria, Known Context, Open Questions, Team Composition). Brief explicitly assigns differentiated perspectives to A/B/C (00-problem-brief.md:116-120). |
| Researcher activated when warranted | **N/A — JUSTIFIED SKIP** | Brief explicitly justifies skipping researcher: *"No Researcher — The 'external knowledge' we need is already gathered (SDK source + state.json baseline)"* (00-problem-brief.md:126). SKILL.md heuristic permits skipping when architects can do their own targeted grep. |
| 3 architects in parallel with DIFFERENT perspectives | **DELIVERED** | A = "Embrace Untyped" (in-process message-sniff), B = "Move Resilience Out" (process-boundary supervisor), C = "Park-and-Resume" (filesystem queue + deferred drainer). Perspectives are genuinely orthogonal. See §3. |
| Each architect proposes 3 DISTINCT solutions | **DELIVERED** | A1 Sniff Table / A2 Inline Heuristic / A3 Two-Stage Triage; B1 Supervisor / B2 Drainer-on-Orphan / B3 Sync Hook; C1 Quiet Park / C2 State.json Queue / C3 Append-Only Journal. 9 options total. |
| Architects do NOT read each other's R1 output | **DELIVERED (with one soft violation)** | No direct cross-naming in A/B. C makes one abstract reference to *"a sibling architect's text-pattern detection"* (03-architect-c.md:909). See §2 W1. |
| Adversary attacks ALL 9 proposals | **DELIVERED** | `05-adversary-r1.md` has named sections "Proposal A1/A2/A3/B1/B2/B3/C1/C2/C3" + Comparative Analysis + Scorecard + Honest Effort Re-Estimates. Every proposal addressed. |
| Adversary verifies against codebase (grep, not speculation) | **DELIVERED — high quality** | Spot-verified 5 claims: (a) hook timeouts at `.claude/settings.json` SessionStart=15, PreCompact=10, SessionEnd=10 — exact match; (b) `subprocess_cli.py:613-616` ProcessError hardcoded stderr="Check stderr output for details" — exact match; (c) `batch-flush.py:454` non-atomic write — exact match; (d) `pyproject.toml` has no pytest dep — exact match; (e) 32 session-flush-*.md + 0 flush-context-*.md orphans on disk — exact match. |
| Judge synthesizes + recommends + surfaces dilemma + hesitations | **DELIVERED** | `06-judge-r1.md` ruling table per proposal, named synthesis (A1+C1+adversary's dedup), explicit Major Dilemma with α/β options, 6 named Hesitations. Acts as advisor, not arbitrator. |
| User checkpoint before Round 2 | **DELIVERED (per reviewer prompt context)** | User selected α-split (Slice 1 now, Slice 2 deferred). This decision propagates verbatim into FINAL-DESIGN.md: "**User decision:** α-split" (FINAL-DESIGN.md:9) and shapes FINAL-PLAN.md scope ("Slice 1 only", FINAL-PLAN.md:3). |
| Round 2 (skipped) | **JUSTIFIED SKIP** | FINAL-DESIGN.md:5 documents: *"Rounds: 1 (skipped R2 — Slice 1 specified clearly enough; Slice 2 will get its own R2-style refinement pass when evidence is in)"*. SKILL.md §"Common Mistakes" explicitly permits this: *"Running all 3 rounds when Round 1 produces a clear winner — respect the user's time; if the Judge says READY, finalize."* Judge said ALMOST-READY for the synthesis but the user's α-split converts Slice 1 into a fully-specifiable scope. Defensible. |
| FINAL-DESIGN.md + FINAL-PLAN.md | **DELIVERED** | Both present, internally consistent. FINAL-DESIGN.md covers full A1+C1 synthesis (215 lines); FINAL-PLAN.md scopes to Slice 1 only (339 lines, 9 steps). |
| Self-grading (Phase 4) | **IN PROGRESS** | This file. |

**Overall contract adherence: A.** Every promise either delivered or justifiably skipped with explicit reasoning in the artifact. The Round-2 skip is the most aggressive decision and it is defended on grounds SKILL.md explicitly endorses.

---

## 2. What Went Sideways

### W1 — Architect C makes one abstract sibling reference (SOFT VIOLATION, mitigated by Edit 1 from prior run)

**WHAT.** `03-architect-c.md:909` says: *"A sibling architect's text-pattern detection of 'rate limit' in `stderr_tail` could compose into this design — if the drainer's `last_error.stderr_tail` contains rate-limit signal, multiply the next-attempt delay. That's a clean orthogonal extension."* The phrase "a sibling architect's text-pattern detection" attributes a specific technique to a specific (un-named) sibling.

**ROOT CAUSE.** SKILL.md §Step 1b (after the prior run's Edit 1) says: *"ALSO: do not refer to sibling architects by name... You may name and rebut alternative approaches in the abstract ('primitive-first risks X') but not as if you've read another architect's specific proposal."* Architect C does NOT name the sibling by letter ("Architect A's"), but DOES attribute the technique ("text-pattern detection of 'rate limit' in stderr_tail") with enough specificity that it reads like cross-reading. The phrase "text-pattern detection of 'rate limit' in stderr_tail" matches Architect A's CLASSIFICATIONS table approach (01-architect-a.md:99-101 — `_Rule("rate_limit_5h", ... needle=re.compile(r"rate.?limit|429..."))`).

However: the brief itself proposes this very approach in Open Question #1 (00-problem-brief.md:100): *"the pragmatic options are: (a) catch Exception, parse str(exc) for known transient patterns"*. So Architect C COULD be reasoning from the brief alone. Cannot distinguish from artifacts. Edit 1 from the prior run is partially effective; the violation is softer than the prior run's "Architect A's witness primitive is premature" but the same shape persists.

**WHO should have caught it.** Edit 1 from prior run handled the "by name" case; it does NOT handle the "by technique" case. Architect C's prompt template should additionally forbid: "Do not attribute specific *techniques* to siblings ('a sibling architect's regex sniff'). Discuss alternatives only in the abstract perspective frame ('message-pattern matching') if at all."

**HOW to prevent.** Sharpen the existing prohibition. See §5 Edit 1.

**Severity: LOW.** Outcome is fine; same shape but milder than prior run.

### W2 — Judge cites an unverifiable denominator on the dominant-failure-mode claim

**WHAT.** `06-judge-r1.md:11` says: *"`grep "exit code 1" scripts/flush.log | wc -l` → **1637 / 1648 = 99.3%** of all FLUSH_ERROR lines."* I verified `grep -c "exit code 1" scripts/flush.log` = **1637** (matches numerator). But `grep -c "FLUSH_ERROR" scripts/flush.log` = **163** (not 1648). `grep -c "Agent SDK error" scripts/flush.log` = **507**. `grep -c "ERROR" scripts/flush.log` = **1170**. None of these match the Judge's denominator of 1648.

The Adversary's claim was different and verifiable: `05-adversary-r1.md:100` says *"163 `FLUSH_ERROR` lines, 162 of them `exit code 1`"* — 163 matches `grep FLUSH_ERROR` exactly. The Adversary appears to have counted the FLUSH_ERROR-line-level signal; the Judge appears to have introduced a denominator that I cannot derive from any single grep on either of the two log files (`flush.log` 28,697 lines; `compile.log` 4,284 lines).

The Judge's *load-bearing* downstream claim is *"any retry layer that doesn't have a rule for it is fiction."* That claim survives even with the right denominator (162/163 = 99.4% per the Adversary). But the specific number "1637/1648 = 99.3%" is unverifiable from the artifacts available to me.

**ROOT CAUSE.** Judge introduced an enhancement-of-Adversary citation. Possibly fabricated the denominator to match the Adversary's percentage; possibly counted some other log file or filtered log subset I cannot reproduce. Either way it's a citation that does not survive re-verification.

**WHO should have caught it.** The Judge's own verification block at the top of `06-judge-r1.md:4-13` shows discipline. The single line that introduced this denominator was not double-checked. SKILL.md §"Step 1d" prompt template says: *"For each proposal: 1. Does the adversary's attack hold up?"* but does not require Judge to re-verify Adversary's quantitative citations *as enhanced/re-counted by the Judge*. The Judge's own citations should also be greppable.

**HOW to prevent.** SKILL.md §"Step 1d" should add: "Every quantitative claim in your output must be a verbatim citation from upstream agents OR a fresh grep that you ran yourself with the exact command shown. Do not silently re-aggregate upstream counts."

**Severity: LOW-MEDIUM.** The downstream conclusion holds; the specific number does not. This is the same class of failure as the prior run's W2 (adversary inflated 9 → 13 → really 7 writer sites). Recurring failure mode at the quantitative-precision layer.

### W3 — FINAL-PLAN.md claims tests are `unittest.IsolatedAsyncioTestCase` but the actual tests are plain `unittest.TestCase`

**WHAT.** `FINAL-PLAN.md:18` says: *"Uses stdlib `collections.deque` and `unittest`."* and §"Testing Strategy" (FINAL-PLAN.md:299) says: *"**Unit tests:** 7 `unittest.IsolatedAsyncioTestCase` tests"*. But the actual test class in Step 5 (FINAL-PLAN.md:172-247) is `class TestFlushErrorFormat(unittest.TestCase)` — plain TestCase, NOT IsolatedAsyncioTestCase. None of the 7 test methods are `async def`. None of them await anything. There is no asyncio involvement.

This is harmless (plain TestCase is the correct choice — the tests target a synchronous helper `_build_flush_error_response`), but the plan's prose contradicts the plan's own code block. A reader who only skimmed the §"Testing Strategy" section would expect async tests and be confused.

**ROOT CAUSE.** Orchestrator likely lifted "IsolatedAsyncioTestCase" from the brief's success criterion #6 wording ("dependency-injection-style tests on the retry helper, OR an integration test that simulates...") and Adversary's recommendation to *"rewrite tests on `unittest.IsolatedAsyncioTestCase`"* (05-adversary-r1.md:53, 116, 167). For Slice 1, no async is needed because there's no retry yet. The prose was not updated when the implementation scope narrowed.

**WHO should have caught it.** Orchestrator's own consistency check on FINAL-PLAN.md. SKILL.md does not currently require a final cross-check that FINAL-PLAN.md's prose matches its code blocks.

**HOW to prevent.** Add to SKILL.md §"Phase 3: Produce Final Deliverables": "Before reporting done, scan FINAL-PLAN.md for prose-code mismatches. Test framework / dependency / file-path claims in prose must match the code blocks below them."

**Severity: LOW.** Harmless cosmetic inconsistency. But it's the kind of thing that erodes trust when caught on review.

### W4 — Test count mismatch: prose says "4 tests", strategy says "7 tests", code block has 7 methods

**WHAT.** `FINAL-PLAN.md:16` table row says *"4 `unittest.IsolatedAsyncioTestCase` tests"*. `FINAL-PLAN.md:299` Testing Strategy says *"7 `unittest.IsolatedAsyncioTestCase` tests"*. The actual code block (Step 5) has exactly 7 test methods: `test_includes_exit_code_when_process_error`, `test_omits_exit_code_for_bare_exception`, `test_includes_stderr_tail_when_callback_fed`, `test_truncates_stderr_tail_to_50_lines`, `test_preserves_existing_regex_parseability`, `test_truncates_long_message_to_300_chars`, `test_records_sdk_stderr_even_when_boilerplate`. So the body says 7; the front-matter table says 4. Inconsistent. The 4 likely came from the FINAL-DESIGN.md §"Slice 1 Test Strategy" (FINAL-DESIGN.md:156-162) which lists 4 tests by name — but the implementation grew to 7 during plan writing without back-updating the design.

**ROOT CAUSE.** Same as W3: orchestrator drafted FINAL-DESIGN first, then FINAL-PLAN, but did not back-propagate when the plan added detail.

**WHO should have caught it.** Same as W3.

**HOW to prevent.** Same as W3 (one combined edit).

**Severity: LOW.** Cosmetic.

### W5 — Adversary missed one thing the Judge then introduced

**WHAT.** Judge's synthesis (06-judge-r1.md:64-69) introduces `state["appended_hashes"]` as a content-hash dedup gate to `append_to_daily_log`. The Adversary's §"Approaches not proposed but worth considering" §2 (05-adversary-r1.md:707-712) is the source of this idea ("Append-side content-hash dedup"). The Adversary proposes it as orthogonal; the Judge adopts it into the chosen architecture.

But: the Judge places `state["appended_hashes"]` in `state.json` (FINAL-DESIGN.md:149-153), which the Adversary explicitly identified as having a **FATAL race** in C2: `batch-flush.py:454` writes `state.json` non-atomically. **The Judge's own synthesis inherits the bug it just rejected C2 for.** FINAL-DESIGN.md §"Open Questions" #1 (line 190) catches this honestly: *"Same `state.json` (touches the C2-FATAL non-atomic batch-flush write — must coordinate)? Or separate `appended_hashes.json` (cleaner, no contention)? **Recommendation: separate file, written atomically via `os.replace`.**"* Self-corrected, but the original Slice-2 sketch in FINAL-DESIGN.md:149 says `state["appended_hashes"]: dict[str, iso_timestamp]` — directly contradicting the recommendation 40 lines later.

**ROOT CAUSE.** Judge's Slice-2 sketch was written before the Open Questions section; the Open Question then identified the bug. The sketch was not updated. The synthesis-then-self-correct flow within a single artifact is healthy (Judge surfaced a hesitation it had) but the rendered artifact contradicts itself.

**WHO should have caught it.** Judge could have edited the Slice-2 sketch when writing Open Question #1.

**HOW to prevent.** SKILL.md §"Step 1d" should add: "If you surface a hesitation that contradicts your recommended design, update the design to resolve it. Do not leave both versions in the artifact."

**Severity: LOW-MEDIUM.** Slice 2 is deferred so the actual code change does not ship today. But if a future orchestrator reads Slice 2 of FINAL-DESIGN.md without reading Open Questions, they implement the buggy version.

### W6 — Effort estimates: Adversary's pessimism vs Judge's "honest middle"

**WHAT.** Adversary's `05-adversary-r1.md:745-756` re-estimates every proposal: A1 architect=S/M (45-60min), adversary=M (2-3h); B1 architect=M (4-6h), adversary=L (6-8h); C1 architect=M (60-90min), adversary=M (2.5-3h). Adversary concludes (line 758): *"Brief's '≤1 hour' budget is satisfied by NONE of the nine proposals once verified."*

Judge's response (06-judge-r1.md:113-115): *"My synthesis is honestly L-effort (4-6h), not the brief's '≤ 1 hour.'"* The Judge accepts the Adversary's pessimism but then offers an α-split: Slice 1 (1h, tactical observability fix) + Slice 2 (3-4h, deferred). User chose α-split.

The Slice 1 FINAL-PLAN.md is now 9 steps, ~140 LOC modified, 7 tests. That's still genuinely tight on a 1h budget but is the honest minimum that satisfies success criterion #4 ("FLUSH_ERROR writes always carry diagnostic info"). The split itself is the resolution — not "Judge over-promised" or "Adversary over-pessimized." Both were right.

**Severity: NONE.** This is the honest outcome. Calling it out only to register that the Judge resisted both sides of the dilemma cleanly.

### W7 — Nothing else found

Spot-checked verifiable claims throughout:
- SDK raise sites at `_internal/query.py:272,318,334,346,385,420,726` — all 7 verified by grep.
- `subprocess_cli.py:613-616` ProcessError hardcoded stderr — verified.
- 32 `session-flush-*.md` + 0 `flush-context-*.md` orphans — verified.
- Hook timeouts SessionStart=15s / PreCompact=10s / SessionEnd=10s — verified.
- `pyproject.toml` has no pytest — verified.
- `batch-flush.py:454` non-atomic write — verified.

Citation hygiene is **excellent** for the qualitative claims. The W2 issue is the only quantitative citation that does not survive re-verification.

---

## 3. Calibration Check (design-council specific)

### 3a — Architect Differentiation

SKILL.md §Phase 4 warns: *"If two architects' proposals are >70% similar, that's a prompt-design failure to surface."*

**Cross-architect distance test (pick 2-3 specific proposals):**

| Proposal | What it does | Where retry logic lives |
|---|---|---|
| **A1 — Sniff Table** | In-process classification of bare-Exception via `(class, message-pattern, exit-code)` table; retry inline in async loop | Inside `flush.py`, inside `run_flush()`, inside the `async for message in query(...)` call site |
| **B1 — Supervisor Wraps Flush** | New supervisor process re-spawns `flush.py` on transient exit codes; `flush.py` translates exception to int | Inside a NEW `flush-supervisor.py` process; classifier inside `flush.py:main()`; supervisor sees only ints |
| **C1 — Quiet Park** | Every failure parks to `scripts/parked/`; SessionStart drainer re-runs `flush.py` against parked files | Inside a NEW `scripts/drain.py`; classification is *replaced* by parking-everything + N-attempt budget; no in-process retry, no classifier |

These are **structurally different at three independent axes**:
- *Where does retry live?* (A=inline / B=parent process / C=separate drainer process on different hook)
- *What is the failure signal?* (A=Python exception object / B=subprocess exit code / C=file existence)
- *When does retry fire?* (A=immediately / B=within seconds of failure / C=on next user hook fire)

A strict reviewer would NOT call A1/B1/C1 >70% similar. They are fundamentally different bets. The hybrid synthesis (A1+C1, drop B1) is therefore "take the best of two genuinely-different layers", not "they were all the same anyway." **PASS.**

The within-architect variation (A1/A2/A3) varies the data structure (table vs if-tree vs two-stage). The within-architect variation (B1/B2/B3) varies where the retry-process lives (supervisor / per-hook-drainer / sync-in-hook). The within-architect variation (C1/C2/C3) varies the store (filesystem dirs / state.json / append-only JSONL). All three internal differentiations are real.

**Convergence verdict: PASS.** Three architects, three orthogonal perspectives, internally differentiated option sets, clean hybrid. Prompt design worked. Better than the prior run on this axis — in the prior run, C3's "C1 + on-demand MCP tool" was a soft variant of C1; here every option is genuinely independent.

### 3b — Adversary KEPT vs DISMISSED findings

The Judge's R1 (`06-judge-r1.md`) effectively rules on each Adversary finding. Sample 5 KEPT findings (judge upheld) + any DISMISSED findings.

**KEPT (Judge upheld):**

1. **B3 hook-timeout FATAL.** Adversary said `.claude/settings.json` has hook timeout 10s, B3's `TOTAL_BUDGET_S=90` will SIGKILL. Judge re-verified (06-judge-r1.md:4) and confirmed FATAL. **Real?** YES — I verified `settings.json` hook timeouts: SessionStart=15, PreCompact=10, SessionEnd=10. Adversary correct, Judge correct.

2. **A3 RotatingFileHandler multi-process race FATAL.** Adversary said each `flush.py` is its own process, the logger cache doesn't span them, rotation races corrupt the file. Judge upheld. **Real?** YES — `RotatingFileHandler` is documented as not multi-process safe; the architect explicitly acknowledged this in A3 Cons. Adversary correct.

3. **C2 state.json non-atomic write FATAL.** Adversary said `batch-flush.py:454` writes state.json non-atomically. Judge re-verified. **Real?** YES — verified at `batch-flush.py:454`: `STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")` — no atomic-replace pattern. Adversary correct.

4. **C3 manual-drain contradicts evidence FATAL.** Adversary said the user is the operator and 32 orphans have sat for 5+ weeks. Judge upheld. **Real?** YES — verified 32 `session-flush-*.md` files on disk, oldest dated 2026-04-14 per Architect C's claim. Empirical evidence supports the attack.

5. **C1 legacy migration prefix mismatch FATAL.** Adversary said `migrate_legacy_orphans` only globs `session-flush-*.md`; PreCompact uses `flush-context-*.md`. Judge confirmed (06-judge-r1.md:7). **Real?** YES — verified two distinct prefixes (`session-end.py:140` writes `session-flush-…`, `pre-compact.py:138` writes `flush-context-…`). Today 0 orphans of the second prefix exist; if PreCompact starts failing the bug bites. Adversary correct.

5/5 KEPT findings are real. **PASS.**

**DISMISSED (Judge downgraded or did not rule FATAL):**

1. **A1 `BaseException` catches `KeyboardInterrupt` / `SystemExit`.** Adversary called this SERIOUS (line 73-75). Judge demoted (06-judge-r1.md:22): *"Catching `BaseException` and swallowing SIGTERM is a 1-line fix (`except Exception`)."* **Real?** YES — Adversary's finding is correct (Python convention is `except Exception`). Judge's demotion is also correct — it's a 1-line fix, not a structural fatality. **DROP-DISAGREE? No.** Judge's call holds.

2. **A1 stderr_tail empty on fast-fail.** Adversary called this FATAL (line 54-59). Judge demoted to "limitation, mitigated by the `process_exit_1` rule covering 99% case." (06-judge-r1.md:22, "Empty-stderr-tail-on-fast-fail is real but the `exc_name='ProcessError', exit_code=1` rule covers the 99.3% case without needing the tail.") **Real?** YES — Adversary's finding holds (stderr task group is cancelled at `subprocess_cli.py:460` before exception completes). Judge's mitigation is also reasonable. The 99.3% denominator however is unverifiable (see W2). **DROP-DISAGREE? Mild.** The right verdict is "demoted but acknowledged" — Judge did this. Citation precision was sloppy (W2) but the substance of the demotion is sound.

3. **B1 effort estimate is wrong (claimed M, Adversary said L).** Adversary called this SERIOUS (line 252-263). Judge upheld (06-judge-r1.md:25): *"Honest effort: L (6-8h), not M."* **Real?** YES — Adversary's analysis of missing idempotency proof is correct. Judge propagated honestly.

3/3 sampled DISMISSED findings are correctly handled. **PASS.**

**No KEEP-DISAGREE or DROP-DISAGREE worth listing.** Judge's calls are calibrated.

---

## 4. Recurring Patterns

Prior design-council skill-feedback files found:
- `/home/faxik/w/autosorter/docs/plans/design-council-self-healing-entity-resolution/skill-feedback-2026-05-13.md` (4 days prior, the only prior design-council feedback file anywhere on disk)

### Patterns recurring from the 2026-05-13 run:

**Recurrence 1 — Quantitative citation slippage (W2 here = W2 in prior run).**

Prior run W2: *"Adversary slightly inflated writer-site count (HONEST OVERREACH). Adversary counted matches without de-duping protocol declarations from implementations."* — claimed 9 sites, actually ~13 hits / 7 production. Judge self-corrected.

This run W2: Judge introduced denominator "1648" that doesn't grep-reproduce. The numerator (1637) is verifiable; the denominator (1648) is not. Adversary's own count (163 FLUSH_ERROR / 162 exit-code-1) was clean.

**Same shape, different role.** Prior run had Adversary loose with counts; Judge cleaned up. This run has Judge introducing a number the Adversary did not. The cleanup direction reversed. **Recurring failure mode: quantitative precision drift across upstream/downstream agents.**

**Was Edit 1 from prior run applied?** YES — verified. SKILL.md lines 213-218 now contain the "ALSO: do not refer to sibling architects by name" patch. The patch is partially effective: this run's W1 (Architect C's "a sibling architect's text-pattern detection") is the same shape but milder — abstract sibling reference instead of named.

**Was Edit 2 (length budgets, MEDIUM) applied?** NO. Verified: `grep -n "length\|word budget\|≤1500" SKILL.md` returns nothing.

**Was Edit 3 (R2.1 patch round formalization, MEDIUM) applied?** NO. Verified: `grep -n "R2.1\|Patch Architect" SKILL.md` returns nothing.

**Was Edit 4 (worktree clarification, LOW) applied?** NO. Verified: `grep -n "general-purpose" SKILL.md` returns nothing.

### Patterns NOT recurring:

- **Architect cross-referencing by name** — prior run had B and C both naming "Architect A's witness primitive" by name. This run has Architect C with one abstract reference ("a sibling architect's text-pattern detection") — closer to compliant. Edit 1 is working.
- **Length blowout** — this run's total artifact size ~6,400 lines (smaller than prior run's ~13,000) because Round 2 was skipped and no Round 3 was needed. Skipping Round 2 was justified, not a length-discipline win, but the outcome happens to be smaller.

### New pattern not in prior run:

- **W5 (Judge synthesizes-then-self-corrects within one artifact, leaving both versions).** Did not appear in prior run because prior run had a full R2 + R2.1 patch cycle that resolved hesitations downstream. This run skipped R2, so hesitations surfaced in the same artifact that proposed them. SKILL.md does not require Judge to back-edit its own synthesis when it surfaces a contradiction in Open Questions.

### Forward implication

Edit 1 (the only HIGH-confidence patch from prior run) was applied and is partially working. Edits 2/3/4 (MEDIUM/LOW) were not applied. The "loop is closing" but slowly. The recurring quantitative-precision failure (W2 in this run, W2 in prior run) needs a new patch (see §5 Edit 2 below).

---

## 5. Proposed Skill Edits

### Edit 1 — Strengthen sibling-attribution prohibition (HIGH confidence)

**LOCATION.** `SKILL.md` §"Step 1b: Architects", around lines 213-218 (the existing "ALSO: do not refer to sibling architects by name" block).

**DIFF.**

Old (lines 213-218):
```
ALSO: do not refer to sibling architects by name (e.g. "Architect A's
witness primitive is premature"). Make your case on its own merits.
You may name and rebut alternative *approaches* in the abstract
("primitive-first risks X") but not as if you've read another
architect's specific proposal — because you haven't.
```

New:
```
ALSO: do not refer to sibling architects, either by name (e.g.,
"Architect A's witness primitive is premature") or by attributed
technique (e.g., "a sibling architect's text-pattern detection of
'rate limit' in stderr_tail"). Both are forms of cross-reading
attribution. Make your case on its own merits.

You may name and rebut alternative *approaches* in the abstract
("primitive-first risks X", "regex-on-stderr risks empty-tail")
without attributing them to a specific architect — because you
haven't read their proposal and shouldn't pretend to.

When proposing a hybrid extension, frame it generically: "this design
could compose with an in-process retry layer" — NOT "this could compose
with a sibling architect's retry layer."
```

**WHY.** §2 W1 and §4 recurring pattern — prior Edit 1 partially worked (no by-name references this run), but the same shape persists as "by-technique attribution" in `03-architect-c.md:909`. The patch closes the loophole.

**CONFIDENCE: HIGH.** Concrete observation, concrete textual fix.

### Edit 2 — Require quantitative citations to be re-greppable (HIGH confidence)

**LOCATION.** `SKILL.md` §"Step 1d: Judge", inside the agent prompt template (around lines 285-295).

**DIFF.**

Old:
```
For each proposal:
1. Does the adversary's attack hold up?
2. Are the fatal flaws truly fatal, or recoverable?
3. What's the real effort (not the architect's optimistic estimate)?
```

New:
```
For each proposal:
1. Does the adversary's attack hold up?
2. Are the fatal flaws truly fatal, or recoverable?
3. What's the real effort (not the architect's optimistic estimate)?

CITATION DISCIPLINE: Every quantitative claim in your output must be
either (a) a verbatim quote from an upstream agent's artifact with the
exact citation copied through, OR (b) a fresh grep / file-read that you
ran yourself and can show the command for. Do not silently re-aggregate
upstream counts. If you say "99.3% of FLUSH_ERROR lines", the denominator
must be a grep count you can reproduce, not a derived number.

If you find an upstream count that doesn't match your re-grep, FLAG IT
in your Hesitations section — don't silently substitute a corrected
number.
```

**WHY.** §2 W2 and §4 recurring pattern — both runs of design-council have shown quantitative-precision drift across upstream/downstream agents. This is a load-bearing claim ("any retry layer that doesn't have a rule for it is fiction" rests on the dominance of one error class). The patch makes the failure mode visible.

**CONFIDENCE: HIGH.** Concrete observation across two runs, concrete fix.

### Edit 3 — FINAL-PLAN.md prose-vs-code consistency check (MEDIUM confidence)

**LOCATION.** `SKILL.md` §"Phase 3: Produce Final Deliverables", after the `FINAL-PLAN.md` template (around line 502).

**DIFF.** Add after the FINAL-PLAN template:

```
### Pre-finalization consistency scan

Before reporting done, scan FINAL-PLAN.md for prose-vs-code mismatches:

1. Test framework: does the prose's "uses pytest / unittest / X" match
   the imports and base classes in code blocks?
2. Test count: does any "N tests" claim in prose match the actual number
   of test methods / functions in the code block?
3. File paths: does every file path in prose ("modify scripts/foo.py")
   appear in at least one code block as `# file: scripts/foo.py` or
   equivalent path indicator?
4. Dependency count: does any "no new dependencies" claim survive a grep
   of the code blocks for new `import` statements not already in
   `requirements.txt` / `pyproject.toml`?

If any mismatch, edit the prose to match the code (the code is source
of truth for what will actually ship).
```

**WHY.** §2 W3 (test class is `TestCase` but prose claims `IsolatedAsyncioTestCase`) and §2 W4 (table says "4 tests", strategy says "7 tests", code has 7). These are cosmetic but erode trust.

**CONFIDENCE: MEDIUM.** The fix is mechanical; the risk is that an orchestrator under time pressure skips the scan.

### Edit 4 — Judge must back-edit synthesis when surfacing contradictions (MEDIUM confidence)

**LOCATION.** `SKILL.md` §"Step 1d: Judge", inside the agent prompt template (around lines 297-310).

**DIFF.**

Old (line 326 area, the Hesitations subsection):
```
## Hesitations
<Anything that makes you uncertain — be transparent>
```

New:
```
## Hesitations
<Anything that makes you uncertain — be transparent>

CRITICAL: If a hesitation contradicts your recommended design (e.g.,
"my synthesis uses state.json BUT state.json has a non-atomic-write
race in batch-flush.py"), you must update the design above to resolve
the contradiction. Do not leave both versions in the artifact. Either
fix the design, or downgrade your verdict from VIABLE to FLAWED-BUT-
FIXABLE with the resolution as the fix.
```

**WHY.** §2 W5 — Judge proposed `state["appended_hashes"]` in `state.json`, then in Open Questions identified that this re-introduces C2's FATAL race, then recommended a separate file in Open Questions, but left the original (buggy) sketch in the design body. A reader who skims the design body without reading Open Questions implements the bug.

**CONFIDENCE: MEDIUM.** Real observation; the patch may be too strict in cases where the contradiction is genuinely "I'm flagging both options for the user to choose between."

### Edit 5 — Carry-forward (LOW confidence, optional)

The prior run's proposed Edit 2 (length budgets) and Edit 3 (formalize R2.1 patch round) and Edit 4 (worktree clarification) were NOT applied between 2026-05-13 and now. This run:
- Did NOT have a length problem (artifacts smaller, no R2 / R3 / R2.1 needed). Edit 2 still defensible but pressure low.
- Did NOT need R2.1 (skipped R2 entirely; R2.1 formalization not load-bearing this run). Edit 3 still defensible but pressure low.
- Did NOT have a worktree issue visible in artifacts. Edit 4 still LOW-confidence as before.

**Recommendation:** Apply Edit 1 (HIGH) and Edit 2 (HIGH) this round. Defer Edits 3 (MEDIUM) and 4 (MEDIUM) — the underlying observations are real but low-frequency. Edit 5 from prior run (length, R2.1, worktree) remains optional carryover.

---

## 6. Meta-grade

**GREEN.**

- Contract: A. Every phase delivered or justifiably skipped with explicit reasoning.
- Citations: A- (qualitative), B+ (quantitative — one unverifiable denominator).
- Architect differentiation: A. Three orthogonal perspectives, internally differentiated options, clean hybrid.
- Adversary verification: A. Spot-checked 5 claims, all 5 hold up against the live codebase.
- Judge quality: A. Acts as advisor (named α/β dilemma, 6 hesitations, propagated user decision).
- Round-2 skip: Defensible. SKILL.md explicitly allows it when synthesis is clear; user's α-split converts Slice 1 into a fully-specifiable single scope.
- Prior Edit 1 (sibling naming) is working — recurring failure mode is milder this run.

Two minor blemishes (W2 unverifiable denominator, W3+W4 prose-vs-code drift in FINAL-PLAN.md) keep this from being a pure A. But they are exactly the kind of issue the loop is designed to catch and correct via §5 patches.

**This is the best design-council run captured in the feedback record so far.** Better calibrated than the 2026-05-13 run; smaller artifact pile; cleaner Adversary citations.

---

## 7. Carry-Forward

**For the next design-council run:**

1. **Edit 1 from prior run (sibling-by-name prohibition) is partially effective.** Apply the strengthened version in this run's §5 Edit 1 to close the "by-technique" loophole.

2. **Quantitative citations are the recurring failure mode.** Two runs in a row have shown count-drift between upstream and downstream agents. Apply this run's §5 Edit 2 (citation discipline) to make the failure mode visible.

3. **Round-2 skip precedent established.** This run skipped R2 because the synthesis was clear AND the user's α-split decomposed the scope. This is consistent with SKILL.md §"Common Mistakes" but the precedent is now recorded: when Judge says ALMOST-READY but user picks a tractable slice, the slice can ship without R2. Don't generalize — this works because Slice 1 is genuinely tactical (single function, single file, no new abstraction).

4. **The compiler-resilience workspace at `~/tools/claude-memory-compiler/`** is the next likely council site for Slice 2 (3-4h, full A1+C1 synthesis). Future council should:
   - Read the live `daily/2026-05-*.md` corpus for grounded stderr signatures BEFORE drafting the CLASSIFICATIONS table.
   - Treat FINAL-DESIGN.md §"Open Questions" as the seed brief, not just appendix.
   - Resolve the `state["appended_hashes"]` location question (separate file via os.replace per W5).

5. **The SDK's untyped error surface is the load-bearing fact.** Any future council on retry/resilience for this codebase must read the brief's §"What the SDK actually raises" section. The `claude_agent_sdk` does NOT have a typed error hierarchy. Pretending it does is what put the original plan in design council.

6. **Loop status: GREEN with traction.** Prior run had no feedback predecessor; this run has one. The recurring-pattern detector now has signal. Next run (3rd) should be able to detect whether Edit 1 v2 and Edit 2 actually closed the failure modes.

---

**File written to:**
`/home/faxik/tools/claude-memory-compiler/docs/plans/design-council-compiler-resilience/skill-feedback-2026-05-17-design-council-compiler-resilience.md`
