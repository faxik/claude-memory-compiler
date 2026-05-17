# Adversarial Review — Round 1

> Verified against the real codebase at `~/tools/claude-memory-compiler/` and the bundled
> `claude_agent_sdk` at `.venv/lib/python3.13/site-packages/claude_agent_sdk/` on 2026-05-17.

Two cross-cutting facts that color *every* proposal:

- **Hook timeouts are 10 / 10 / 15 seconds** (`.claude/settings.json` lines 22, 34, 10).
  Any design that proposes synchronous retry inside a hook against `flush.py` cold start
  (`uv run python scripts/flush.py …` ≈ 200ms warm, ≥ 1s if `.venv` is cold) does **not**
  have a 90-second budget. Claude Code will SIGKILL the hook at 10s.
- **No pytest dependency in `pyproject.toml`**. The only existing test file
  (`scripts/test_utils_state.py`) uses stdlib `unittest`. The brief calls "no new
  heavyweight dependencies" with stdlib + claude-agent-sdk + python-dotenv + tzdata
  *only*. Every proposal that ships pytest-asyncio tests (all three Architect A options)
  drags in pytest + pytest-asyncio + (effectively) an inversion of the dependency floor.
- **PreCompact prefix is `flush-context-*`, SessionEnd prefix is `session-flush-*`.**
  Verified via grep. Currently 32 orphans on disk, ALL with the `session-flush-*` prefix
  (zero `flush-context-*`). Any drainer that only globs one prefix misses half the failure
  surface.
- **SDK `ProcessError.stderr` is hard-coded to `"Check stderr output for details"`** at
  `subprocess_cli.py:613-617`. The real stderr only arrives via the `options.stderr`
  callback. Architects A1/A2/A3 acknowledge this; B's `classify_exception` and C2's
  `_summarize_error` both read `getattr(exc, "stderr", "")` and will capture only the
  hard-coded string. Diagnostic value: zero.
- **Stderr task group is cancelled (line 460) BEFORE `__aexit__`.** Some stderr lines
  emitted in the last milliseconds before exit may be lost. Architects A1/A2/A3 assume
  the deque/StderrTail is hydrated by the time the exception arrives; in practice the
  tail is often empty for fast-fail cases (CLI exit_code=1 with no streamed output).
- **`scripts/batch-flush.py:454` writes `state.json` non-atomically** (plain
  `write_text`, not the atomic-replace pattern from `utils.save_state`). Any proposal
  that adds a sub-key to `state.json` (Architect C2) inherits this race: a concurrent
  batch-flush can truncate the new `pending_flushes` key.
- **`session-start.py` has NO `CLAUDE_INVOKED_BY` recursion guard.** Only `session-end.py`
  and `pre-compact.py` have it (lines 24-25 each). Architect C1's "drainer fires from
  SessionStart" therefore loses the recursion guard if the drainer spawns flush.py
  which spawns Claude Code which… actually, `flush.py:16` sets the env var BEFORE the
  SDK import, so the SDK's child CLI invocation does NOT recurse into a new SessionStart
  (SessionStart fires on the *outer* `claude` invocation, not on the SDK subprocess).
  So C1 is safe on this axis, but the audit was non-trivial — the architect's
  "Verified safe" comment is correct but understated.

---

## Proposal A1: "Sniff Table"

### FATAL findings

- **`pytest.mark.asyncio` requires `pytest-asyncio`.** Neither `pytest` nor
  `pytest-asyncio` is in `pyproject.toml`'s `dependencies` (constraint #6 forbids new
  heavyweight deps). The proposed test suite cannot run without violating the
  constraint. Mitigation possible (rewrite tests on `unittest.IsolatedAsyncioTestCase`),
  but the architect didn't price this in.
- **`stderr_tail` is read AFTER the exception fires** but the SDK's stderr task group
  has been cancelled at `subprocess_cli.py:460` *before* the exception path completes.
  For the `ProcessError(exit_code=1)` case where the CLI exits fast, the streamed-stderr
  callback may have received zero lines. The "rate-limit needle in stderr_tail" rule
  (`_Rule("rate_limit_5h", ... needle=re.compile(r"rate.?limit|429..."))`) will not fire
  on fast 429s — exactly the case the rule was written for.

### SERIOUS findings

- **`type(e).__name__ = "ProcessError"` line in tests doesn't do what it looks like.**
  `type(e)` returns the class; assigning to `.__name__` on `FakeProcessError` then
  re-assigning `type(e).__name__ = "ProcessError"` (test line 282) sets the class
  attribute on the dynamically-created class. This works but only because the test
  already created the class with that name. Spurious. Reads as cargo cult code.
- **`asyncio.get_event_loop().time()`** (line 168) is deprecated in Python 3.12+ when
  no loop is running, and the docstring elsewhere implies Python 3.13. `loop.time()` on a
  freshly-created event loop returns monotonic but the deprecation warning may surface
  in the SDK logs and add noise.
- **The `unknown_bare` catch-all retries on `KeyboardInterrupt` / `SystemExit`.** The
  retry helper catches `BaseException`. If the parent process receives SIGTERM during
  a hook firing, this will swallow it and re-attempt. PreCompact running through SIGTERM
  is exactly the path where you do NOT want to retry — Claude Code is signalling cleanup.
- **The diagnostic propagation claim is partly false.** A1 writes
  `f"FLUSH_ERROR: {type(exc).__name__}: {str(exc)[:120]} | attempts=N | rule=X | stderr_tail=..."` and claims this "lands inline in the daily-log line the user already reads."
  Verified path: `flush.py:246` calls `append_to_daily_log(response, "Memory Flush")`. The
  user does read the daily log. But the proposal sets `stderr_tail` to `last.stderr_tail.replace(chr(10), ' | ')[:300]` which truncates and squashes; if the real rate-limit hint is at line 21 of the tail, it's lost.

### WEAKNESS findings

- **Pattern drift is acknowledged in the Cons but understated.** "New CLI versions can
  subtly change error wording." This isn't theoretical — Architect A's own evidence shows
  the SDK has eight raise sites with seven of them bare `Exception(...)`. The exact
  wording is unsupported API surface.
- **No test that exercises the actual SDK code path** (success criterion #6). The
  pure-function `classify()` test is fine but doesn't verify integration with `query()`.
  A monkey-patch test on `claude_agent_sdk.query` is mentioned but not written.
- **`unknown_bare` catch-all retries once at 2s.** A genuine bug (assertion failure in
  the SDK, missing tool config) will burn one retry + 2 seconds. Cheap, but the
  cumulative cost across 13 failures-in-a-day (the 2026-05-13 cluster) is 26s of pure
  waste on top of the real retry budget.

### STRENGTH findings

- **The table is genuinely greppable.** "Show me everything we retry on" → one `grep
  _Rule scripts/retry.py`. This is the proposal's real win.
- **`process_exit_1` exit-code matching is correct.** It's the ~80% case in
  `flush.log` (verified: 163 `FLUSH_ERROR` lines, 162 of them `exit code 1`).
- **Inline FLUSH_ERROR diagnostics solve the silent-failure complaint** (success
  criterion #4) — the user already reads the daily log; the line gets `attempts=N |
  rule=X | stderr_tail=...` appended.
- **Zero new files** (success criterion #5). Strict improvement over the round-2
  per-PID stderr files.
- **The `_Rule` ordering contract is explicit** ("Order matters: more specific patterns
  first, catch-all last") — this is the kind of property a test could lock in (one
  trivial test: assert `CLASSIFICATIONS[-1].name == "unknown_bare"`).

---

## Proposal A2: "Inline Heuristic"

### FATAL findings

- **Same `pytest.mark.asyncio` constraint violation as A1.**
- **`exc.retry_history = history` and `exc.stderr_tail = tail` on a bare `Exception`**
  (lines 426-427) writes attributes to a possibly-frozen built-in subclass. `Exception`
  instances are normally writable, but a future SDK version using `__slots__` on a
  custom exception class would `AttributeError` here. Bare `Exception` from `query.py:420`
  is mutable, so this works today, but it's a fragile contract.
- **`_should_retry` reads `exc.exit_code` and `exc.stderr`** but does NOT capture them
  into a structured record (unlike A1 which has `AttemptRecord`). If the catch site
  catches a chained exception and re-raises, the original `exit_code` is lost. The
  inline if-tree's lack of a structured record means later diagnostics rely on
  hot-attribute mutation.

### SERIOUS findings

- **Same `stderr_tail` race as A1** — read after cancellation may be empty.
- **`stderr_tail_getter` is a closure over a deque** that lives in `run_flush`. If
  `run_flush` is later refactored to extract the deque-and-callback wiring into a helper,
  the closure breaks silently. No explicit lifetime contract.
- **`"unauthorized" in msg` is a substring match** — `"unauthorized response from
  cache"` (a hypothetical CLI debug line) matches and force-fails-fast a recoverable
  error. The brief explicitly noted "the right test would mock the exact `Exception(...)`
  the SDK raises" — A2 doesn't have that test.
- **"Order is significance, not alphabet" is a comment, not enforced.** A future
  contributor reorganizing `_should_retry` for readability flips the policy silently.
  No test asserts that auth-fail-fast runs before transient-text-match (which would
  catch the regression).

### WEAKNESS findings

- **The 12th-13th-14th elif problem is real.** Already today there are 9 branches.
  Adding "OOMKilled", "ENOSPC", "model not found" pushes this to 12-15. The proposal
  acknowledges this and says "refactor to A1 when it happens"; but the catch is that
  if you actually need to ship 15 cases, A1 was the right call on day one.
- **No `caller` parameter** (which A3 has). A `compile_pass2` retry that fails the same
  way as a `flush` retry produces indistinguishable history records. Forensics suffer.

### STRENGTH findings

- **The shortest helper of the three** (~80 lines vs ~140 vs ~180). Smallest
  surface to bug-audit.
- **Same diagnostic-inline-FLUSH_ERROR pattern as A1.** Strict improvement over
  current silent-stderr behavior.
- **A genuine reader can trace control flow without abstractions.** "What happens on
  401 unauthorized?" → read the 4th `if`. No table lookup, no rule iteration. For a
  small N, this is the cleanest possible code.

---

## Proposal A3: "Two-Stage Triage"

### FATAL findings

- **`pytest.mark.asyncio` constraint violation** (same as A1/A2).
- **The diag log is a NEW per-process file** that the architect characterizes as "fixes
  the round-2 per-PID file growth problem." It DOES rotate, but the round-2 problem
  was actually that per-PID files were created with 0-byte content for short-lived
  runs. A3's `_get_diag_logger` is module-level cached, so within a single Python
  process it works. But each `flush.py` invocation IS a separate Python process —
  the cache resets per invocation. Each flush creates a fresh `RotatingFileHandler`
  pointing at the same file. **Two concurrent flush.py invocations both call
  `RotatingFileHandler.doRollover()` at the size boundary, race on the rename, and
  one of them clobbers the other.** This is the textbook reason `RotatingFileHandler`
  is documented as not multi-process-safe. The architect acknowledges this in Cons
  but characterizes it as "tolerable, diagnostic only" — except the entire point of
  A3 over A1/A2 was the structured log.

### SERIOUS findings

- **`logger.handlers` cache check is intra-process only**, as above. Across processes
  the cache always misses and a new handler is added. If `flush.py` somehow lived
  longer (unlikely but possible during `maybe_trigger_compilation`), multiple handlers
  could be attached to the same logger, double-logging every line.
- **The diag log path `SCRIPTS_DIR / "retry-diag.log"`** lives in the same directory as
  the orphan context files. After backupCount=3 + size=2MB, you have ~8MB in `scripts/`
  forever. Lower-cost than no rotation, but not "zero new files" (success criterion #5
  is partially violated — the file isn't "per-invocation" but it IS new and persistent).
- **Stage 2's "exit_code=1 and nothing recognizable → retry" is a footgun.** The 80%
  case in `flush.log` is `Command failed with exit code 1`. Stage 2 routes it to "retry"
  because the regex tail is empty (per the stderr-task-group-cancellation issue above).
  That's correct for the May 2026 cluster but wrong for the case where the CLI exits
  1 because the API key is invalid — same exit code, fundamentally permanent error.
- **Same `unauthorized` substring footgun as A2** in `_AUTH_RE` (it's a regex now but
  the `(unauthorized|invalid api key)` alternation doesn't word-boundary the matches).

### WEAKNESS findings

- **More moving parts.** Cons acknowledged.
- **The triage path "read FLUSH_ERROR → see pid → grep retry-diag.log"** assumes the
  user knows about this workflow. Today the operator reads the daily log; they don't
  jq logs. The "two-step instead of one-step" cost is understated.
- **`_AttemptLogEntry` dataclass is shipped to JSON via `asdict`** — works for primitives,
  but the architect's `ts=time.strftime(...)` returns a string with timezone-suffix that
  may not round-trip cleanly across locales. Minor.

### STRENGTH findings

- **Stage 1 / Stage 2 separation IS a real architectural improvement** over the flat
  table or flat if-tree. `CLINotFoundError` should never sniff text. A3 enforces that.
- **`caller="flush"` / `"compile_pass1"` / `"compile_pass2"` parameter** is the only
  proposal that distinguishes the three retry sites. Forensically valuable.
- **Bounded log growth (8MB cap)** strictly improves over round-2's unbounded per-PID
  files (modulo the multi-process race).
- **JSON-structured diagnostic log** is genuinely useful for "how often did rate-limit
  fire this week" — IF the multi-process race is fixed (single-writer pattern via a
  named-pipe pusher, or just accept the race and treat lost lines as best-effort).

---

## Proposal B1: "Supervisor Wraps Flush"

### FATAL findings

- **The supervisor `subprocess.run` is SYNCHRONOUS** inside `main()`. The supervisor is
  itself launched fire-and-forget from the hook — fine. But each `subprocess.run`
  with 3 attempts × `BACKOFF_BASE_S=5` × backoff 2x → 5s+10s+20s = 35s, plus 3×
  flush.py execution (each ~10-60s for real SDK calls). The supervisor process can
  live 2-5 MINUTES. **Not a hook problem (it's detached), but**:
  - If the user closes Claude Code and shuts down their machine, the supervisor dies
    mid-retry. The context file remains (per the design), but the supervisor has
    already deleted the daily-log entry? Actually no, supervisor only writes FLUSH_ERROR
    after exhaustion, so this is OK. Demoted from FATAL on review.
- **The proposal sets `creationflags=subprocess.CREATE_NO_WINDOW`** on Windows but
  uses `start_new_session` semantics nowhere. `flush.py` spawned from the supervisor
  on Linux/macOS will inherit the supervisor's session group. If the supervisor process
  is killed (the user reboots; an OOM-killer hits), all child flush.py processes are
  also killed via the session group. Cost: lost work. **Not unique to B1 — same applies
  to B2/C1/C2 — but B1 owns multiple chained subprocesses where the propagation matters
  most.** Mitigation: `start_new_session=True` on the inner `subprocess.run` (which
  requires `Popen + wait()` instead of `subprocess.run`).

### SERIOUS findings

- **Effort estimate is wrong.** Architect claims M (4-6h). Real cost:
  - 30 min for exit-code conventions
  - 60 min for supervisor + log
  - 30 min for hook edits
  - 90 min for tests
  - 60 min for integration tests
  - **MISSING: 60-90 min for idempotency proof** — the `--attempt N` flag means the
    second attempt re-fires `flush.py`, which re-reads the context file, re-calls SDK,
    re-appends to daily log. On attempt 1 the dedup-window (`flush.py:218`) blocks the
    re-fire. On attempt 2+ the proposal SAYS to skip dedup, but then how does it
    prevent two daily-log appends if attempt-1 actually succeeded server-side but
    timed out client-side? **Server-side success + client-side failure is the classic
    LLM-call retry footgun.** The proposal doesn't address it. Conservative cost:
    half-day = L, not M.
- **`classify_exception` lives in flush.py**, which the architect calls out as a "we
  relocated the problem, didn't solve it" trade. Correct self-assessment. But the
  relocation has a real cost: the classifier is now coupled to the SDK error surface
  AND to the supervisor's exit-code contract. Two contracts to maintain.
- **Cost-accounting double-counting.** The brief calls this out: if attempt 1 of
  `compile.py` succeeds (writes state.json with `total_cost`) but the supervisor
  decides it failed (e.g., spurious non-zero exit on a successful run), attempt 2
  fires `compile.py` again. `compile.py:160` does `state["total_cost"] += cost` —
  no idempotency gate beyond the hash check. **B1 makes this worse, not better**,
  because the supervisor's retry budget is exit-code-based and decoupled from the
  per-pass success/failure semantics.
- **Supervisor's stderr file** `supervisor-{session_id}.stderr` is unlinked on success
  AND on final-failure paths. Verified. But: if the supervisor itself crashes between
  spawning flush.py and unlinking the stderr file (Python crash in the supervisor,
  OS-killed mid-loop), the stderr file leaks. New orphan class.

### WEAKNESS findings

- **PreCompact safety claim is right but the hook timeout is 10s** — the supervisor
  Popen needs to complete in <10s. `subprocess.Popen` returns immediately, so this
  works. But if the supervisor's startup hangs (slow `uv run`, cold .venv), the hook
  hits 10s and is killed by Claude Code — and so is the supervisor before it can
  spawn flush.py. Cold-start mitigation: avoid `uv run`, use absolute paths to
  `.venv/bin/python`. The architect doesn't address this.
- **Two new files (`flush-supervisor.py`, `supervisor.log`)** — also success criterion
  #5 partial violation (the log is "bounded by usage" — i.e., unbounded).

### STRENGTH findings

- **Zero coupling to SDK exception types from the retry layer's perspective** — the
  supervisor only sees integers. This is the right architectural move.
- **Survives Python crashes inside flush.py.** A1/A2/A3 can't handle a segfault; B1
  can. Real value for the bundled-CLI which has historically been crash-prone.
- **`--attempt N` is a clean idempotency seam** — flush.py knows whether to honor dedup.
  If properly carried through to compile.py's two-pass design, this gives a true
  retry token, not a magic-byte hack.
- **Per-`session_id` stderr file** (not per-PID) IS strictly better than round-2's
  per-PID files — deterministic name + per-supervisor GC.
- **Tests are exit-code-only**, no SDK mocking required. This is the brief's success
  criterion #6 met more cleanly than A1/A2/A3.

---

## Proposal B2: "Hook Triggers Drainer"

### FATAL findings

- **The drainer race is real and the mitigation is insufficient.** `os.utime(p, None)
  + MIN_AGE_SECONDS=90` means two drainers fired within 90s see different worlds: the
  first picks up the orphan, touches it; the second sees it as "fresh" (mtime just
  bumped). BUT: between the `os.utime` and the `Popen`, there's a ~1ms window where
  another drainer can stat-and-decide. On a fast SSD with a hot inode cache, this is
  microseconds — but the failure mode is *duplicate daily-log entries*, which is
  visible in compiled wiki articles. **The architect calls this a "cosmetic" risk;
  it isn't — duplicate session entries get embedded as duplicate facts in compiled
  knowledge.** Mitigation: `os.rename(p, p.with_suffix(".inflight"))` before Popen,
  rename back on subprocess failure. The architect mentions adding an `.inflight`
  rename "as a small but real piece of the design" but didn't put it in the code
  block. Without it, B2 is racy.
- **`drainer.stderr` is appended-to single file across all retries.** The architect
  calls this "interleaved output, useful for archaeology but harder to attribute."
  Worse than that: on Linux, parallel `open(... mode='ab')` writers using `O_APPEND`
  guarantees atomic-up-to-PIPE_BUF (4KB). Each line from `flush.py` stderr is < 4KB,
  so individual lines are atomic. BUT: a single SDK stderr "line" can exceed 4KB
  during traceback dumps. Interleaving on tracebacks is the worst case for forensics
  (a 5-attempt drainer with mixed tracebacks is unparseable).

### SERIOUS findings

- **`MIN_AGE_SECONDS=90` is arbitrary** and the architect acknowledges this. But the
  consequence isn't just "miss a 5-minute rate-limit window" — it's that the drainer
  *will retry stale work that the user manually copied to investigate*. If an operator
  does `cp scripts/session-flush-abc.md /tmp/inspect.md` and the original mtime hasn't
  been touched, the next drainer fires against it. The architect should advise: parked
  items should `replace()` (atomic-rename) to a `parked/` subdir, NOT live in `scripts/`
  next to live work-in-flight items. (This is C1's design.)
- **`flush.py` still writes `FLUSH_ERROR` on its own failure** (per the proposal's
  edit: "do NOT unlink the context file" but keeps logging.error). The daily log
  STILL gets spammed with FLUSH_ERROR lines on every retry — until the 24h give-up
  threshold. The architect later says "the FLUSH_ERROR is NOT written to the daily log
  here anymore" but the code block only removes the `context_file.unlink(missing_ok=True)`
  call — `flush.py:246` (`append_to_daily_log(response, "Memory Flush")`) is untouched.
  **The proposal's edit is incomplete.**
- **24h retry budget × 16 attempts** for a permanently-poisonous context means 16
  successful API calls to Claude before give-up. At avg $4.49 / compile this could
  cost real money on a true poison-pill. The architect notes "Mitigation: per-file
  attempt count in a sidecar `.attempts` file" but doesn't include it in the code
  block.
- **Effort estimate is wrong.** S (2-3h) is too short once you add:
  - Concurrent-drainer race fix (`.inflight` rename + restore-on-failure) → +30 min
  - Per-file attempt counter → +20 min
  - Fixing the FLUSH_ERROR-in-daily-log bleed → +15 min
  - Real `flush-context-*` glob (not just `session-flush-*`) → +10 min
  Realistic: 3-4h = M.

### WEAKNESS findings

- **Retry latency is unbounded from the user's perspective.** Architect acknowledges
  this in Cons. Success criterion #1 implies recovery "within minutes" of the
  Control-request-timeout cluster — B2 only retries when the user opens Claude Code
  next, which could be hours.
- **`flush_drainer.py` cold-start cost.** Each hook fires the drainer (~50ms `uv run`
  + ~20ms drainer init) AND the producer flush.py (~50ms uv run). PreCompact hook
  budget is 10s, so 100ms is fine — but it's 2× the cost of today's hook.

### STRENGTH findings

- **The 32 orphans get drained on first deployment.** Verified at `scripts/` —
  literally the queue we already have. B2 is the most-aligned with the codebase's
  current shape.
- **Self-healing on machine reboot / supervisor death.** Context file = work item.
  Existence = needs-flushing. This is genuinely the simplest possible queue.
- **Smallest code footprint.** ~80 LOC drainer + ~10 LOC edits. Hook stays in budget.
- **No state.json contention** (unlike C2). Atomic at the filesystem level.

---

## Proposal B3: "Sync Hook With Tight Budget"

### FATAL findings

- **`TOTAL_BUDGET_S = 90.0` violates the configured hook timeout of 10s.** The
  proposal calls this a "constraint renegotiation" and argues SessionEnd fires "after
  the user is done." **This is wrong.** `.claude/settings.json:34` says `"timeout":
  10`. Claude Code's hook timeout is a hard SIGKILL — it does NOT care that the user
  has typed "exit." Within 10 seconds the hook process is killed and any in-flight
  retry is lost. The architect did not verify the configured timeout.
- **`subprocess.run(timeout=TOTAL_BUDGET_S)` doesn't help** because the *outer* hook
  process gets killed at 10s, taking the subprocess down with it (Linux session group
  / Windows job object behavior).
- **The fallback "FLUSH_ERROR with diagnostics" write at the end of the hook is
  unreachable** because the hook gets SIGKILL'd before it can run.

### SERIOUS findings

- **Splits the retry mechanism**: SessionEnd uses sync-in-hook; PreCompact uses
  supervisor (B1) or drainer (B2). Two code paths for the same problem. Architect
  admits this in Cons.
- **Same `--attempt N` idempotency contract as B1**, same un-resolved
  server-side-success / client-side-failure footgun.

### WEAKNESS findings

- **Splits flush.py exit-code logic across two callers**. Maintenance burden.
- **30+ lines inline in the hook** is more code in the hook than the existing 30 lines
  of context-extraction. Hook scope creep.

### STRENGTH findings

- **None survive the hard-timeout fatality.** The "operator gets immediate feedback"
  pro is real *if* the timeout were 90s; it isn't.

---

## Proposal C1: "Quiet Park"

### FATAL findings

- **`migrate_legacy_orphans` only globs `session-flush-*.md`** (line 295). The
  PreCompact orphans use prefix `flush-context-*.md` (verified at `pre-compact.py:138`).
  Today there are 0 of those because PreCompact has never failed in the wild — but
  **the moment PreCompact starts failing (which is part of the failure surface this
  council is here to fix), the legacy adoption will silently drop them.** This is the
  same bug the brief warns about: "35 already-orphaned context files. Each proposal
  makes implicit assumptions about how those legacy files are handled. Verify." C1
  failed verification.

### SERIOUS findings

- **`parked_for_retry` is called from a function that doesn't get a clean reference
  to the exception's `last_exception_summary()`** (line 92). The architect handwaves
  with `last_exception_summary()` — that function does not exist anywhere in the
  proposal. **The sidecar's `last_error` field can be `None` / `"unknown"` on first
  park, which contradicts the design's whole observability pitch.** Fixable in one
  line but is missing as-written.
- **`run_flush` returns `None` to signal "park me"** (line 84) and `flush.py:main()`
  is the one that calls `park_for_retry`. The current `flush.py:236` does
  `response = asyncio.run(run_flush(context))` and then branches on `"FLUSH_OK" in
  response` / `"FLUSH_ERROR" in response`. If `response is None`, the existing branch
  `else: append_to_daily_log(response, "Session")` calls `append_to_daily_log(None, ...)`
  — `append_to_daily_log` does `entry = f"### {section} ({time_str})\n\n{content}\n\n"`,
  which interpolates `None` as the string `"None"`. **The daily log now has a section
  literally saying "None".** The architect needs to add an `if response is None: park
  and return` branch BEFORE the existing branches. The code block in the proposal does
  this (line 88-92), but it's easy to miss in review and the cost of getting it wrong
  is a corrupted daily log.
- **`drain.py` spawns flush.py with `FLUSH_FROM_DRAIN=1`** but the proposal's flush.py
  edit doesn't check this env var. Without it, a drainer-invoked flush.py whose
  inner SDK call fails will re-park the same item (because the `except Exception`
  block parks unconditionally), leaving an infinite loop of "drain → park → drain →
  park." Architect describes the intent ("tells flush.py to not re-park on failure")
  but the flush.py code edit doesn't implement it. **As-written, C1 has a duplicate-park
  bug.**
- **`subprocess.run(..., timeout=180)`** inside the drainer is fine, but the drainer
  is fire-and-forget from SessionStart. So a hung drainer is invisible to the user
  for up to 180s, then dies, with no daily-log breadcrumb. The architect needs to add
  a give-up-marker write on timeout.
- **Effort estimate is wrong.** "M (medium), ~150 LOC + ~40 LOC edits" → 60-90 min.
  Real cost once we add:
  - Both prefixes in legacy migration (`session-flush-*` AND `flush-context-*`) → +5 min
  - `last_exception_summary()` implementation → +10 min
  - `FLUSH_FROM_DRAIN` env check + branch → +10 min
  - `.inflight` lock-rename pattern → +20 min
  - `MIN_AGE_SECONDS` bump from 60→300 → +0 min
  - Unit tests (drainer behavior, race conditions, dead-letter promotion) → +60 min
  - Manual smoke test (drain the 32 real orphans, verify daily-log) → +30 min
  Realistic: 2.5-3h. Still fits the brief's "≤1h" only if you trust the architect's
  rosy estimate, which the brief explicitly calls a footgun ("Effort estimates honesty").

### WEAKNESS findings

- **Resolution-order daily-log entries** — architect acknowledges this. Real cost
  for forensics ("when did this session actually happen?").
- **Dead-letter directory grows forever** without GC. Architect notes the followup
  but doesn't include it.
- **Drainer fires only once per SessionStart at max=1.** With 32 legacy orphans + a
  truly-bad day's 13 new failures = 45 parked items. At one drain per SessionStart
  and ~5 SessionStarts per day, that's 9 days to drain the backlog. Realistic but
  slow.

### STRENGTH findings

- **Matches existing codebase patterns** — files in `scripts/`, JSON sidecars,
  atomic renames, detached subprocesses. The architect's argument here is correct
  and supported by the orphan-on-disk evidence.
- **The 32 orphans get drained** — for the FIRST prefix only. Still a real
  improvement.
- **Survives machine reboot, network outage, suspend/resume** without losing work.
- **Hooks stay in budget** — SessionStart's drainer kick is Popen, returns in <50ms.
- **`dead-letter/` directory is visible in `ls`** — operator can see stuck items
  without running a special command (unlike C2 which buries them in state.json).
- **Sidesteps SDK-error classification entirely.** This is the proposal's genuinely
  novel move — every failure parks; retry-count IS the discrimination. No `str(exc)`
  sniffing, no rule table.

---

## Proposal C2: "State.json Queue"

### FATAL findings

- **`scripts/batch-flush.py:454` writes `state.json` non-atomically** (plain
  `write_text`, not the atomic-replace pattern from `utils.save_state`). The proposal
  adds `pending_flushes` to the same `state.json` file. **A concurrent batch-flush
  + drain.py + compile.py can corrupt the queue.** The architect adds `state_lock`
  via `fcntl.flock` — which only works if EVERY writer takes the lock. `batch-flush.py`
  doesn't (in the proposal's code block; the architect didn't audit batch-flush.py).
  **One unlocked write loses the queue.**
- **Cross-platform locking** with `msvcrt.locking` on Windows. The architect tests
  on neither (admitted in Cons). `msvcrt.locking` locks a single byte; concurrent
  writers across processes need the SAME byte locked. The proposal's `lock_path.touch()`
  + `open(lock_path, "r+")` + lock-byte-1 pattern works *if* all writers do exactly
  the same dance. Adding compile.py / batch-flush.py to that protocol is N more
  audit points — none of which the proposal does.
- **A crashed process holding the lock blocks everyone.** Architect acknowledges this
  in Cons ("a crashed process holding the lock blocks everyone"). Mitigation
  ("`fcntl.LOCK_NB` retry loop") is hand-waved, not coded. As-written, C2 is
  vulnerable to lock-corruption deadlock.

### SERIOUS findings

- **`bump_attempt_in_state` is called but not defined in the proposal.** Just
  referenced (line 477). The architect handwaves it as "obvious" but the locking
  pattern (LOAD state under lock → MUTATE → SAVE under same lock) needs careful
  re-acquisition or a held-lock pattern. As-written, ambiguous.
- **State.json grows unboundedly with stuck items** (acknowledged in Cons). C2's
  implicit dead-letter ("next_attempt_at >= 24h cap") means stuck items stay in
  pending_flushes forever, getting attempted once a day, costing tokens daily until
  operator intervenes.
- **JSON corruption blast radius.** `state.json` already holds `ingested` (52
  entries) + `batch_flush` (with `processed_sessions` keyed by session_id, possibly
  hundreds of entries) + `total_cost`. Adding `pending_flushes` puts the parked
  queue in the same file. **A bad atomic-rename mid-write loses all of it.** The
  architect notes the atomic-replace mitigation, but the existing code (`batch-flush.py:454`)
  doesn't use it.
- **The 24h cap for stuck items + "one shot per day"** is a real cost. If an
  Anthropic outage caused a burst of permanent failures at 10pm, the operator
  fixes the underlying issue at 9am the next day. C2 won't retry those items until
  the 24h boundary at 10pm. Latency to recovery: 12 hours.
- **`should_drain = (int(session_id.replace("-", "")[:8], 16) % 5) == 0`** —
  deterministic based on session_id, but session_id is assigned by Claude Code, not
  the compiler. If the user happens to get session_ids that all happen to hash to
  non-zero-mod-5, **the drainer never fires.** Probabilistic correctness depends on
  Claude Code's session-id distribution. Low risk, but the proposal should have a
  "force drain after N hours" backstop.

### WEAKNESS findings

- **Effort M-to-L** — architect's own estimate already exceeds "~1 hour."
- **Cross-platform locking ugly and untested** (acknowledged).
- **No explicit `dead-letter/` directory** — operator must run `--status` to see
  stuck items. Visibility loss vs. C1.

### STRENGTH findings

- **Single source of truth** for state.json is a real architectural appeal — IF the
  locking actually works across all writers.
- **Exponential backoff naturally handles rate-limit windows** — better than C1's
  fixed `MIN_AGE_SECONDS`.
- **Drainer-from-SessionEnd composes** with the existing producer hook. No new
  hook surface.

---

## Proposal C3: "Append-Only Journal"

### FATAL findings

- **Manual drain is the design.** The architect calls this "operator agency." But
  the user has **32 orphans dating back to April 14** that have sat there for 5+
  weeks without any operator intervention. **The user IS the operator and demonstrably
  does not manually drain.** Shipping a manual-only design when the empirical evidence
  is "the operator forgets" is shipping a system that fails closed. Success criterion
  #1 ("absorb the 2026-04-12 cluster") requires *something* to actually retry. A
  passive SessionStart warning is not retry. **C3 ships the documentation of failures,
  not the recovery from them.**
- **PIPE_BUF atomicity is 512 bytes on macOS** (POSIX minimum), not 4KB. Architect
  acknowledges this in Cons but the proposal's truncation cap is "3500 bytes." On
  macOS, concurrent journal writes will interleave at the 512-byte boundary. **The
  journal becomes corrupt** under PreCompact + SessionEnd firing within seconds of
  each other on macOS. Architect's mitigation ("a `fcntl.flock` around the append")
  reintroduces the lock C3 was avoiding.

### SERIOUS findings

- **`parked_warning` is a context-injection nudge** — but Claude doesn't read its own
  injected context out loud to the user unless something prompts it. The user sees
  the injection only if they happen to ask "what's going on with my memory" — which
  they won't.
- **Effort estimate misleadingly low.** "S-to-M, ~165 LOC, 45-60 min." But the
  proposal has zero retry — its `--drain` is operator-invoked. **The only design that
  satisfies success criterion #1 (absorb the cluster) requires the operator to *also*
  set up a cron job calling `drain.py --drain`.** That's not in the design. Cron
  setup + script + validation + integration testing = 1-2h on top of the 45-60 min.
  Realistic total: 2-3h = M.
- **Compaction strategy (`--compact` command) is hand-waved.** A 100K-line journal
  with `--compact` is mentioned but not implemented. Without it, replay-cost grows
  forever.

### WEAKNESS findings

- **No automatic retry of transient errors.** Architect acknowledges. The whole brief
  is about transient retry; C3 deliberately doesn't do it.
- **Stale records rot.** Same architect-acknowledged failure mode — but in this case
  the design IS the failure mode.

### STRENGTH findings

- **The smallest producer-side change.** flush.py gains ~25 LOC, hooks gain 0-15 LOC.
- **Append-only journal IS auditable** — replay-from-zero is idempotent, truncated
  lines are detectable.
- **No locking on the read path** (architect's claim) — replay-from-zero. Real
  perf win for `--status`.
- **No re-entrancy risk anywhere** — drainer is human-invoked, never auto-invoked.
- **Operator agency is a real principle** — let the user decide when to burn $$$
  on doomed retries. Just needs to be combined with *some* automation for the
  transient cases.

---

## Comparative Analysis

### Shared flaws across proposals

- **All three Architect A options ship pytest+asyncio tests** but the project has no
  pytest dependency and constraint #6 forbids new heavyweight deps. The brief
  specifically lists pytest as the test surface in success criterion #6 — implicit
  contradiction between constraint #6 and criterion #6 that the brief doesn't resolve.
  Architect A should have either (a) rewritten on `unittest.IsolatedAsyncioTestCase`
  or (b) flagged the constraint as needing renegotiation.
- **All three Architect A options rely on `stderr_tail` content** for rate-limit
  classification, but the SDK's stderr task group can be cancelled before the tail
  is hydrated, yielding empty content on fast-fail. The "rate-limit needle in
  stderr_tail" rules will be dead code on the common fast-fail path.
- **All three Architect B options + C2 introduce new files in `scripts/`** with no
  GC — supervisor.log (B1), drainer.log + drainer.stderr (B2), retry-diag.log (A3),
  state.json `pending_flushes` (C2). Success criterion #5 ("no new growth/orphan
  problem") is partially violated by each.
- **Every proposal except A1/A2/A3 ignores the cost-accounting double-counting
  footgun.** The brief explicitly warns that `cost_pass1` is added to both
  `state["total_cost"]` AND the caller's local `total_cost`. None of B1/B2/C1/C2/C3
  address whether retrying a `compile_pass1` re-charges the cost (it does, in the
  current code, because there's no idempotency gate on cost accumulation).
- **No proposal absorbs the server-side-success / client-side-failure case.** SDK
  call hits the API, model generates a response, network/CLI dies before the result
  message returns to Python. Retry re-bills, re-generates, re-appends. This is the
  fundamental retry footgun for LLM workflows and every proposal punts it.
- **No proposal verifies `flush-context-*` prefix orphans** would be drained. Both
  prefixes exist in the codebase (`session-end.py:140` vs `pre-compact.py:138`).
  B2's `find_orphans` covers both. C1's `migrate_legacy_orphans` covers only one.
  C2/C3 don't migrate at all.

### Complementary strengths (synthesis seeds)

- **A1's table + B1's exit-code conventions + C1's parked dir.** Hybrid:
  - flush.py classifies exceptions via A1's table → returns `EXIT_TRANSIENT` /
    `EXIT_PERMANENT` / `EXIT_OK`
  - Supervisor (B1) retries on exit codes only — SDK-agnostic
  - On exhaustion, supervisor MOVES the context file to `scripts/parked/` (C1)
    instead of writing FLUSH_ERROR
  - SessionStart drainer (C1) re-tries parked items on subsequent sessions
  - This composes: in-process retry handles transient bursts; process-boundary retry
    handles SDK crashes; parking handles unrecoverable-but-deferrable.
- **A3's per-`caller` parameter + B1's supervisor + B2's drainer.** Forensic-grade
  observability without state.json contention.
- **C3's append-only journal + B1's supervisor.** Journal records every supervisor
  attempt; replay-from-zero is the read path; supervisor still does automatic retry.
  Best of both: machine-driven retry + human-readable audit trail.

### What everyone missed

- **The 10s hook timeout (`.claude/settings.json`).** B3 is FATAL because of it.
  B1/B2/C1's "hook returns in <50ms after Popen" assumes the Popen itself completes
  the syscall in time; on a cold .venv this can be >1s. Nobody verified.
- **Server-side-success / client-side-failure.** The "Control request timeout" cluster
  is *not* purely failure-side — when control_request_timeout fires at `query.py:420`,
  the API call may have already succeeded server-side. A blind retry re-bills. None
  of A1/A2/A3/B1/B2/B3/C1/C2/C3 propose a request-deduplication seam (e.g., hash the
  prompt → if same prompt seen <60s ago, look up the cached response).
- **`pyproject.toml` has no pytest dependency.** Either the constraint #6 is
  renegotiable (and the brief should have said so) or every test surface must use
  stdlib `unittest`. Three of the nine proposals (A1/A2/A3) silently violate.
- **The `flush-context-*` orphan prefix.** PreCompact has zero orphans today, but
  if PreCompact starts failing (and the council exists because it might), the
  prefix-mismatch will lose work. Only B2 globs both.
- **Idempotency at the daily-log level.** `append_to_daily_log` has NO dedup. The
  current 60-second flush.py-level dedup keys on `session_id`. If two flushes for
  the same session_id happen >60s apart (e.g., session-end then a manual drain 30
  min later), both succeed and write two `### Session (HH:MM)` blocks. None of the
  proposals add a content-hash-keyed dedup at the append site.
- **Cost-budget circuit breaker.** A poison-pill context that always fails will burn
  N retries × $0.10 = real money. Architect C1 dead-letters at 5 attempts; B1 at 3.
  But none propose "if pass-2 costs more than $X, abort" — yet `state["total_cost"]`
  grows visibly per the verified state.json (`$255.42` total). A daily / per-run
  cost cap would prevent runaway retries.

### Approaches not proposed but worth considering

Within the brief's constraints (no `anthropic` SDK, stdlib + claude-agent-sdk only):

1. **Two-layer minimal hybrid.** In-process A1-style "retry once on Control-request-
   timeout, otherwise park" + B2-style "drainer on hook fire to retry parked items."
   This gives the in-day "one quick retry" + the across-day "drain orphans" semantics
   without B1's supervisor process or C2's locking. ~120 LOC total. Test with
   `unittest.IsolatedAsyncioTestCase` to satisfy constraint #6.

2. **Append-side content-hash dedup.** Hash the context-file content; store the hash
   in `state["compiled_contexts"]` (a new ledger). On any `append_to_daily_log` from a
   retry path, check the hash first. Two retries of the same content → one daily-log
   entry. Solves the cost-accounting + double-append + duplicate-fact-in-wiki problems
   for ALL retry strategies. Orthogonal — adds 20 LOC anywhere.

3. **Exit-code-aware FLUSH_ERROR with the streamed stderr tail.** Tiny change, big
   impact: change flush.py:147 from `f"FLUSH_ERROR: {type(e).__name__}: {e}"` to
   `f"FLUSH_ERROR: {type(e).__name__}: {e} | exit_code={getattr(e,'exit_code',None)} |
   stderr_tail={collected_stderr_lines[-500:]}"`. This is 15 minutes of work and
   immediately fixes the silent-failure half of the problem without ANY retry layer.
   Should be a "ship today" tactical fix while the council debates the retry design.

4. **Probe + skip-on-known-bad.** Read the parked sidecar's `last_error.message`
   before retrying. If the message matches `"invalid api key"` / `"prompt is too long"`,
   skip retry entirely; move to dead-letter immediately. Cheaper than C1's "burn 5
   attempts then dead-letter" for known-permanent errors.

---

## Scorecard

| Proposal | Fatal | Serious | Weakness | Strengths | Viable? |
|---|---|---|---|---|---|
| A1 | 2 (pytest dep, empty stderr_tail) | 4 | 3 | 5 | CONDITIONAL — needs unittest tests + accept empty-tail fallback |
| A2 | 3 (pytest dep, attr-mutation, lost exit_code) | 4 | 2 | 3 | CONDITIONAL — same as A1 + ordering contract |
| A3 | 2 (pytest dep, RotatingFileHandler race) | 4 | 3 | 4 | NO — multi-process RotatingFileHandler is broken |
| B1 | 1 (session-group propagation, FIXABLE) | 4 | 2 | 5 | YES — best architectural separation, but L-effort |
| B2 | 2 (drainer race, drainer.stderr interleave) | 4 | 2 | 4 | CONDITIONAL — needs .inflight rename, both prefixes, daily-log FLUSH_ERROR fix |
| B3 | 3 (hook timeout, SIGKILL, unreachable fallback) | 2 | 2 | 0 | **NO** — violates hard 10s hook timeout |
| C1 | 1 (legacy migration prefix mismatch) | 5 | 3 | 6 | CONDITIONAL — fixes are mechanical, design is sound |
| C2 | 3 (non-atomic batch write, cross-platform lock, deadlock-on-crash) | 5 | 3 | 3 | NO — state.json contention is unfixable cheaply |
| C3 | 2 (manual-only contradicts evidence, macOS atomicity) | 3 | 2 | 5 | NO — doesn't satisfy criterion #1 |

---

## Honest Effort Re-Estimates

| Proposal | Architect's estimate | My re-estimate | Why |
|---|---|---|---|
| A1 | S/M (45-60 min) | M (2-3h) | Rewriting tests for stdlib unittest + handling empty-stderr-tail path + adding the monkey-patch integration test the architect punts to a sentence. |
| A2 | S (30-45 min) | M (1.5-2h) | Same as A1 for the test layer + adding an explicit ordering-contract test to lock in the "Order is significance" comment. |
| A3 | M (60-75 min) | L (4-6h) | Multi-process RotatingFileHandler needs replacement (e.g., a `concurrent-log-handler` design or a named-pipe sink). Plus the structured-log consumer ("how do I find rate-limits this week?") requires `jq` or a Python `cat retry-diag.log | python -c "..."` — needs docs. |
| B1 | M (4-6h) | L (6-8h) | Idempotency proof for `--attempt N` re-entry across server-side-success / client-side-failure; `start_new_session` propagation fix; cost-accounting double-charge audit on compile.py path. |
| B2 | S (2-3h) | M (4-5h) | `.inflight` rename + per-file attempt counter + both-prefix glob + correct FLUSH_ERROR-in-daily-log removal in flush.py + race-condition tests. |
| B3 | M (4-5h) | N/A | Doesn't ship — hook timeout fatality. Wasted estimate. |
| C1 | M (60-90 min) | M (2.5-3h) | Both-prefix migration + `last_exception_summary` impl + `FLUSH_FROM_DRAIN` branch in flush.py + `.inflight` lock-rename + cron / gc design + tests. |
| C2 | M-to-L (90-120 min) | L (5-7h) | All callers of state.json (compile.py, batch-flush.py, flush.py, drain.py) must adopt the cross-platform lock. Cross-platform testing on Linux + macOS + Windows is required. Deadlock-recovery design. |
| C3 | S-to-M (45-60 min) | M (2-3h) | Macroscopic locking IF macOS users exist + cron design IF automatic retry desired (otherwise criterion #1 is violated) + journal-compact command + tests. |

**Brief's "≤1 hour" budget is satisfied by NONE of the nine proposals once verified.**
The honest answer is that the design council was over-scoped relative to the budget; the
brief should renegotiate the budget OR accept a smaller-than-proposed scope (e.g., ship
just the "exit-code-aware FLUSH_ERROR with streamed stderr tail" tactical fix as v1, defer
retry to v2).
