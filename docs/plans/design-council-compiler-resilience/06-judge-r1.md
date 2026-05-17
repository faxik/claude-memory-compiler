# Judge's Recommendation — Round 1

> Verifications I ran before ruling (so this isn't just refereeing the adversary):
>
> - `.claude/settings.json` — hook timeouts confirmed at **SessionStart=15s, PreCompact=10s, SessionEnd=10s**. The adversary's "B3 is FATAL" stands.
> - `pyproject.toml` — `dependencies = ["claude-agent-sdk>=0.1.29", "python-dotenv>=1.0.0", "tzdata>=2024.1"]`. **No pytest, no pytest-asyncio.** Architect A's test surface violates constraint #6 as written.
> - `ls scripts/session-flush-*.md | wc -l` → **32**. `ls scripts/flush-context-*.md` → **0** (zero matches). C1's `migrate_legacy_orphans` glob mismatch is confirmed (today silent; the moment PreCompact starts failing it loses work).
> - `hooks/session-end.py:140` writes `session-flush-…`; `hooks/pre-compact.py:138` writes `flush-context-…`. Two distinct prefixes confirmed.
> - `subprocess_cli.py:613-616` — `ProcessError(...stderr="Check stderr output for details", ...)` hardcoded. Diagnostic value of `getattr(exc, "stderr", "")` is genuinely zero. Architects B1/C1/C2/C3 that read this attribute will capture only the boilerplate string.
> - `batch-flush.py:454` — `STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")`. Non-atomic. C2 FATAL stands.
> - `grep "exit code 1" scripts/flush.log | wc -l` → **1637 / 1648 = 99.3%** of all FLUSH_ERROR lines. The "process_exit_1" pattern dominates; any retry layer that doesn't have a rule for it is fiction.
> - `hooks/session-end.py:164-165` + `hooks/pre-compact.py:160-161` — both pipe to `subprocess.DEVNULL`. Silent-stderr complaint is real.
>
> The adversary's evidence holds. I'm going to treat their FATAL/SERIOUS findings as load-bearing unless I call out a demotion.

---

## Ruling on Each Proposal

| Proposal | Verdict | Key Reason |
|---|---|---|
| **A1 — Sniff Table** | FLAWED-BUT-FIXABLE | Pytest-dep is a one-evening rewrite to `unittest.IsolatedAsyncioTestCase`. Empty-stderr-tail-on-fast-fail is real but the `exc_name="ProcessError", exit_code=1` rule covers the 99.3% case without needing the tail. Catching `BaseException` and swallowing SIGTERM is a 1-line fix (`except Exception`). Recoverable. |
| **A2 — Inline Heuristic** | FLAWED-BUT-FIXABLE | Same pytest-dep issue. The "if-tree implicit ordering" risk is real but is the same shape as A1's table-ordering, just less ceremony. Loses to A1 only on auditability and on the `caller=` provenance axis. |
| **A3 — Two-Stage Triage** | REJECTED | The multi-process `RotatingFileHandler` race the adversary identified is genuine and structurally hostile to the design's main selling point (the diag log). Each `flush.py` invocation is its own process; the handler cache doesn't span them; rotation races corrupt the file. "Fix it" means replacing the rotating-handler with a single-writer pattern, which removes the design's appeal. Demote to A1 if observability is wanted. |
| **B1 — Supervisor Wraps Flush** | VIABLE-WITH-CAVEATS | The strongest *architectural* move (SDK-agnostic retry via exit codes). The adversary's `start_new_session` fix is real but is a 1-line change. The serious issue is the **server-side-success / client-side-failure rebill** — B1 cannot distinguish "stream died after model already generated" from "stream died before model started." Honest effort: L (6-8h), not M. |
| **B2 — Hook Triggers Drainer** | FLAWED-BUT-FIXABLE | The drainer race is real, the `.inflight` rename mitigation is well-understood, the daily-log FLUSH_ERROR bleed is a 1-line fix. The 24h × 16-retry budget on a poison-pill is genuinely expensive. Latency is unbounded — fails success criterion #1 ("absorb the cluster") if the user closes Claude Code for hours. |
| **B3 — Sync Hook With Tight Budget** | REJECTED | Adversary nailed it: `.claude/settings.json` says 10s. `TOTAL_BUDGET_S=90` is fantasy. The hook gets SIGKILL'd at 10s and the entire retry + fallback-write becomes unreachable code. The architect did not verify the timeout. Dead. |
| **C1 — Quiet Park** | VIABLE-WITH-CAVEATS | The legacy-glob prefix mismatch is mechanical (1 LOC). `last_exception_summary` not being implemented is mechanical (10 LOC). The `FLUSH_FROM_DRAIN` env check missing in flush.py is mechanical (5 LOC). The genuinely-novel move ("retry-count IS the discrimination") is sound and sidesteps the SDK-error-classification problem cleanly. Honest effort: M (2.5-3h). |
| **C2 — State.json Queue** | REJECTED | The `batch-flush.py:454` non-atomic write is verified. `compile.py` also writes state.json. Making the queue safe requires retro-fitting cross-platform locks across 4 writers, none audited by this proposal. Lock-on-crash deadlock has no graceful recovery. The "single source of truth" appeal collapses once you price in the multi-writer audit. |
| **C3 — Append-Only Journal** | REJECTED | The empirical evidence is damning: **the user IS the operator, and 32 orphans have sat for 5+ weeks without manual drain**. Shipping manual-only retry fails success criterion #1 by design. macOS PIPE_BUF=512 atomicity is real. Combined with a cron, C3 could work — but then the cron is the design, not the journal. |

---

## My Recommendation

**Ship a synthesis: A1 (in-process first-line retry) + C1 (filesystem park as second-line) + the adversary's content-hash dedup as a cross-cutting backstop. Drop the B1 supervisor; we don't need its extra process.**

Call it **A1+C1 with three orthogonal patches**. Concretely:

### The architecture as a single coherent design

```
flush.py:run_flush()
  ├── async retry loop driven by A1's CLASSIFICATIONS table
  │     ├── verdict=retry      → in-process sleep + retry (max 2 attempts inside hook process)
  │     ├── verdict=rate_limit → in-process sleep ≥ 60s if budget allows, else PARK
  │     └── verdict=fail_fast  → write FLUSH_ERROR with diagnostics, do NOT park
  │                               (auth/400/CLINotFoundError will never get better by retry)
  └── on exhaustion of in-process budget
        └── PARK the context file to scripts/parked/<name>.md + .json sidecar (C1 shape)

hooks/session-start.py
  └── Popen scripts/drain.py --max=1   (fire-and-forget, returns <50ms)

scripts/drain.py (C1 shape)
  ├── Globs BOTH session-flush-*.md AND flush-context-*.md   (adversary fix)
  ├── .inflight rename before subprocess.run                  (adversary fix)
  ├── Sets FLUSH_FROM_DRAIN=1 so flush.py knows not to re-park (the missing branch)
  ├── Reads sidecar.last_error; if message matches fail-fast pattern → promote to dead-letter
  │     (adversary suggestion #4, "probe + skip-on-known-bad")
  └── max 5 retries → dead-letter/

flush.py:append_to_daily_log()
  └── Content-hash dedup gate (adversary suggestion #2)
        ├── Hash the section body
        ├── If hash in state["appended_hashes"] in last 24h → skip + log "DUP_SKIP"
        └── Solves: drainer-replay double-append, retry-after-server-success rebill-of-text,
                   duplicate-fact-in-wiki on legacy orphan adoption
```

### Why this composition wins

**1. Two-layer retry matches the two real failure clusters.**

- The **2026-04-12 "Control request timeout"** cluster is bursty-transient — recovers within seconds. A1's in-process retry (1 retry, 2s backoff, ≤4s total) absorbs it inside the hook's spawned process. The hook itself isn't blocking — `flush.py` is detached.
- The **2026-05-13 rapid-fire** cluster (13 failures in minutes) is rate-limit-shaped — recovers within minutes, not seconds. A1 verdicts `rate_limit` → if the in-process budget can't accommodate 60s, **park** and let the SessionStart drainer pick it up on the next session. Drainer's `MIN_AGE_SECONDS=300` honors the rate-limit window. No amplification.

**2. Fail-fast errors never park (handles the auth/400 case).**

The current C1 design "everything parks" burns 5 retries on a permanent error. The adversary's "probe + skip-on-known-bad" plus A1's `fail_fast` verdict turn this into a 0-retry path: `auth_invalid`/`bad_request`/`cli_not_found` write FLUSH_ERROR immediately with diagnostics. Success criterion #2 satisfied without ceremony.

**3. The drainer process boundary handles SDK crashes that in-process retry can't.**

B1's main architectural argument was "survive Python crashes inside flush.py." We retain that property — the drainer re-spawns flush.py as a subprocess. We just don't need a dedicated supervisor process *per* flush; the SessionStart drainer fills that role at no extra wall-clock cost.

**4. Content-hash dedup at the append site solves the rebill/duplicate-fact problem the adversary flagged.**

This is the move nobody made. Every retry strategy (A/B/C) re-bills on server-side-success-client-side-failure. The fix is at the daily-log seam, not the retry layer: hash the response content, gate `append_to_daily_log` on it. Cross-cutting, ~20 LOC, benefits every design. This is also the only proposal-agnostic protection against legacy-orphan double-append when the drainer adopts the 32 existing orphans.

### How it handles the two confirmed failure clusters

| Failure | Path |
|---|---|
| `Exception("Control request timeout: initialize")` (2026-04-12) | A1 classifies → `verdict=retry, base=2s`. In-process retry succeeds on attempt 2. Hook process exits cleanly. No park, no daily-log FLUSH_ERROR. |
| 13× `ProcessError(exit_code=1)` in minutes (2026-05-13) | A1 classifies → `verdict=retry` for first attempt. If still failing OR if `stderr_tail` shows rate-limit text → `verdict=rate_limit` and budget exhausted in-process → **park**. Drainer fires from next SessionStart, `MIN_AGE_SECONDS=300` already elapsed for half the parked items, drains successfully. Worst case: all 13 park, drain across the next ~13 sessions. |
| `Exception("invalid api key")` | A1 classifies → `verdict=fail_fast`. Write FLUSH_ERROR with diagnostics inline. No retry, no park. Operator sees it in the daily log on next read. |
| Python segfault inside flush.py | OS kills the process. Context file remains on disk (because flush.py didn't reach `unlink`). Next SessionStart, drainer adopts it as an orphan and retries. (Same recovery as B1's supervisor would provide.) |

### How it handles the 5 missed gaps

| Gap | Where it's addressed |
|---|---|
| (a) 10s hook timeout | All blocking work stays in detached `flush.py` and detached `drain.py`. Hooks only `Popen` (~50ms). B3-style sync-in-hook is rejected. |
| (b) Content-hash dedup at daily-log append | New patch — `append_to_daily_log` gates on content hash stored in `state["appended_hashes"]` (24h TTL). |
| (c) `flush-context-*` orphan prefix | Drainer's `find_orphans()` globs **both** `session-flush-*.md` and `flush-context-*.md`. Legacy migration also covers both (today 32+0; tomorrow could be 32+N). |
| (d) Cost-budget circuit breaker | Add `state["daily_retry_cost"]` accumulator. If `>$5` in last 24h, drainer refuses to spawn flush.py — writes FLUSH_BUDGET_EXCEEDED to daily log and parks-without-retry. One-line gate in `attempt_one()`. |
| (e) Server-side-success / client-side-failure rebill | Content-hash dedup at append site (gap b's mechanism). Re-charged tokens are unavoidable for a single flush, but **the duplicate daily-log entry is prevented**, which means the compile-cost cascade (the expensive part — $4.49/compile) is gated on hash equality, which is already shipped in compile.py. Total cost of one duplicate flush: ~$0.002. Acceptable. |

---

## The Major Dilemma

**Best-design vs fit-the-budget. The adversary's effort re-estimates are credible, and my synthesis is honestly L-effort (4-6h), not the brief's "≤ 1 hour."**

The brief's success-criterion #7 says ≤1h. The adversary's scorecard says **NONE** of the 9 proposals satisfy it once verified. My synthesis is bigger than any of them. So the user has to choose:

### Option α — "Ship the right design (4-6h)"

Implement A1+C1 with the three patches. Accept that we're over budget. The plan that already exists at `docs/superpowers/plans/2026-05-17-compiler-observability-and-cost.md` is already half-baked; spending another half-day to get a design that **actually absorbs the two confirmed failure clusters and stops the silent-data-loss** is a reasonable investment.

- **Pros:** Fixes the failure modes the codebugs are about. Survives SDK error-surface drift. Dead-letters poison pills. Drains the 32-orphan backlog. Cost-budget circuit breaker prevents runaway retries. Content-hash dedup is a strategic guard that pays off for every future retry path.
- **Cons:** Over budget by 3-5×. The brief explicitly cited "≤ 1 hour" as a hard gate (criterion #7).

### Option β — "Ship the 15-min tactical fix (today)"

Take the adversary's suggestion #3 verbatim: change `flush.py:147` from `f"FLUSH_ERROR: {type(e).__name__}: {e}"` to include `exit_code`, `stderr_tail` (collected via a deque + the `options.stderr` callback), and that's it. No retry. No park. Just make failures **observable**.

Then file the A1+C1 synthesis as a separate plan for a real implementation pass next week.

- **Pros:** Fits the ≤1h budget with room to spare. Immediately fixes the silent-failure half of the problem (the user can finally triage). Doesn't lock us into a retry architecture before we have stderr evidence to design against.
- **Cons:** Doesn't address the data loss. The 25-30% of flushes that fail silently still fail — they just fail loudly now. Success criterion #1 ("absorb the Control-request-timeout cluster") is **not** satisfied.

### My lean

**Option α, with one concession:** ship in two slices on the same week, not one PR.

- **Slice 1 (1h, this session):** Adversary's suggestion #3 — exit-code + stderr-tail in the FLUSH_ERROR line. That's the "ship today" tactical fix. Buys us observability immediately. **No retry yet.**
- **Slice 2 (3-4h, next session):** A1 in-process retry + C1 park + drain.py + content-hash dedup. Now we have a week of FLUSH_ERROR-with-diagnostics evidence to validate the CLASSIFICATIONS table against. The table rows are *grounded in data*, not speculation.

This is also better engineering than racing to ship A1+C1 blind: we'd be guessing at rate-limit text patterns. With Slice 1 in production for a few days, we'd see the real stderr signatures.

**Reasonable people could disagree.** If the user wants the data-loss bleeding stopped *today*, Option α-in-one-shot is defensible — but they need to mentally over-budget to 5h and accept that the first iteration of CLASSIFICATIONS rules will be partly speculative.

---

## What Round 2 Should Focus On

If the user picks my recommendation (α-split into slices):

**Slice 1 design questions to nail down in R2:**

1. Where exactly does the stderr deque live in `flush.py`? (Module-level? Closure in `run_flush`? Passed in?)
2. How big is the tail we capture — 20 lines, 50 lines, 4KB? (Adversary points out fast-fail can leave the tail empty; we should ALSO capture the SDK's last few `logging.error` lines from `flush.log` as a backstop.)
3. Do we preserve the existing `FLUSH_ERROR` regex parsability for downstream tools? (`scripts/compile.py` parses daily-log entries — verify it doesn't choke on the extended line.)

**Slice 2 design questions for R2 (or R3):**

1. **The content-hash dedup ledger.** Where does `state["appended_hashes"]` live? Same `state.json` (touches the C2-FATAL non-atomic batch-flush write — must coordinate)? Or a separate `appended_hashes.json` (cleaner, no contention)?
2. **CLASSIFICATIONS table seed.** Based on Slice 1 evidence: what patterns are *actually* in our stderr? The current proposals' regexes (`rate.?limit|429|too many requests`) are guesses.
3. **Drainer concurrency.** SessionStart can fire from multiple Claude Code instances simultaneously (two terminal windows). Two drainers can race on the same parked item. `.inflight` rename is necessary; needs a test fixture.
4. **Cost-budget circuit breaker semantics.** $5/day is a number I picked from thin air. Should this be configurable? Based on `state["total_cost"]` history?
5. **Promotion-to-dead-letter triggers.** Pure attempt count (5)? Or also pattern match (fail-fast verdict from probe-before-retry)? I think both.

---

## Hesitations

**1. The "≤1h budget" question.** I'm flagging this honestly: I do not believe any design that satisfies success criteria #1-#5 can be implemented in 1 hour. The brief's #7 may be a stated-vs-revealed-preference issue. If the user genuinely meant "1 hour is non-negotiable," then Option β (tactical observability fix only) is the correct call and they need to accept that the retry+park work is a separate, later plan. I lean toward telling them, not deciding for them.

**2. A1's empty-stderr-tail problem is real and I'm under-weighting it.** The adversary is right that on fast `ProcessError(exit_code=1)` failures, the stderr task group is cancelled before our callback drains. If we rely on `stderr_tail` content to detect rate-limit, we're relying on a signal that won't be there for the case it was written for. Mitigation in my synthesis: the `exc_name="ProcessError", exit_code=1` rule fires *without* needing the tail. But that means **we can't actually distinguish rate-limit-1 from generic-crash-1 in fast-fail mode.** We'd just retry generically and let the drainer's longer wait absorb the rate-limit window indirectly. That's adequate but not elegant.

**3. C1's "resolution-order daily log" is a real UX cost.** I'm dismissing it as v1-acceptable; the user might disagree. A drained 14:30 flush appearing in the next morning's 9:15 log under "Memory Flush (09:15)" is confusing if you're scanning chronologically. The sidecar's `parked_at` preserves truth, but the daily log is what humans read. Fixable with a `### Memory Flush (originally 14:30, drained 09:15)` header tweak.

**4. The B1 supervisor has one real advantage I'm dropping: it survives Python segfaults more cleanly than in-process A1.** My synthesis relies on the OS killing flush.py and the next SessionStart's drainer adopting the orphan. That's a 5-minute-to-hours latency on segfault, vs B1's "supervisor immediately retries." In practice, the bundled CLI has not, to my knowledge, segfaulted in this codebase's history (the 1648 FLUSH_ERROR lines are all `exit code 1`, which is a clean exit, not a segfault). So I'm comfortable. But if segfault recovery latency is critical, B1 wins on that single axis.

**5. The adversary might be over-pessimistic on A3's `RotatingFileHandler` race.** In practice, the size-rotation boundary is hit rarely (8MB / 200-byte-lines ≈ 40K log lines). Two concurrent processes both crossing the rotation boundary in the same millisecond is genuinely rare. But "rare" + "silent corruption" is still bad enough to reject A3, and the alternative (single-writer named pipe) is more complex than A1's inline-FLUSH_ERROR. The adversary's call stands; I'm just noting it's not as catastrophic in practice as it sounds.

**6. My "Slice 1 first, then Slice 2" plan assumes the user has a 1-week iteration cadence available.** If they want this all in one ship and won't come back to it, Slice 2 will languish. In that case, push back on me and ship Option α in one go.
