# Design: Compiler Resilience + Observability (A1+C1 Synthesis)

## Design Council Session

- **Rounds:** 1 (skipped R2 — Slice 1 specified clearly enough; Slice 2 will get its own R2-style refinement pass when evidence is in)
- **Team:** 3 architects (A — embrace untyped / B — process boundary / C — park-and-resume), 1 adversary, 1 judge
- **Date:** 2026-05-17
- **Outcome:** A1 (Sniff Table) + C1 (Quiet Park) hybrid, with adversary's content-hash dedup as cross-cutting backstop, B1 supervisor dropped
- **User decision:** α-split — Slice 1 (observability) ships now; Slice 2 (retry+park) ships next session with real-evidence-grounded classification rules

## Problem Statement

The `claude-memory-compiler` has three open dated bugs that interact:

1. **Silent failures** — `hooks/session-end.py:164-165` / `hooks/pre-compact.py:160-161` pipe `flush.py`'s stderr to `subprocess.DEVNULL`. ~25-30% of flushes fail silently on busy days; **1637/1648 (99.3%)** of FLUSH_ERROR lines in `scripts/flush.log` are `Exception("Command failed with exit code 1")` with no diagnostic context.
2. **No transient-failure recovery** — `flush.py:74-149` catches `Exception` broadly and writes `FLUSH_ERROR` on first failure. The 2026-04-12 "Control request timeout: initialize" cluster lost ~8 sessions. The 2026-05-13 rapid-fire cluster lost more.
3. **Compile cost** — separate but co-located concern; already fixed in `docs/superpowers/plans/2026-05-17-compiler-observability-and-cost.md` (two-pass dedup, hash gate, MAX_SELECTED_ARTICLES). NOT redesigned here.

A round-1 plan attempted all three. After two adversarial-review iterations, the **cost piece is sound**; the **retry/observability piece was disqualified by SDK reality** — the type-based retry whitelist caught NONE of the SDK's bare-`Exception` raises.

This council redesigns (only) the resilience+observability piece.

## Chosen Approach: A1+C1 Two-Layer Retry with Content-Hash Dedup

A **two-layer** retry architecture that absorbs both confirmed failure clusters without coupling to SDK internals:

- **First-line (in-process):** A1-style Sniff Table classifies each exception by `(class, message-pattern, exit-code)` into `retry` / `rate_limit` / `fail_fast`. Retries inside the detached `flush.py` process up to a small budget.
- **Second-line (filesystem queue):** When in-process budget is exhausted, **park** the work to `scripts/parked/`. A `drain.py` script fires fire-and-forget from `SessionStart`, processes the queue with `.inflight` rename protection, and promotes 5+ failures to `dead-letter/`.
- **Cross-cutting (daily-log seam):** Content-hash dedup gate on `append_to_daily_log` prevents duplicate entries from retry replay, drainer resumption, or server-side-success-client-side-failure rebill cascades.

Plus three orthogonal patches the adversary identified:

- (a) Drainer globs **both** `session-flush-*.md` AND `flush-context-*.md` (PreCompact uses the latter prefix — 0 today, but the bug is real).
- (b) `state["daily_retry_cost"]` cost-budget circuit breaker: if > $5/day in retries, refuse further spawns; write `FLUSH_BUDGET_EXCEEDED`.
- (c) `FLUSH_FROM_DRAIN=1` env flag in `flush.py` so drainer-spawned attempts don't re-park on first failure (would create park loops).

## Design Rationale

### Why this composition wins

**1. Two layers match the two failure clusters.**

| Cluster | Shape | Absorbed by |
|---|---|---|
| 2026-04-12 "Control request timeout" | Bursty-transient (seconds) | In-process A1 retry (1 retry, 2s backoff, ≤ 4s total) — well inside hook's detached child budget |
| 2026-05-13 rapid-fire exit-code-1 | Rate-limit shaped (minutes) | A1 verdicts `rate_limit`, in-process budget exhausts, parks. Drainer's `MIN_AGE_SECONDS=300` waits out the limit window. No amplification. |

**2. Fail-fast errors never park.**

`auth_invalid` / `bad_request` / `cli_not_found` → A1 `fail_fast` verdict → diagnostic-rich `FLUSH_ERROR` written immediately, no retry, no park. Success criterion #2 satisfied without ceremony. C1's "everything parks" footgun is avoided.

**3. Drainer replaces B1's supervisor at zero extra wall-clock cost.**

B1's main architectural advantage was "survive Python segfaults inside flush.py." We retain it: the drainer re-spawns `flush.py` as a subprocess, and `flush.py` doesn't reach the context-file `unlink` if it crashes, so the file stays on disk and gets adopted by the next SessionStart drainer. We don't need a dedicated supervisor process per flush.

**4. Content-hash dedup is the move nobody made.**

Every retry strategy (A/B/C) re-bills the API on server-side-success-client-side-failure. Fixing this at the retry layer is hard. Fixing it at the **daily-log append seam** is trivial: hash the response body, gate `append_to_daily_log` on `state["appended_hashes"][hash]` within 24h TTL. Cross-cutting, ~20 LOC, benefits every retry path. This is also the only protection against duplicate-article cascades when the drainer adopts legacy orphans (32 already on disk).

### What alternatives were rejected (and why)

| Rejected | By | Why |
|---|---|---|
| **A3 — Two-Stage Triage with `RotatingFileHandler`** | Adversary | Multi-process rotation race. Each `flush.py` is its own process; logger cache doesn't span them; rotation crossings corrupt log files silently. |
| **B3 — Sync hook with 90s budget** | Adversary | `.claude/settings.json` hard hook timeouts are SessionEnd=10s, PreCompact=10s. SIGKILL at 10s makes the retry + fallback-write unreachable code. The architect did not verify the timeout. |
| **C2 — `state.json` queue** | Adversary | `batch-flush.py:454` already writes `state.json` non-atomically. Adding a queue inside the same file would require retro-fitting cross-platform locks across 4 writers (`compile.py`, `flush.py`, `batch-flush.py`, the new drainer). Lock-on-crash has no graceful recovery. |
| **C3 — Append-only journal with operator manual drain** | Adversary | Empirical evidence is damning: **the user IS the operator, and 32 orphans have sat for 5+ weeks without manual intervention.** Manual-only retry contradicts the lived reality. |
| **B1 supervisor process** | Judge | Architecturally the strongest move, but the C1 drainer fills the supervisor's role at no extra cost. We don't need a dedicated supervisor process per flush. |
| **B2 hook-triggered drainer (instead of SessionStart-triggered)** | Judge | B2's drainer race is real but fixable; the genuine issue is that the hook firing the drainer fights for the same 10s budget as the flush spawn it just initiated. SessionStart is the right trigger because it has 15s budget AND no concurrent flush.py firing from the same hook event. |
| **Type-based retry whitelist** (round-2 plan) | Reality | SDK launders all errors to bare `Exception` — no `RateLimitError` class exists. |

### The honest tradeoffs that survive

**1. Drained entries land in the *next day's* daily log.** A 14:30 failure that drains at 09:15 next day appears under that morning's "Memory Flush (09:15)" section. The sidecar's `parked_at` preserves the original time, but the log is what humans read. **Mitigation:** drainer's append uses `### Memory Flush (originally 14:30, drained 09:15)` header tweak.

**2. We cannot distinguish rate-limit-exit-code-1 from generic-crash-exit-code-1 in fast-fail mode.** The SDK's `subprocess_cli.py:613-616` hardcodes `stderr="Check stderr output for details"`; real stderr arrives via the `options.stderr` callback. On fast `ProcessError` failures, the stderr task group may cancel before our callback drains. **Mitigation:** A1's classification falls through to a generic `retry` verdict on `ProcessError(exit_code=1)` with empty stderr — we retry once and let the drainer's longer wait absorb rate-limit windows indirectly.

**3. The CLASSIFICATIONS table in Slice 2 will be partly speculative on first deployment.** Adversary suggestion #3 (the Slice 1 fix) writes exit_code + stderr_tail to the FLUSH_ERROR line. After a few days of these in production, Slice 2 can ground the rate-limit / fail-fast patterns in **real** stderr signatures from the user's actual rate-limit events, instead of guessing `r"rate.?limit|429|too many requests"`.

## Detailed Design

### Slice 1 — Observability (this session, ~1h)

**Single file change:** `scripts/flush.py`.

The current `FLUSH_ERROR` write at line 244-246:
```python
elif "FLUSH_ERROR" in response:
    logging.error("Result: %s", response)
    append_to_daily_log(response, "Memory Flush")
```
…receives a `response` string built at line 147:
```python
response = f"FLUSH_ERROR: {type(e).__name__}: {e}"
```

Replace the response-building site to capture:
- `type(e).__name__` (kept; useful when SDK exposes a typed class like `ProcessError`)
- `str(e)` first 300 chars
- `getattr(e, "exit_code", None)` for `ProcessError`
- `getattr(e, "stderr", "")` (we KNOW this is hardcoded boilerplate; capture it anyway for completeness)
- **stderr tail collected from the `options.stderr=` callback** (currently `_log_stderr` at line 119 writes to `logging.error`; we additionally append to a bounded deque)
- The attempt count (1 in Slice 1 since no retry yet, but the field reserves space for Slice 2)

The deque lives as a closure in `run_flush`. Tail size: 50 lines (or first ~4KB, whichever smaller) — enough to capture rate-limit text without bloating the daily log.

Final FLUSH_ERROR line format:
```
FLUSH_ERROR: ProcessError | exit_code=1 | attempts=1
  message: Command failed with exit code 1
  stderr_tail: (50 lines, indented 4 spaces, fenced if multi-line)
```

The body of `append_to_daily_log(content, "Memory Flush")` already handles multi-line; no change to the writer.

**No new files. No new dependencies. No retry. No park.** The data loss continues for now; failures are just *visible* so we can plan Slice 2 against real evidence.

### Slice 2 — Retry + Park + Dedup (next session, 3-4h)

This is the council's full A1+C1 synthesis. Specified here at design-level; full implementation plan deferred until we have a week of Slice-1 stderr evidence to ground the CLASSIFICATIONS table.

```
flush.py:run_flush()
  ├── stderr deque + structured FLUSH_ERROR (Slice 1 already shipped)
  ├── async retry loop driven by A1's CLASSIFICATIONS table
  │     ├── verdict=retry      → in-process sleep + retry (max 2 attempts inside hook child)
  │     ├── verdict=rate_limit → in-process sleep ≥ 60s if budget allows, else PARK
  │     └── verdict=fail_fast  → write FLUSH_ERROR with diagnostics, do NOT park
  └── on exhaustion of in-process budget
        └── PARK the context file to scripts/parked/<name>.md + .json sidecar

hooks/session-start.py
  └── Popen scripts/drain.py --max=1   (fire-and-forget, returns <50ms)

scripts/drain.py (new file, ~150 LOC)
  ├── Globs BOTH session-flush-*.md AND flush-context-*.md
  ├── .inflight rename before subprocess.run (concurrent-drainer safe)
  ├── Sets FLUSH_FROM_DRAIN=1 so flush.py knows not to re-park on first failure
  ├── Reads sidecar.last_error_verdict; if fail_fast → promote to dead-letter immediately
  └── max 5 retries → dead-letter/

flush.py:append_to_daily_log()
  └── Content-hash dedup gate
        ├── Hash the section body (SHA256 first 16 chars)
        ├── If hash in state["appended_hashes"] in last 24h → skip + log "DUP_SKIP"
        └── Solves: drainer-replay double-append, retry-after-server-success rebill-of-text,
                   duplicate-fact-in-wiki on legacy orphan adoption

Schema additions (split across files to avoid the state.json non-atomic-write
contention that disqualified C2 — see Open Questions #1):
  - scripts/appended_hashes.json: dict[str, iso_timestamp]
      (TTL 24h, pruned on write, written atomically via os.replace)
  - state["daily_retry_cost"]: dict[iso_date, float]  (cost-budget circuit breaker)
  - state["dead_letter_count"]: int  (audit)
```

### Slice 1 Test Strategy

`scripts/test_flush_error_format.py` (new, ~80 LOC, `unittest.IsolatedAsyncioTestCase` — NO new pytest dep, per problem-brief constraint #6):

1. `test_flush_error_includes_exit_code_when_process_error` — synthesize a `claude_agent_sdk.ProcessError(exit_code=1, stderr="boilerplate")`, run through `run_flush` with stubbed `query()`, assert daily-log entry contains `exit_code=1`.
2. `test_flush_error_includes_stderr_tail_when_callback_fed` — exercise the `_log_stderr` callback with 5 stderr lines, raise the exception, assert tail in the daily-log entry.
3. `test_flush_error_truncates_stderr_tail_to_50_lines` — feed 1000 stderr lines, assert tail is ≤ 50.
4. `test_flush_error_preserves_existing_regex_parseability` — daily log starts with `FLUSH_ERROR:` token at line head; downstream `state.json` parsers (none today, but reserve compatibility) can still detect.

### Slice 2 Test Strategy (sketched, full plan deferred)

`scripts/test_classifier.py` — table-driven tests using fixtures captured from Slice 1's real stderr corpus.
`scripts/test_drainer.py` — `tmp_path`-based; spawn drain.py against synthetic parked files; assert idempotency.
`scripts/test_dedup.py` — content-hash gate; replay scenarios.

### Migration / Backward Compatibility

- Slice 1: no schema change. Pure logging format enhancement. Old daily logs parse identically.
- Slice 2: `state.json` gains 3 keys. `utils.py:load_state()` already returns `_default_state()` for missing keys (defensive on `.get()`), so old `state.json` files keep working.
- The 32 legacy `session-flush-*.md` orphans get adopted by the Slice 2 drainer automatically on first SessionStart after Slice 2 deploys (subject to the content-hash dedup gate — duplicates produced by adoption + drain don't pollute the log).

## Known Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| Slice 1's stderr tail is empty on fast-fail `ProcessError(exit_code=1)` | Medium | Capture the `_log_stderr` callback's full session, not just the tail at error time. Falls back gracefully if empty. |
| Drainer race on shared parked file (two SessionStarts at once) | High | `.inflight` rename via `os.rename` (POSIX atomic). Loser exits. Test fixture in Slice 2. |
| Drained daily-log entry breaks chronology | Low (UX) | `(originally HH:MM, drained HH:MM)` header tweak in Slice 2's drainer. |
| Content-hash dedup TTL bug: hash collision within 24h legitimately differs | Very Low | SHA256-16 collision space is 2^64; user produces <1000 sessions/day; collision odds ~10^-13. Acceptable. |
| `state["appended_hashes"]` grows unbounded | Medium | Slice 2's `append_to_daily_log` prunes entries older than 24h on every write. Bounded at ~few-hundred entries. |
| Cost-budget circuit breaker false-positive on a legitimately-expensive day | Low | $5/day threshold is empirical; user can tune via `state["daily_retry_cost_cap"]` override. Default conservative. |
| Slice 2 CLASSIFICATIONS rules are guessed if Slice 1 evidence is thin | Medium | Slice 2 design is **explicitly** deferred until Slice 1 evidence accumulates. Don't ship Slice 2 with speculative regexes. |

## Open Questions (deferred to Slice 2 planning)

1. **~~Where does `state["appended_hashes"]` live?~~** RESOLVED in the Slice 2 sketch above: `scripts/appended_hashes.json` (separate file, atomic `os.replace`). Sharing `state.json` would re-introduce the C2 non-atomic-write race that disqualified that proposal in Round 1.
2. **CLASSIFICATIONS table seed** — what patterns are actually in our stderr? Answer comes from Slice 1 evidence.
3. **Drainer concurrency** — two terminal windows running Claude Code = two SessionStarts = two drainers. `.inflight` rename is necessary; test fixture in Slice 2.
4. **Cost-budget threshold** — $5/day picked from thin air. Make configurable; default conservative.
5. **Dead-letter promotion semantics** — pure attempt count (5)? Or also pattern match (fail-fast verdict on first failure)? **Recommendation: both. Fast-promote on fail_fast; slow-promote on attempt budget exhaustion.**
6. **PreCompact context-file prefix orphan handling** — today 0 `flush-context-*.md` files exist; if PreCompact starts failing, the drainer needs both globs. Already in design.

## Appendix: Council Deliberation

Full artifact directory: `/home/faxik/tools/claude-memory-compiler/docs/plans/design-council-compiler-resilience/`

- `00-problem-brief.md` — framing, constraints, success criteria
- `01-architect-a.md` — A1/A2/A3 (in-process untyped sniff)
- `02-architect-b.md` — B1/B2/B3 (process-boundary supervisor / drainer / sync hook)
- `03-architect-c.md` — C1/C2/C3 (parked/state-queue/journal)
- `05-adversary-r1.md` — attack on all 9, comparative analysis, gap discovery
- `06-judge-r1.md` — ruling, synthesis recommendation, dilemma, hesitations

The adversary discovered **5 gaps all 3 architects missed**, which became patches in the final design:
1. 10s hard hook timeout (eliminates B3)
2. Content-hash dedup at daily-log append site
3. `flush-context-*` orphan prefix coverage
4. Cost-budget circuit breaker
5. Server-side-success / client-side-failure rebill prevention

The Judge's synthesis preserves the best of A1 (in-process discrimination) and C1 (filesystem park), drops B1 (drainer fills supervisor role), adds the adversary's cross-cutting dedup patch, and explicitly defers Slice 2 to wait for grounded evidence — which is itself a design decision (better engineering than racing to ship speculative classification rules).
