# Design Council: Compiler Resilience + Observability + Cost

## Problem Statement

The `claude-memory-compiler` tool at `~/tools/claude-memory-compiler` has three open dated bugs:

1. **Silent failures** — `hooks/session-end.py:164-165` and `hooks/pre-compact.py:160-161` pipe `flush.py`'s stderr to `subprocess.DEVNULL`. On `FLUSH_ERROR`, the user sees only "Command failed with exit code 1 — check stderr output for details" but stderr is discarded. Result: ~25-30% of flushes fail silently on busy days (13+ `FLUSH_ERROR` lines logged on 2026-05-13 alone), and the user cannot triage.
2. **No transient-failure recovery** — `flush.py:74-149` catches `Exception` broadly and immediately writes `FLUSH_ERROR` on first failure. The 2026-04-12 "Control request timeout: initialize" cluster lost ~8 sessions. The 2026-05-13 rapid-fire cluster (13+ failures in a few minutes during batch compile) lost more.
3. **Compile cost** — `scripts/compile.py:53-63` embeds every existing wiki article into every compile prompt. With 221 articles × ~6.8KB ≈ **375K tokens per compile**, cost runs at min $0.84 / max $11.88 / **avg $4.49** per daily-log compile (verified from `state.json`). Linear scaling means costs only grow.

We attempted to fix all three in one plan at `docs/superpowers/plans/2026-05-17-compiler-observability-and-cost.md`. After two adversarial-review iterations, the **cost piece (two-pass dedup + hash-equality gate)** is sound. The **observability + retry pieces** are not — and the second adversarial review uncovered **two new FATALs** rooted in the original author (Claude, round 1) not reading the actual SDK source:

### The disqualifying evidence (verified at `/.venv/lib/python3.13/site-packages/claude_agent_sdk/`):

`_internal/query.py:420`:
```python
raise Exception(f"Control request timeout: {request.get('subtype')}") from e
```

`_internal/query.py:726`:
```python
raise Exception(message.get("error", "Unknown error"))
```

`_errors.py` exception classes (full list): `ClaudeSDKError`, `CLIConnectionError`, `CLINotFoundError`, `ProcessError`, `CLIJSONDecodeError`, `MessageParseError`. **There is no `RateLimitError`, no `AuthenticationError`, no `BadRequestError`.** Rate-limit and auth errors arrive as either:
- `ProcessError(exit_code=N, stderr=...)` from the bundled-CLI subprocess, OR
- bare `Exception(message.get("error"))` laundered from a streaming control message.

### What this kills:

The round-2 plan's `RETRYABLE = (ConnectionError, TimeoutError, OSError, SubprocessError, asyncio.TimeoutError)` whitelist catches NONE of the failure modes the spec was built to absorb. Every "Control request timeout" still writes `FLUSH_ERROR` on the first attempt. The `_rate_limit_delay()` helper keys on `type(exc).__name__ == "RateLimitError"` — a class that doesn't exist in this dependency. Both are dead code.

The plan converged on the wrong design abstraction (strict type-based whitelisting) for a dependency that emits untyped errors.

## Constraints

1. **The bundled CLI is the only path to Claude.** The `claude_agent_sdk` Python package shells out to a Node.js CLI subprocess. We cannot reach the Anthropic HTTP API directly from this codebase, so we cannot use the `anthropic` Python SDK's typed exception hierarchy.
2. **Hooks must stay fast.** `SessionStart` / `SessionEnd` / `PreCompact` hooks fire on every Claude Code invocation. Hook-process startup latency budget is < 200ms.
3. **Background flush is the design.** Hooks spawn `flush.py` as a detached subprocess; the parent hook process must return immediately so Claude Code doesn't block on flush completion.
4. **Single-user, single-machine.** Local SQLite-equivalent simplicity. No coordination service, no message broker, no shared state across machines.
5. **The two-pass cost-reduction work survives.** Hash-equality idempotency, `wiki_index` dropped from pass-2, `MAX_SELECTED_ARTICLES=12` cap, numbered-list regex — these are all keep. We are NOT redesigning the cost piece, only the resilience piece around it.
6. **No new heavyweight dependencies.** Stdlib + the existing `claude-agent-sdk`, `python-dotenv`, `tzdata` only. No `tenacity`, no `aiohttp` retries, etc.

## Success Criteria

A design is "good enough" when it satisfies ALL of:

1. **The 2026-04-12 "Control request timeout" cluster is absorbed** — these are bare `Exception("Control request timeout: ...")`. A re-occurrence should not leak as `FLUSH_ERROR` on first attempt.
2. **Auth / 400 / unrecoverable errors fail fast** — they go straight to `FLUSH_ERROR` with the actual reason captured. No 3-attempt 30-second-backoff wait for an unrecoverable error.
3. **Rate-limit handling does not amplify pressure** — if Anthropic surfaces a 429 (as `ProcessError.stderr` or a bare `Exception` with rate-limit text), we wait long enough that the next attempt is not still in the limit window.
4. **`FLUSH_ERROR` writes always carry diagnostic info** — current behavior writes only `f"FLUSH_ERROR: {type(e).__name__}: {e}"`. The design should preserve enough context (stderr tail, exit code, retry attempt count) for an operator to triage without re-running.
5. **No new growth/orphan problem** — round-2 introduced per-PID stderr files (`flush-stderr.<pid>.log`) with no cleanup. The new design must address GC of any per-invocation files it creates, OR avoid creating them.
6. **Backed by tests that exercise the actual SDK error surface** — not synthetic subclasses. Either dependency-injection-style tests on the retry helper, OR an integration test that simulates a `ProcessError` and a bare `Exception("Control request timeout")`.
7. **No more than ~1 hour of implementation effort** — this is a fix-pass on an already-half-baked plan. Designs that require rewriting `flush.py` from scratch are out of scope.

## Known Context

### What works (do not redesign)

- `compile.py` two-pass dedup with `build_first_pass_prompt` / `build_second_pass_prompt`
- Hash-equality gate at the top of `compile_daily_log`
- Pass-2 dropped `wiki_index` (LLM has Read tool)
- `MAX_SELECTED_ARTICLES = 12` cap in `load_selected_articles`
- Bracket-balanced JSON regex + numbered-list `_BULLET_RE` fallback
- `DETACHED_PROCESS` warning preserved in `build_flush_popen_kwargs` docstring
- The pytest infrastructure added in Task 1 of the plan

### What's broken (under design scrutiny)

- `scripts/retry.py` — the helper exists but its `retry_on` whitelist mismatches the SDK's error surface
- `flush.py`'s call to `run_with_retry` with a typed tuple — catches nothing real
- `compile.py`'s use of `run_with_retry` around `_pass1` / `_pass2` — same problem
- The `_rate_limit_delay()` name-based detection — unreachable code
- Per-PID `flush-stderr.<pid>.log` — solves interleaving but creates 0-byte file pollution
- `partial_costs` list in `state.json` — unbounded growth on repeated pass-2 failures

### Memory entries relevant to this

- **No prior memory** in `~/.claude/projects/-home-faxik-w-autosorter/memory/` covers the compiler-retry topic. The compiler is documented in `~/tools/claude-memory-compiler/knowledge/concepts/claude-memory-compiler-setup.md` but that article documents the 5 structural gaps, not the SDK exception surface.
- **Related compiler concepts on disk:** `batch-flush-incremental-write-fix.md` (May 14 — "lose at most one session" invariant), `secretary-vs-claude-compiler/00-comparison.md` (Secretary's provenance/confidence pattern as a future target). Neither addresses retry directly.

### What the SDK actually raises (verified May 17)

Searched `claude_agent_sdk/_internal/query.py` for `raise` statements:
- Line 272: `raise Exception("canUseTool callback is not provided")`
- Line 308: `raise TypeError(...)`
- Line 318: `raise Exception(f"No hook callback found for ID: {callback_id}")`
- Line 334: `raise Exception("Missing server_name or message for MCP request")`
- Line 346: `raise Exception(f"Unsupported control request subtype: {subtype}")`
- Line 385: `raise Exception("Control requests require streaming mode")`
- Line 420: `raise Exception(f"Control request timeout: {request.get('subtype')}") from e`
- Line 726: `raise Exception(message.get("error", "Unknown error"))`

7 of 8 raise sites use bare `Exception`. The SDK does not preserve underlying error types. Anything that wants to discriminate transient-vs-permanent MUST read the message string OR a sibling attribute (`ProcessError.exit_code`, `ProcessError.stderr`).

## Open Questions

These are the questions the team should resolve:

1. **Should retry discriminate by message text, exit code, exception class, or some combination?** Type-based whitelist failed. The pragmatic options are: (a) catch `Exception`, parse `str(exc)` for known transient patterns; (b) catch `ClaudeSDKError` ⋃ bare `Exception` and inspect `getattr(exc, "exit_code", None)` and `getattr(exc, "stderr", "")`; (c) abandon in-process retry, retry at the orchestrator/process boundary.

2. **Should retry happen at the API-call site (current design), at the function-call site (one level up), at the process boundary (re-run `flush.py` as a subprocess), or at the queue boundary (park and retry later)?** Each has different cost/complexity/failure-domain tradeoffs.

3. **How do we handle the "non-retryable but should still observe" case?** A `BadRequestError` shouldn't retry, but its message ("prompt too long", "invalid model name", etc.) is exactly what an operator needs. Where does that diagnostic land?

4. **Is the per-PID stderr file the right abstraction?** Alternatives: a single shared `flush-stderr.log` with `fcntl.flock()`; appending stderr inline to the daily log itself; routing stderr through Python's `logging` module to a `RotatingFileHandler`.

5. **Should we accept that some failures are unrecoverable in the hook context and design for graceful degradation?** E.g., if all retries exhaust, do we write a "park" file to a `failed/` directory for later operator review, or do we accept data loss and just log loudly?

6. **What's the right test surface?** Round-2's tests used a synthetic `RateLimitError(ConnectionError)`. The right test would mock `claude_agent_sdk.query()` to raise the exact `Exception("Control request timeout: initialize")` that the real SDK raises — but that requires either a dependency-injection seam in `run_flush` or monkey-patching the SDK module.

7. **Is the partial-cost-save mechanism the right pattern, or is it accounting overhead that hides the real fix?** A `partial_costs` list that grows on every pass-2 failure is symptomatic of a recovery design that doesn't actually recover.

## Team Composition

**Architect A — "Embrace Untyped" (Pragmatic message-pattern retry).** Perspective: accept the SDK's untyped error surface as a fact of life. Sniff `str(exc)` and `getattr(exc, "exit_code", None)` / `.stderr` for known patterns. Catch broadly, discriminate textually. Optimize for "works with what the SDK actually does, today."

**Architect B — "Move Resilience Out" (Process-boundary retry).** Perspective: stop catching SDK errors inside the Python process. Treat each `flush.py` invocation or each pass of `compile.py` as an idempotent subprocess. Retry at the *process* boundary by re-spawning. Coupling to SDK internals goes to zero; failure detection is based on exit code + captured stderr.

**Architect C — "Park-and-Resume" (Queue-based deferred retry).** Perspective: real-time retry inside a hook is the wrong abstraction. Treat every failure as "park this work" — write the unfinished context to a `failed/` directory with a JSON sidecar describing why, and let a separate cron-style cleanup job (or the next SessionEnd hook) attempt re-processing. Sacrifices freshness for robustness.

**Adversary** — Critic, attacks all 9 proposals against the real SDK + codebase evidence (no speculation).

**Judge** — Synthesizes, recommends, surfaces the major dilemma honestly.

**No Researcher** — The "external knowledge" we need is already gathered (SDK source + state.json baseline). Architects can do their own targeted grep for codebase context.
