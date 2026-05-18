# R1 Defender response — adversarial review of compiler-followups-v1

Verdict-per-finding against the real codebase. Evidence inline.

---

## FATAL

### F1: CONCEDE
**Evidence:**
- `wc -l scripts/compile.py` = 224 lines.
- `grep -nE 'two[-_ ]?pass|dedup|hash[-_ ]?eq|early[-_ ]?return|content_hash|idempot' scripts/compile.py` returns NO matches.
- Only ONE `query(` call in `scripts/compile.py` (one prompt, one LLM round-trip).
- The hash check is the `to_compile` selection predicate: `if not prev or prev.get("hash") != file_hash(log_path): to_compile.append(log_path)`. This is "skip files that haven't changed since last compile" — NOT a "hash-equality idempotency gate" that retires per-article compile cost, NOT a two-pass dedup pass that retires input tokens for unchanged articles.
- The May-13 retro gap #3 ("compile cost scales linearly with KB size — all 200 articles re-sent every compile") is genuinely unresolved: lines 53-63 still pump the full `existing_articles_context` into every prompt.

The brief's premise is false. Slice 2 shipped resilience + observability; it did NOT retire compile cost.

**Fix:** Rewrite `00-problem-brief.md` §"Problem statement" first paragraph:
- Replace "The compile cost problem was retired by the two-pass dedup work in compile.py plus the hash-equality idempotency gate" with: "The file-level hash check in `compile.py:main()` skips unchanged daily logs, but per-article compile cost still scales linearly with KB size (full `existing_articles_context` sent every compile). Cost reduction is OUT OF SCOPE for this sprint — see 'Out of scope' deferral."
- Re-enumerate the May-13 gaps as **5** (gap #3 = compile-cost), of which 4 are in-scope this sprint and #3 is explicitly deferred.
- This also resolves W1.

State.json data backing the cost concern: 52 historical compiles, mean $4.48, max $11.88, total recorded $232.74 — confirms gap is real.

---

### F2: CONCEDE
**Evidence:**
- `find knowledge/connections/ -size 0 | wc -l` = `0`.
- 19 files, total 74.7KB. Includes substantive articles e.g. `sa-core-migration-and-database-mixin-boundary.md`, `worktree-development-lifecycle-automation.md`.
- The brief's claim "knowledge/connections/ has 0-byte placeholders" is FACTUALLY WRONG today.

The retro lie was inherited verbatim. Connections-synthesis fires today; the directory is not empty.

**Fix:**
1. `00-problem-brief.md:12,43` — replace "0-byte placeholders" / "every file is 0 bytes" with: "`knowledge/connections/` has 19 articles totalling 75KB BUT no governance — `compile.py` produces connections opportunistically without a guard rail on quality, format, or per-run count. Gap is now 'connections-quality + per-run cap', not 'empty dir.'"
2. `COMPLETION-CHECKLIST.md:T2.A.1` — change assertion from "0-byte placeholder files are deleted" to "the empty-dir guard in compile.py (if it exists) is removed OR replaced with a 'has-at-least-N-existing-concepts' precondition." Verify by `grep -n "connections.*0\|empty" scripts/compile.py` returning no surviving early-return.
3. T2.A.4 test renamed `test_connections_synthesis_respects_per_run_cap` — verifies the cap, not the no-longer-existent skip path.
4. The "Known context" line in the brief saying "every file is 0 bytes" deleted.

---

### F3: CONCEDE
**Evidence:**
- `grep -c 'query(' scripts/compile.py` = 1. `grep -n 'query(' scripts/compile.py` returns one site (the `async for message in query(...)` call inside `compile_daily_log`).
- One prompt, lines ~67-127.
- "pass-2 prompt" referenced in T2.A.2 and T2.A.4 doesn't exist.

**Fix:** Choose ONE of two paths in `COMPLETION-CHECKLIST.md`:
- **Path A (recommended, smaller scope):** rename T2.A.2 to "compile.py's SINGLE prompt is extended with a `connections/` rules block instructing the LLM..." — keep one query() call, modify the existing prompt.
- **Path B (larger scope, only if Path A's quality is insufficient):** introduce a real pass-2 via a second `query(...)` call after the concept pass returns; this is materially larger work and would require splitting T2 into T2.A.PROMPT (extend existing) + T2.A.PASS2 (architecture change). NOT recommended for a 3-5d sprint.

Also fix the brief's "Known context" line referencing "Pass-2 prompt has Read/Write/Edit/Glob/Grep tool allowlist" — there is no pass-2; that allowlist is on the SINGLE pass. Reword to "The compile prompt has..."

---

### F4: PARTIAL
**Evidence:**
- Slice 1 (`26bbaa6 feat(flush): structured FLUSH_ERROR with exit_code + stderr_tail (Slice 1)`) shipped before today; today is 2026-05-18.
- Inside `scripts/flush.log`: 163 lines matching `FLUSH_ERROR`, BUT `grep -cE 'FLUSH_ERROR.*exit_code='` = 0. The Slice-1 format does NOT put `exit_code=` on the same line as `FLUSH_ERROR` (likely structured-log line with newlines or a different key).
- Adversary is correct that T0.C.0 cannot pass against the CURRENT flush.log if the grep predicate is the literal `FLUSH_ERROR.*exit_code=` regex on the same line.
- Adversary is INCORRECT about "Slice 1 shipped 2026-05-17" — git log shows Slice 1 in earlier commits, with format `structured FLUSH_ERROR with exit_code + stderr_tail`. But the regex doesn't match.

**Defense partial:** T0.C is gated. `T0.C.1` already says "If `grep -c "FLUSH_ERROR.*exit_code=" scripts/flush.log` ≥ 100 entries spanning ≥7 days" — that's an "if" guard, and T0.C.2 says "If <7d evidence available at sprint close: a `followups.md::FU-FU1-T0-C` entry exists with refined-regex-after-N-days deferral rationale" — so the deferral path IS planned. The sprint can ship without satisfying T0.C.1.

BUT T0.C.0 is marked Blocking=Y and its verify command is `python tools/check_classifier_groundedness.py` exit 0. That script reads flush.log; if its parsing regex is the same broken one, the script will (correctly) report 0 grounded patterns and we'd then need the speculative-comment fallback to satisfy exit 0.

**Fix:**
1. T0.C.0 verify command: clarify the script's intent — it should EITHER find grounded patterns OR ensure each speculative regex in `classifier.py` has a `# UNVERIFIED — speculative` comment. The current row text already says that, but make the EXIT-0 path on day-1 explicit: "if 0 grounded patterns found, exit 0 IFF every regex in CLASSIFICATIONS is annotated `# UNVERIFIED`."
2. T0.C.0 ALSO inspect the actual flush.log line format BEFORE writing the script. Add a first sub-step: `python -c "import re; lines=open('scripts/flush.log').readlines(); print([l for l in lines[:20] if 'FLUSH_ERROR' in l])"` to determine the real line format, then build the regex against ground truth.
3. Mark T0.C.1 explicitly Blocking=N (it's already N, but call it out in the brief's "Constraints" so nobody promotes it).

---

### F5: PARTIAL
**Evidence:** I ran the regex behavior on a probe file:
- `grep -E "session_id\|flushed_at\|confidence" file` → ZERO matches (the `-E` mode interprets `\|` as literal pipe).
- `grep "at most ONE\|max=1\|single connection" file` (BRE, no `-E`) → matches all alternatives correctly (GNU grep BRE treats `\|` as alternation).

So adversary is **half right**: the bug applies ONLY to rows that use `grep -E`. BRE rows (no `-E`) are fine.

Auditing each cited row:
- T0.A.3: `ls scripts/flush.log* \| wc -l \>= 1` — no grep at all. The `\|` and `\>=` are MD-table escapes leaking into shell. The `ls ... | wc -l >= 1` form isn't shell-valid; should be `[[ $(ls scripts/flush.log* | wc -l) -ge 1 ]]`. **Bug confirmed.**
- T0.E.1: `tools/check_telemetry_span.sh` exit code — no grep. False alarm.
- T1.A.2: `grep -E "session_id\|flushed_at\|confidence"` — **bug confirmed.** Needs `grep -E "session_id|flushed_at|confidence"`.
- T1.B.2: `python tools/check_evidence_blocks.py knowledge/concepts/` — no grep. False alarm.
- T2.A.3: `grep -n "at most ONE\|max=1\|single connection"` — BRE, works correctly. False alarm.
- T3.A.1: `grep -n "User.*\\?\|extract_questions"` — BRE, works. False alarm.
- T3.B.2: `head -10 /tmp/test-qa.md \| grep "question_hash\|asked_at\|confidence"` — BRE inner, works; OUTER `\|` is shell-pipe escape leak (same as T0.A.3 class).

**Fix:**
1. T1.A.2 verify: replace `grep -E "session_id\|flushed_at\|confidence"` with `grep -E "(session_id|flushed_at|confidence)"`.
2. T0.A.3 and T3.B.2 outer verify expressions: replace `\|` with a literal `|` (it's a shell pipe, not a regex token); replace `\>=` with `-ge` in a `[[ ]]` test or drop the `>=1` and just rely on `test -s`.
3. Author note in COMPLETION-CHECKLIST.md preamble: "Verify commands are bash-as-pasted, NOT markdown-table-escaped. Pipes are literal `|`; alternation in `grep -E` is `|`; alternation in BRE `grep` is `\|`."

---

### F6: CONCEDE
**Evidence:**
- state.json: 52 historical compiles, mean $4.48, max $11.88, total $232.74. T-FINAL.6's "< $5 for the run" is below the historical mean.
- §6 of team-of-agents claims "Dogfood compile: ~$0.50 max (per the cost-budget circuit breaker T0.D will install)" — but the breaker is per-day at $5, NOT per-compile. A single compile averaging $4.48 would consume the whole daily budget.
- Sprint hard cap $10 buys ~2 historical-average compiles, NOT the 5-track Opus run pictured in §7 ("max 4 Opus workers concurrent").

Three internal contradictions:
1. T-FINAL.6 "< $5/run" vs historical $4.48 mean (1.0× ratio, almost certainly trips).
2. T0.D.3 "$5/day cap" vs typical $4.48 compile (one compile burns the whole day).
3. §6 "$10 total LLM spend" vs §6 "max 4 Opus workers concurrent" (4 Opus workers × any non-trivial task each blows $10).

**Fix:**
1. T-FINAL.6 dogfood: drop the "< $5 for the run" wording; replace with "user signs off at CHECKPOINT-D that the compile produced sensible output AND that cost was within their tolerance (state cost in dollars; do not hardcode a threshold)." Cost-threshold tuning is post-sprint work after T0.D telemetry exists.
2. T0.D.3 default cap raised to $15/day (or made explicit-default `state["daily_retry_cost_cap"]`, default 15.0, with the rationale that historical mean-compile is $4.48 and the cap should permit ≥2 retries per day).
3. §6 budget: re-scope to "$25 total LLM spend" OR commit to "compile-only no-rerun dogfood at CHECKPOINT-D, all worker tasks are non-LLM" (which is realistic since R1-R5 are code/test changes, not LLM-call code). The 4-Opus-workers number applies to ORCHESTRATION concurrency, NOT to in-task LLM spend; restate to clarify.

---

## SERIOUS

### S1: DEFEND
**Evidence:** §10 of `team-of-agents-v1.md`:

> "Until `tools/check-checkpoint-artifact.sh` exists, no `checkpoint-A-applied.json` artifact needed for THIS sprint — Orchestrator will surface CHECKPOINT-A questions live to user (the canonical path; artifact path only exists for violations)."

**Defense:** The skill (`autonomous-sprint`) requires the artifact when the harness is enforcing it. We grep'd `/home/faxik/.claude/skills/autonomous-sprint/` and found no enforcement file. The artifact exists to PROVE user-checkpoint compliance to a checker tool; the canonical compliance path is the user directly responding to CHECKPOINT-A questions. §10 already documents the deferral with a "when X tool exists, switch to artifact" clause. This is appropriate scope-management, not skill-violation.

That said, ONE concession: §10's language "no `checkpoint-A-applied.json` artifact needed" is too broad. **Fix (clarification, not change):** Add to §10: "If `tools/check-checkpoint-artifact.sh` lands MID-SPRINT, the Orchestrator backfills `checkpoint-A-applied.json` from the conversation transcript before the next wave dispatches. No checkpoint compliance is skipped, only the artifact format is deferred."

---

### S2: PARTIAL
**Evidence:** T2.A.2's prompt rule text (in the COMPLETION-CHECKLIST.md row) does NOT reference `evidence:`. T1.B.1-T1.B.3 say compile.py's prompt instructs `evidence:` emission. These are two separate prompt-edit asks on the same prompt by two different implementers (R2 and R3).

**Defense:** The wave plan in §7 puts T1 and T2 in Wave 1 in parallel because their CHANGES are disjoint at the spec level. They are NOT spec-coupled. They ARE merge-coupled — both edit the same prompt string. That's a Wave-2 integration concern (handled by R6 integrator forward-merging).

**Concession:** The "T2 depends on T1" framing if it appears anywhere should be removed. Let me check the actual text — §7 sequencing says "Wave 1: T1, T2, T3, T4 in parallel" — no T2→T1 dependency stated. So adversary's claim that the dependency is "fictional" is correct in the sense that NO dependency is asserted in the plan. Adversary is then arguing that R6 integration risk between T1+T2 prompt edits isn't called out.

**Fix:** §7 add: "**Prompt-edit interleave risk:** R2 (T1.B.1, T1.B.3) and R3 (T2.A.2) both modify the compile-prompt text. Integrator R6 must forward-merge T1 first, then T2, and re-run `scripts.test_classifier scripts.test_dedup` after each integration. If conflict, prefer T1's structure as the outer scaffold (it's earlier in the prompt) and T2 as a `## Cross-concept connections` subsection."

---

### S3: CONCEDE
**Evidence:** T-FINAL rows have `Owner=R0` (Orchestrator) in COMPLETION-CHECKLIST.md. §1 role catalogue: R0 owns "Dispatch, merge, codesweep, user-facing checkpoints" — explicitly NOT implementation/test-authoring.

`tools/check_followups_v1.py` is a real script that needs to be authored. R0 (Orchestrator) doesn't author code.

**Fix:** Reassign T-FINAL.1, T-FINAL.2, T-FINAL.3, T-FINAL.4, T-FINAL.5 ownership to **R-FINAL** — a Sonnet-tier ephemeral integrator role spawned post-Wave-1. T-FINAL.6 (dogfood) stays with R0 because it's user-facing. Update §1 role catalogue:
```
| **R-FINAL** | Acceptance test author | Sonnet | tools/* Write + Bash | tools/check_followups_v1.py + the 4 sub-checks; dogfood orchestration | T-FINAL.1-5 |
```
Bump worker-staggering math accordingly (Sonnet is unmetered, so no concurrency cap impact).

---

### S4: CONCEDE
**Evidence:** T-FINAL.3 ("included in T-FINAL.2 exit"), T-FINAL.4 ("counted as part of T-FINAL.2"), T-FINAL.5 ("T-FINAL.2") all defer their exit code to T-FINAL.2's single binary. If T-FINAL.2 fails, no granular breakdown.

**Fix:** Each of T-FINAL.3/4/5 gets its OWN sub-check inside `tools/check_followups_v1.py`:
- `check_polish_items()` (T-FINAL.2)
- `check_structural_gaps()` (T-FINAL.3)
- `check_unit_tests()` (T-FINAL.4)
- `check_lint_clean()` (T-FINAL.5)

Each returns its own non-zero exit code on failure. The script prints per-check verdict before final exit. Verify row text updated to: "`python tools/check_followups_v1.py --check structural-gaps` exit 0" for T-FINAL.3, etc.

---

### S5: CONCEDE
**Evidence:** T1.B.2 verify is `python tools/check_evidence_blocks.py knowledge/concepts/`. Row says "every article ... modified during this sprint." No predicate defined for "modified during this sprint." `knowledge/` is gitignored (we confirmed it's a flat md tree, not a tracked git tree — `git diff` shows no knowledge changes in recent commits while files clearly are being added).

**Fix:** T1.B.2 either:
- **Strict path (recommended):** drop "modified during this sprint" entirely. After this sprint, ALL articles must have `evidence:` (via the T1.C backfill). So `tools/check_evidence_blocks.py knowledge/concepts/` exit 0 means "every concept has evidence." Backfill closes the legacy gap.
- **Surgical path:** add an `--since YYYY-MM-DD` flag and compare file mtimes against sprint open date (2026-05-18). Mtime is unreliable when files are LLM-edited (Claude Code Write tool updates mtime), so this is fine as a soft signal.

Strict path eliminates the "modified during this sprint" predicate problem entirely. Go strict.

---

### S6: CONCEDE
Already addressed in F6 fix. The $5/day breaker + $10 sprint cap + $5 dogfood is internally inconsistent against historical compile cost. Fix per F6.

---

## WEAKNESS

### W1: CONCEDE
Already addressed in F1 fix — reframe May-13 gaps as 5 (not 4), with gap #3 explicitly deferred to "Out of scope."

### W2: PARTIAL
**Evidence:** §1 explicitly says "No QA tier. All tests are unit-level + dogfood. No real-LLM acceptance run." T2 (connections-synthesis) and T3 (QA loop) are LLM-call tracks that ship with only mocked unit tests + dogfood at CHECKPOINT-D.

**Defense:** This is a known constraint, called out explicitly. Dogfood IS the integration test. Adding a QA-tier real-LLM run would double sprint cost and timebox. For a 3-5d sprint, dogfood + mocked unit is acceptable.

**Partial concession:** Add to §5 convergence criteria: "Per-track-with-LLM-call done ALSO requires CHECKPOINT-D dogfood to specifically exercise the new LLM path (T2 synthesis dispatched on a real recent daily; T3 query on a real recent session question). Recorded in `status/dogfood-evidence.md`."

### W3: PARTIAL
**Evidence:** T3.A.3 says ≤100ms target, but verify is `time (echo '{}' | uv run python hooks/session-start.py > /dev/null) < 1s`. 1s already has ~10× margin over 100ms.

**Defense:** The header claim "100ms latency ceiling" is aspirational; the actual verify command uses 1s. The 1s budget includes Python startup overhead and three Popen spawns of `query.py`, which are fire-and-forget (so the parent returns immediately after spawn). 1s is realistic.

**Partial fix:** Rewrite T3.A.3 assertion text from "stays ≤ 100ms" to "stays under 1s wall (Python startup + 3 fire-and-forget Popens of query.py)." Matches the verify command. Drop the 100ms aspiration entirely.

### W4: CONCEDE
**Evidence:** `followups.md` is empty at sprint open. The brief uses "followups" for both (a) round-3 deferrals and (b) exec-time discoveries.

**Fix:** Stamp `followups.md` at sprint open with two H2 sections:
```
## Brief-time deferrals (from round-3 adversarial review)
(empty — this sprint is the dispositions; nothing else carried forward)

## Exec-time discoveries (added during Wave 0-1)
- FU-FU1-T0-C: classifier regex refinement deferred pending ≥7d telemetry. Re-trigger: when grep -cE 'FLUSH_ERROR.*exit_code=' scripts/flush.log ≥ 100.
```
Pre-populate FU-FU1-T0-C as a known deferral path so T0.C.2 row passes day-1.

### W5: PARTIAL
**Evidence:** Current KB has 0 dead links and 1 orphan (T4.B.2 + T4.C canary tests would still verify the detection logic against synthetic inputs).

**Defense:** T4.B.2 explicitly says "temporarily add `[[concepts/does-not-exist]]` to a test article; lint exits 1; revert." Test uses SYNTHETIC input. T4.C.3 says "test_orphan_detection covers a synthetic 3-article KB with one orphan." Both work against synthetic test inputs, not the live KB. So the "zero existing work" framing is misleading — the detection logic gets tested deterministically.

**Partial concession:** The T-FINAL acceptance line "lint_kb.py catches 4 canary defects" — define the 4 canaries explicitly: (1) missing frontmatter, (2) malformed frontmatter, (3) dead wikilink, (4) orphan article. Without that, "4 canary defects" is ambiguous. Update T-FINAL.3.

### W6: CONCEDE
**Evidence:** T1.A.2 row Verify: `grep -E "session_id\|flushed_at\|confidence" docs/plans/compiler-followups-v1/evidence-schema.md` — does NOT include `claim_summary`. T1.A.2 Assertion text DOES say "Optional: `claim_summary`." So required-list is `session_id, flushed_at, confidence`; `claim_summary` is genuinely optional. No inconsistency — adversary misread.

**Fix:** None for THIS inconsistency. But T1.A.2 verify regex IS broken per F5 fix.

### W7: PARTIAL
**Evidence:** §6 says "max 4 Opus workers concurrent." §7 Wave 0 = R1 polish; Waves 1 = T1+T2+T3+T4 in parallel = R2+R3+R4+R5 = 4 Opus workers.

**Defense:** Wave 0 (R1 only) = 1 Opus. Wave 1 = R2+R3+R4+R5 = 4 Opus, AT the cap. AoT (R7) is ephemeral, fires only post-integration on each commit (serially, not in parallel with implementers). So peak concurrent is 4, matching the cap exactly.

**Partial:** §7 explicit math: "Wave 0 peak = R1 (1 Opus). Wave 1 peak = R2-R5 (4 Opus, at cap). AoT (R7) is post-integration, serial. R6 integrators are Sonnet (unmetered)."

---

## NITPICK

### N1: CONCEDE — Row count says 60; recount inside the doc:
- T0: 4+3+3+5+2+2 = 19 ✓
- T1: 3+4+4 = 11 ✓
- T2: 4+2 = 6 ✓
- T3: 3+5 = 8 ✓
- T4: 3+2+3+2 = 10 ✓
- T-FINAL: 6 ✓
- Total: 60 ✓. Math is correct. **Defend.** No fix.

(Wait — T0 contains T0.A=4, T0.B=3, T0.C=3, T0.D=5, T0.E=2, T0.F=2 = 19. But T0.A in the checklist actually has 4 rows (A.1, A.2, A.3, A.4), T0.C has 3 (C.0, C.1, C.2), T0.D has 5 (D.1..D.5). So the breakdown matches.) DEFEND N1.

### N2: PARTIAL
T-FINAL.4 references `scripts.test_log_rotation` which doesn't exist yet (created in T0.A.4). Forward-reference inside an acceptance command is fine for THIS sprint (the test will exist before T-FINAL.4 runs), but the listing should note "T0.A.4 must ship before T-FINAL.4 can run." Add to §7 sequencing.

### N3: ACK — adversary didn't specify which term; can't fix without text. Defer.

### N4: ACK — §7 wave sequencing is explicit (Wave 0 → Wave 1 → Wave 2). Defend.

### N5: CONCEDE — `status/budget-meter.json` referenced in §6 but no bootstrap step writes the initial `{"total_spent_usd": 0.0}`. Add to §7 Bootstrap sequence: "Step 0.5: Orchestrator initializes `status/budget-meter.json` with `{\"total_spent_usd\": 0.0, \"created_at\": now()}`."

---

## Net assessment

**F1, F2, F3, F6 all hold and are substantive.** The brief is built on three factually wrong claims (compile-cost retired, connections empty, two-pass exists) and one cost-budget inconsistency. The brief needs a rewrite of:
- §"Problem statement" para 1 (F1, F2)
- §"Known context" lines about connections + pass-2 (F2, F3)
- §"Success criteria" #3 cost threshold (F6)
- T2.A.1, T2.A.2, T2.A.4 (F2, F3)
- T-FINAL.6, T0.D.3, §6 (F6)

The 4 unfinished structural gaps from May 13 are actually 5 — gap #3 (per-article compile cost) is unresolved and should be either (a) explicitly added to Out of scope, OR (b) added to the sprint with a real fix (likely (a) given timebox).

**F4 and F5 are real but smaller** — fixable with verify-script clarifications and regex audit pass.

**S1 defended** — the deferred-artifact pattern is documented and bounded.
**S3 conceded** — Orchestrator can't own implementation rows. New R-FINAL role.
**S2/S4/S5/S6 conceded** with concrete fixes.

**Recommendation: SUBSTANTIVE REWRITE of brief + checklist before Wave 0 dispatch.** F1/F2/F3/F6 together change the sprint's framing, scope, and budget. Don't paper over them.
