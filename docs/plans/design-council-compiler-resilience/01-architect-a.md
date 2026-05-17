# Architect A — Embrace Untyped: Three Pragmatic Message-Pattern Retry Designs

## Stance

The `claude_agent_sdk` is a Node.js CLI subprocess wrapper. Seven of its eight in-process `raise` sites use bare `Exception(...)` (`_internal/query.py:272,318,334,346,385,420,726`); the eighth case (`ProcessError` from `subprocess_cli.py:613`) has a *hardcoded* `stderr="Check stderr output for details"` — the real stderr only ever reaches the SDK consumer via the `options.stderr` *callback*, never via the exception. There is **no** `RateLimitError`, **no** `AuthenticationError`, **no** `BadRequestError` class. Rate limits during a successful stream arrive as `RateLimitEvent` *message objects* (`types.py:1057-1068`), not exceptions; rate limits that abort the CLI arrive as `ProcessError(exit_code=1)` with the rate-limit text in *streamed stderr*, not in `exc.stderr`.

Two facts from real `flush.log` evidence shape every design here:

```
2026-05-13 16:45:28 ERROR [batch] Agent SDK error: Control request timeout: initialize
2026-05-13 12:22:09 ERROR Result: FLUSH_ERROR: Exception: Command failed with exit code 1 (exit code: 1)
```

The first is bare `Exception("Control request timeout: initialize")` from `query.py:420`. The second is `ProcessError("Command failed with exit code 1", exit_code=1, stderr="Check stderr output for details")` from `subprocess_cli.py:613`. Together these two strings account for the overwhelming majority of observed `FLUSH_ERROR`s. A solution that doesn't discriminate textually on those two strings is fiction.

The retry helper must live **in-process**, at the `async for message in query(...)` call site, because that's the only place that holds (a) the exception, (b) the most-recent stderr lines captured by our streaming callback, and (c) the elapsed wall-clock — all three are needed for triage *and* for choosing a backoff value. Moving retry out of process throws away (b) and (c) for no real gain.

What varies across my three options is **how the message-pattern matching is structured**, **where the matching logic lives**, and **how the diagnostic context propagates to the daily log**. These are genuinely different sub-approaches, not three variations of "catch Exception and grep str(exc)".

---

## Option A1 — "Sniff Table"

### Core Idea

A single hand-curated `CLASSIFICATIONS` table at module top, indexed by ordered `(predicate, verdict, backoff_policy)` triples. The retry helper walks the table once per exception and uses the first match. New observed failure modes are added by appending one row to the table — code review obvious, grep-searchable, and the test suite asserts that every known production stderr line maps to exactly one row.

This is the "boring, explicit, exhaustive" version. The whole knowledge of "what the SDK does today" lives in one place that future maintainers can audit at a glance.

### How It Works

`scripts/retry.py` (new, ~140 lines including docstrings & types):

```python
"""Pragmatic retry helper for the untyped claude_agent_sdk error surface.

Design: walk a static CLASSIFICATIONS table. First match wins. New failure
modes are added by appending one row, not by patching code paths.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

# ---- Classification ----------------------------------------------------

Verdict = Literal["retry", "rate_limit", "fail_fast"]


@dataclass(frozen=True)
class _Rule:
    name: str               # e.g. "control_request_timeout"
    verdict: Verdict
    base_delay: float       # seconds before first sleep at this verdict
    # Either a substring (cheap, common case) or a compiled regex.
    needle: str | re.Pattern[str] = ""
    # Optional discriminator on the exception itself.
    exc_name: str | None = None    # match type(exc).__name__
    exit_code: int | None = None   # match getattr(exc, "exit_code", None)

    def matches(self, exc: BaseException, msg: str) -> bool:
        if self.exc_name and type(exc).__name__ != self.exc_name:
            return False
        if self.exit_code is not None and getattr(exc, "exit_code", None) != self.exit_code:
            return False
        if isinstance(self.needle, re.Pattern):
            return bool(self.needle.search(msg))
        return self.needle in msg if self.needle else True


# Order matters: more specific patterns first, catch-all last.
# Every needle here was observed in real flush.log output OR sourced from
# claude_agent_sdk/_internal/query.py raise sites.
CLASSIFICATIONS: tuple[_Rule, ...] = (
    # ---- known transient, fast retry ----
    _Rule("control_request_timeout", "retry", base_delay=2.0,
          needle="Control request timeout"),                  # query.py:420
    _Rule("cli_connection_lost", "retry", base_delay=2.0,
          exc_name="CLIConnectionError"),
    _Rule("json_decode_partial", "retry", base_delay=1.0,
          exc_name="CLIJSONDecodeError"),
    _Rule("os_pipe_error", "retry", base_delay=1.5,
          exc_name="BrokenPipeError"),

    # ---- ProcessError exit-code-1: usually transient (CLI internal crash,
    # OOM, rate-limit-aborted stream). The hardcoded stderr="Check stderr
    # output for details" tells us nothing; we treat as transient + log
    # the streamed stderr tail collected by our callback.
    _Rule("process_exit_1", "retry", base_delay=3.0,
          exc_name="ProcessError", exit_code=1),

    # ---- known rate-limit phrases (sniffed from streamed stderr; the
    # retry helper checks `last_stderr_tail` in addition to str(exc)) ----
    _Rule("rate_limit_5h", "rate_limit", base_delay=60.0,
          needle=re.compile(r"rate.?limit|429|too many requests", re.I)),
    _Rule("usage_limit", "rate_limit", base_delay=120.0,
          needle=re.compile(r"usage limit|quota exceeded", re.I)),

    # ---- known permanent, fail-fast ----
    _Rule("cli_not_found", "fail_fast", base_delay=0.0,
          exc_name="CLINotFoundError"),
    _Rule("auth_invalid", "fail_fast", base_delay=0.0,
          needle=re.compile(r"unauthorized|invalid api key|forbidden", re.I)),
    _Rule("bad_request", "fail_fast", base_delay=0.0,
          needle=re.compile(r"invalid model|prompt too long|400 bad request", re.I)),

    # ---- catch-all: unknown bare Exception. Retry ONCE, then fail. ----
    _Rule("unknown_bare", "retry", base_delay=2.0),
)


@dataclass
class AttemptRecord:
    attempt: int
    verdict: Verdict
    rule: str
    elapsed_s: float
    exc_type: str
    exc_str: str
    stderr_tail: str = ""


@dataclass
class StderrTail:
    """Mutable ring of the last N lines from the SDK stderr callback."""
    max_lines: int = 20
    _lines: list[str] = field(default_factory=list)

    def append(self, line: str) -> None:
        self._lines.append(line.rstrip())
        if len(self._lines) > self.max_lines:
            self._lines.pop(0)

    def text(self) -> str:
        return "\n".join(self._lines)


def classify(exc: BaseException, stderr_tail: str = "") -> _Rule:
    """Walk CLASSIFICATIONS; return the first matching rule."""
    msg = f"{exc!s}\n{stderr_tail}"
    for rule in CLASSIFICATIONS:
        if rule.matches(exc, msg):
            return rule
    return CLASSIFICATIONS[-1]  # catch-all unknown_bare


# ---- Retry loop --------------------------------------------------------

async def run_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    stderr_tail: StderrTail | None = None,
    log: logging.Logger | None = None,
) -> tuple[Any, list[AttemptRecord]]:
    """Invoke coro_factory(); retry per CLASSIFICATIONS table.

    Returns (result, attempts). Raises the final exception with an
    `attempts` attribute attached on exhaustion.
    """
    log = log or logging.getLogger(__name__)
    attempts: list[AttemptRecord] = []
    start = asyncio.get_event_loop().time()

    for attempt in range(1, max_attempts + 1):
        try:
            result = await coro_factory()
            return result, attempts
        except BaseException as exc:  # SDK launders; we must catch broadly
            tail = stderr_tail.text() if stderr_tail else ""
            rule = classify(exc, tail)
            elapsed = asyncio.get_event_loop().time() - start
            record = AttemptRecord(
                attempt=attempt,
                verdict=rule.verdict,
                rule=rule.name,
                elapsed_s=round(elapsed, 2),
                exc_type=type(exc).__name__,
                exc_str=str(exc)[:200],
                stderr_tail=tail[-1000:],
            )
            attempts.append(record)
            log.warning(
                "retry: attempt=%d rule=%s verdict=%s exc=%s",
                attempt, rule.name, rule.verdict, record.exc_str,
            )
            if rule.verdict == "fail_fast" or attempt == max_attempts:
                exc.attempts = attempts  # type: ignore[attr-defined]
                raise
            # Backoff: exponential + jitter, gated by rule.base_delay.
            delay = rule.base_delay * (2 ** (attempt - 1))
            delay = min(delay, 180.0) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)

    raise RuntimeError("unreachable")
```

`scripts/flush.py` becomes:

```python
async def run_flush(context: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )
    from retry import StderrTail, run_with_retry

    stderr_tail = StderrTail(max_lines=20)

    def _on_stderr(line: str) -> None:
        stderr_tail.append(line)
        logging.error("[bundled CLI stderr] %s", line.rstrip())

    async def _one_attempt() -> str:
        response = ""
        async for message in query(
            prompt=_PROMPT.format(context=context),
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                model="sonnet",
                fallback_model="haiku",
                stderr=_on_stderr,
                extra_args={
                    "strict-mcp-config": None,
                    "disable-slash-commands": None,
                    "setting-sources": "user",
                },
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
        return response

    try:
        response, _attempts = await run_with_retry(
            _one_attempt, max_attempts=3, stderr_tail=stderr_tail,
        )
        return response
    except BaseException as exc:
        attempts = getattr(exc, "attempts", [])
        # Single-line FLUSH_ERROR with structured triage info appended inline.
        last = attempts[-1] if attempts else None
        summary = (
            f"FLUSH_ERROR: {type(exc).__name__}: {str(exc)[:120]}"
            f" | attempts={len(attempts)}"
            f" | rule={last.rule if last else 'n/a'}"
            f" | stderr_tail={(last.stderr_tail.replace(chr(10), ' | ')[:300]) if last else ''}"
        )
        logging.error("Agent SDK error after %d attempts: %s", len(attempts), exc)
        return summary
```

Diagnostics land **inline in the daily-log `FLUSH_ERROR` string** (which the user already reads to triage). No sidecar files, no PID-named clutter, no cleanup job. The same `FLUSH_ERROR` line still parses with the existing `if "FLUSH_ERROR" in response:` branch at `flush.py:244`.

Tests (`tests/test_retry.py`):

```python
import asyncio, pytest
from retry import classify, run_with_retry, StderrTail, CLASSIFICATIONS

def test_control_request_timeout_is_retryable():
    rule = classify(Exception("Control request timeout: initialize"))
    assert rule.name == "control_request_timeout"
    assert rule.verdict == "retry"

def test_process_error_exit_1_is_retryable():
    class FakeProcessError(Exception):
        exit_code = 1
        stderr = "Check stderr output for details"
    FakeProcessError.__name__ = "ProcessError"  # not strictly needed
    e = FakeProcessError("Command failed with exit code 1")
    type(e).__name__ = "ProcessError"           # simulate SDK
    rule = classify(e)
    assert rule.verdict == "retry"

def test_rate_limit_in_stderr_tail_triggers_rate_limit_verdict():
    rule = classify(Exception("stream closed"), stderr_tail="429 too many requests")
    assert rule.verdict == "rate_limit"

def test_auth_fail_fast():
    rule = classify(Exception("401 unauthorized — invalid api key"))
    assert rule.verdict == "fail_fast"

@pytest.mark.asyncio
async def test_run_with_retry_succeeds_on_second_attempt():
    calls = [0]
    async def factory():
        calls[0] += 1
        if calls[0] == 1:
            raise Exception("Control request timeout: initialize")
        return "ok"
    result, attempts = await run_with_retry(factory, max_attempts=3)
    assert result == "ok"
    assert len(attempts) == 1
    assert attempts[0].rule == "control_request_timeout"
```

### Pros

- **Auditable.** One table, ~12 rows, each grep-traceable to a known production string or SDK raise site. A reviewer can read the whole policy in 30 seconds.
- **Cheap test surface.** `classify()` is a pure function over `(exc, str)`. No need to monkey-patch the SDK or simulate subprocess output. Test suite is fast and deterministic.
- **Extension cost is one row.** Discovered a new transient string in tomorrow's `flush.log`? Append a `_Rule(...)` line, ship.
- **Diagnostics inline.** The `FLUSH_ERROR` line in the daily log carries `attempts=N | rule=X | stderr_tail=...`, which is exactly what the operator needs and the existing daily-log reader code already surfaces.
- **No new files, no GC.** Zero sidecar files, no per-PID stderr logs, no `failed/` directory. The current `flush.log` + daily-log are the only two outputs.

### Cons

- **Pattern drift.** New CLI versions can subtly change error wording ("Control request timeout: initialize" → "Initialization control request timed out"). When that happens the catch-all `unknown_bare` rule still retries once, but the verdict-specific backoff (e.g. the 60s rate-limit floor) silently degrades to 2s.
- **No dependency-injection seam for the SDK itself.** Tests can exercise `classify()` and `run_with_retry()` cleanly, but a true integration test that drives `query()` to raise these errors requires monkey-patching `claude_agent_sdk.query` at module level. That's a one-line `monkeypatch.setattr` in pytest, but it does mean the test isn't a pure unit test.
- **Single shared `flush.log` keeps interleaving.** Multiple concurrent batch-flush.py invocations write to the same file. The streamed-stderr lines we capture in `StderrTail` are per-process (good), but the `logging.error` calls into `flush.log` still interleave. Not a regression — the existing code already has this — but the design doesn't fix it.
- **`unknown_bare` is permissive.** A genuinely-unrecoverable bare `Exception` (say, a future SDK assert) will burn 1 retry attempt before failing. Cost: ~2-5s. Acceptable.

### Effort Estimate

**S/M (~45-60 min)** — `retry.py` ~140 lines, `flush.py` refactor ~30 lines changed, `compile.py` retry wrap ~10 lines changed, tests ~80 lines. Well within the "≤ 1 hour" success criterion.

### Risk Profile

- **Most-likely failure:** a new SDK version emits a new error string we haven't seen. Worst case: that one error type falls through to `unknown_bare`, gets one retry, and surfaces as `FLUSH_ERROR` with `rule=unknown_bare` in the daily log — which is the operator's signal to add a new row. **The system tells you what it doesn't know.**
- **Worst plausible case:** the bundled CLI gets a new stderr format and our `stderr_tail` no longer matches the rate-limit regex. Result: rate-limit failures get classified as `unknown_bare`, retry once at 2s, then surface as `FLUSH_ERROR`. Cost: short retry burst into a 429, then graceful failure. The hash-equality gate in `compile.py` (already-shipped) prevents the rate-limit window from being deepened by re-runs.
- **Out of scope:** if the CLI binary itself gets ENOSPC on `flush.log`, neither this nor any retry helper saves us. The existing logging config has no rotation; codebug for that is already on the followup list.

---

## Option A2 — "Inline Heuristic"

### Core Idea

No classification table, no rules dataclass, no module-level state. The retry decision is made **inline** at the call site by a short `_should_retry(exc, stderr_tail) -> tuple[bool, float]` function with `if`/`elif` branches keyed on substring tests. The whole policy is ~25 lines you can read top-to-bottom while debugging. Optimizes for "minimum surface area, easy to step through in a debugger."

The differentiating bet: a future maintainer will be more confident reading 25 lines of straight-line `if` statements than scanning a table-driven dispatch.

### How It Works

`scripts/retry.py` (new, ~80 lines total):

```python
"""Inline-heuristic retry. Walk the if/elif tree; no abstractions."""
from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple


class Decision(NamedTuple):
    retry: bool
    delay_s: float
    label: str  # short string for logging, e.g. "control_request_timeout"


def _should_retry(exc: BaseException, stderr_tail: str, attempt: int) -> Decision:
    """Inline heuristic. Read top to bottom; first match wins.

    Order is significance, not alphabet. The first 4 checks cover ~95% of
    observed flush.log failures. Anything after is best-effort.
    """
    msg = (str(exc) + "\n" + stderr_tail).lower()
    exc_name = type(exc).__name__
    exit_code = getattr(exc, "exit_code", None)

    # Fail-fast cases first — never retry these.
    if exc_name == "CLINotFoundError":
        return Decision(False, 0.0, "cli_not_found")
    if "unauthorized" in msg or "invalid api key" in msg or "forbidden" in msg:
        return Decision(False, 0.0, "auth")
    if "invalid model" in msg or "prompt too long" in msg or "400 bad request" in msg:
        return Decision(False, 0.0, "bad_request")

    # Rate-limit: long sleep, fewer attempts.
    if "rate limit" in msg or "429" in msg or "too many requests" in msg \
       or "usage limit" in msg or "quota exceeded" in msg:
        # Honor approximate "5h window" semantics by sleeping ≥60s on every retry.
        return Decision(attempt < 2, 60.0 * attempt, "rate_limit")

    # Known transient — the two we've actually observed in production.
    if "control request timeout" in msg:
        return Decision(attempt < 3, 2.0 * (2 ** (attempt - 1)), "control_timeout")
    if exc_name == "ProcessError" and exit_code == 1:
        return Decision(attempt < 3, 3.0 * (2 ** (attempt - 1)), "process_exit_1")

    # Network / IO transients.
    if exc_name in {"CLIConnectionError", "CLIJSONDecodeError", "BrokenPipeError"}:
        return Decision(attempt < 3, 1.5 * (2 ** (attempt - 1)), exc_name.lower())

    # Catch-all bare Exception: ONE retry, short delay, then surface.
    return Decision(attempt < 2, 2.0, "unknown")


async def run_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    max_attempts: int = 3,
    stderr_tail_getter: Callable[[], str] = lambda: "",
    log: logging.Logger | None = None,
) -> Any:
    log = log or logging.getLogger(__name__)
    history: list[str] = []

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except BaseException as exc:
            tail = stderr_tail_getter()
            decision = _should_retry(exc, tail, attempt)
            history.append(
                f"a{attempt}:{decision.label}:{type(exc).__name__}:{str(exc)[:80]}"
            )
            log.warning(
                "retry attempt=%d label=%s retry=%s delay=%.1fs",
                attempt, decision.label, decision.retry, decision.delay_s,
            )
            if not decision.retry:
                exc.retry_history = history  # type: ignore[attr-defined]
                exc.stderr_tail = tail        # type: ignore[attr-defined]
                raise
            await asyncio.sleep(decision.delay_s + random.uniform(0, 0.5))
    # Unreachable — last iteration's decision.retry is False.
    raise RuntimeError("unreachable")
```

`flush.py` integration uses a closure-captured deque to feed `stderr_tail_getter`:

```python
from collections import deque

async def run_flush(context: str) -> str:
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )
    from retry import run_with_retry

    stderr_lines: deque[str] = deque(maxlen=20)

    def _on_stderr(line: str) -> None:
        stderr_lines.append(line.rstrip())
        logging.error("[bundled CLI stderr] %s", line.rstrip())

    async def _one_attempt() -> str:
        response = ""
        async for message in query(
            prompt=_PROMPT.format(context=context),
            options=ClaudeAgentOptions(
                cwd=str(ROOT), allowed_tools=[], max_turns=2,
                model="sonnet", fallback_model="haiku",
                stderr=_on_stderr,
                extra_args={...},
            ),
        ):
            ...  # same as before
        return response

    try:
        return await run_with_retry(
            _one_attempt,
            max_attempts=3,
            stderr_tail_getter=lambda: "\n".join(stderr_lines),
        )
    except BaseException as exc:
        history = getattr(exc, "retry_history", [])
        tail = getattr(exc, "stderr_tail", "")
        return (
            f"FLUSH_ERROR: {type(exc).__name__}: {str(exc)[:100]}"
            f" | history={'; '.join(history)}"
            f" | stderr_tail={tail.replace(chr(10), ' | ')[:200]}"
        )
```

Tests are even simpler — `_should_retry` is a pure function of `(exc, str, int)`:

```python
def test_control_timeout_retries_three_times_then_stops():
    d1 = _should_retry(Exception("Control request timeout: initialize"), "", 1)
    d2 = _should_retry(Exception("Control request timeout: initialize"), "", 2)
    d3 = _should_retry(Exception("Control request timeout: initialize"), "", 3)
    assert (d1.retry, d2.retry, d3.retry) == (True, True, False)

def test_auth_never_retries():
    d = _should_retry(Exception("401 unauthorized"), "", 1)
    assert d.retry is False
    assert d.label == "auth"
```

### Pros

- **Minimum abstraction.** No `_Rule` dataclass, no table dispatch, no `re.Pattern` compilation. Just `if`/`elif`. A new dev can read the entire policy in under a minute.
- **Easiest to step through in a debugger.** When something misbehaves at 2am, the maintainer reads straight-line code instead of "which rule matched? what's the order?"
- **Smallest LOC.** ~80 lines for the whole helper vs ~140 for the table approach. Less code surface = fewer bugs to introduce.
- **Same inline-FLUSH_ERROR diagnostics** as A1 — the user keeps seeing triage info in the daily-log line they already read.

### Cons

- **Implicit ordering of cases.** "Order is significance, not alphabet" is a comment, not a contract. A maintainer rearranging `if`s for readability can silently flip the policy. The table version makes order explicit (you literally have to move a row).
- **Heuristic creep.** Adding the 12th, 13th, 14th `elif` makes the function increasingly hard to follow. The table approach degrades more gracefully past N=10.
- **Substring-only matching by default.** No regex support inline without making `_should_retry` ugly. Genuine ambiguity ("rate limit" vs "rate-limit" vs "rateLimit") becomes a row of three `or`s.
- **No central "what does retry know about" surface.** With the table version a maintainer can `grep _Rule` and see everything. Here they have to read the function body.

### Effort Estimate

**S (~30-45 min)** — slightly smaller than A1 because there's no dataclass overhead. Tests are roughly the same size.

### Risk Profile

- **Most-likely failure:** the same as A1 — new SDK version, new error string, falls through to `unknown` catch-all. Behavior on the catch-all is identical (1 retry, then surface), so the failure mode is also identical.
- **Worst case:** the inline `if`-tree becomes a 200-line monster after six months of "just add one more case." That's a refactor-to-A1 moment, not a fundamental failure. The cost of the eventual migration is low because the tests are already pure-function tests.
- **Hidden risk:** `stderr_tail_getter` is called only on failure, but the deque is appended to on every stderr line. If the SDK emits 50K stderr lines for an unusually verbose run, we capture only the last 20 — but those 20 might not include the relevant rate-limit signal. (Same in A1.)

---

## Option A3 — "Two-Stage Triage"

### Core Idea

Treat retry classification as a **two-stage pipeline**: stage 1 is cheap, type-and-exit-code-based ("is this a fail-fast class? is the exit code definitively unrecoverable?"); stage 2 is the expensive textual sniff (regex over `str(exc) + stderr_tail`). The retry helper short-circuits at stage 1 whenever possible — important because some failure modes (e.g. `CLINotFoundError` on a misconfigured machine) we don't *want* to waste a retry attempt on, and we don't want the regex sniff to ever override a definitive class verdict.

This option also differs in **diagnostic propagation**: failures are written to a **single shared `flush-stderr.log`** with `fcntl.flock()` (or `msvcrt.locking()` on Windows), one structured JSON line per attempt. The daily-log still gets the inline `FLUSH_ERROR` summary, but the *full* attempt history (with stderr tail per attempt, timing, classification reasoning) lives in `flush-stderr.log` for the operator who wants to grep. **Single shared file, line-locked, rotated by `logging.handlers.RotatingFileHandler`** — fixes the per-PID-file growth/GC problem the round-2 plan introduced.

### How It Works

`scripts/retry.py` (new, ~180 lines):

```python
"""Two-stage triage: type/exit-code gate, then textual sniff.

Stage 1 (cheap): exception class + exit code → definitive verdict or "needs sniff".
Stage 2 (regex): only runs when stage 1 returns INDETERMINATE.

Diagnostics go to a single shared, lock-protected, rotated log file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

Verdict = Literal["retry", "rate_limit", "fail_fast", "indeterminate"]

# ---- Stage 1: fast class/exit-code gate -------------------------------

# Definitive fail-fast classes — never sniff, never retry.
_FAIL_FAST_CLASSES = frozenset({"CLINotFoundError"})

# Definitive transient classes — retry without sniffing.
_TRANSIENT_CLASSES = frozenset({
    "CLIConnectionError",      # subclass of ClaudeSDKError
    "CLIJSONDecodeError",
    "BrokenPipeError",
    "ConnectionResetError",
})


def _stage1(exc: BaseException) -> Verdict:
    name = type(exc).__name__
    if name in _FAIL_FAST_CLASSES:
        return "fail_fast"
    if name in _TRANSIENT_CLASSES:
        return "retry"
    # ProcessError needs sniffing — exit_code=1 alone is ambiguous (could
    # be transient CLI crash OR a permanent auth error surfaced as exit 1).
    return "indeterminate"


# ---- Stage 2: regex sniff over message + streamed stderr --------------

_RATE_LIMIT_RE = re.compile(
    r"(rate.?limit|429|too many requests|usage limit|quota exceeded)", re.I,
)
_AUTH_RE = re.compile(
    r"(unauthorized|invalid api key|forbidden|401|403)", re.I,
)
_BAD_REQ_RE = re.compile(
    r"(invalid model|prompt too long|context.{0,10}exceed|400 bad request)", re.I,
)
_TRANSIENT_TEXT_RE = re.compile(
    r"(control request timeout|connection reset|stream closed|"
    r"econnreset|epipe|broken pipe|timed out)", re.I,
)


def _stage2(exc: BaseException, stderr_tail: str) -> tuple[Verdict, str]:
    """Returns (verdict, label). Run only when stage 1 returned indeterminate."""
    msg = f"{exc!s}\n{stderr_tail}"
    if _AUTH_RE.search(msg):
        return "fail_fast", "auth"
    if _BAD_REQ_RE.search(msg):
        return "fail_fast", "bad_request"
    if _RATE_LIMIT_RE.search(msg):
        return "rate_limit", "rate_limit"
    if _TRANSIENT_TEXT_RE.search(msg):
        return "retry", "known_transient_text"
    # Bare Exception with exit_code=1 and nothing recognizable: retry ONCE.
    if getattr(exc, "exit_code", None) == 1:
        return "retry", "process_exit_1_unknown_stderr"
    # Truly unknown — retry once, then surface.
    return "retry", "unknown_bare"


def classify(exc: BaseException, stderr_tail: str = "") -> tuple[Verdict, str]:
    s1 = _stage1(exc)
    if s1 != "indeterminate":
        return s1, type(exc).__name__.lower()
    return _stage2(exc, stderr_tail)


# ---- Backoff policy by verdict ----------------------------------------

def _backoff(verdict: Verdict, attempt: int) -> float:
    if verdict == "rate_limit":
        return min(60.0 * attempt, 300.0)
    if verdict == "retry":
        return min(2.0 * (2 ** (attempt - 1)), 30.0)
    return 0.0


# ---- Structured diagnostic log (shared, locked, rotated) --------------

@dataclass
class _AttemptLogEntry:
    ts: str
    pid: int
    caller: str        # "flush" | "compile_pass1" | "compile_pass2"
    attempt: int
    verdict: str
    label: str
    exc_type: str
    exc_str: str
    stderr_tail: str
    elapsed_s: float


def _get_diag_logger(path: Path) -> logging.Logger:
    """Cached. RotatingFileHandler keeps the shared file bounded."""
    logger = logging.getLogger("retry.diag")
    if logger.handlers:
        return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    h = logging.handlers.RotatingFileHandler(
        path, maxBytes=2_000_000, backupCount=3, encoding="utf-8",
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


# ---- Retry orchestrator -----------------------------------------------

async def run_with_retry(
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    caller: str,
    max_attempts: int = 3,
    stderr_tail_getter: Callable[[], str] = lambda: "",
    diag_log_path: Path | None = None,
) -> Any:
    diag = _get_diag_logger(diag_log_path) if diag_log_path else None
    history: list[_AttemptLogEntry] = []
    start = time.monotonic()

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except BaseException as exc:
            tail = stderr_tail_getter()
            verdict, label = classify(exc, tail)
            entry = _AttemptLogEntry(
                ts=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                pid=os.getpid(),
                caller=caller,
                attempt=attempt,
                verdict=verdict,
                label=label,
                exc_type=type(exc).__name__,
                exc_str=str(exc)[:300],
                stderr_tail=tail[-500:],
                elapsed_s=round(time.monotonic() - start, 2),
            )
            history.append(entry)
            if diag is not None:
                diag.info(json.dumps(asdict(entry)))
            if verdict == "fail_fast" or attempt == max_attempts:
                exc.retry_history = history  # type: ignore[attr-defined]
                raise
            await asyncio.sleep(_backoff(verdict, attempt) + random.uniform(0, 0.5))
    raise RuntimeError("unreachable")
```

`flush.py` integration:

```python
async def run_flush(context: str) -> str:
    from collections import deque
    from claude_agent_sdk import (
        AssistantMessage, ClaudeAgentOptions, ResultMessage, TextBlock, query,
    )
    from retry import run_with_retry

    stderr_lines: deque[str] = deque(maxlen=20)

    def _on_stderr(line: str) -> None:
        stderr_lines.append(line.rstrip())

    async def _one_attempt() -> str:
        response = ""
        async for message in query(
            prompt=_PROMPT.format(context=context),
            options=ClaudeAgentOptions(...same..., stderr=_on_stderr, ...),
        ):
            ...
        return response

    try:
        return await run_with_retry(
            _one_attempt,
            caller="flush",
            stderr_tail_getter=lambda: "\n".join(stderr_lines),
            diag_log_path=SCRIPTS_DIR / "retry-diag.log",
        )
    except BaseException as exc:
        history = getattr(exc, "retry_history", [])
        last = history[-1] if history else None
        return (
            f"FLUSH_ERROR: {type(exc).__name__}: {str(exc)[:100]}"
            f" | attempts={len(history)} | verdict={last.verdict if last else '?'}"
            f" | label={last.label if last else '?'}"
            f" | see retry-diag.log pid={os.getpid()}"
        )
```

Tests cover both stages independently:

```python
def test_stage1_cli_not_found_is_fail_fast():
    class CLINotFoundError(Exception): pass
    assert _stage1(CLINotFoundError("x")) == "fail_fast"

def test_stage1_returns_indeterminate_for_bare_exception():
    assert _stage1(Exception("anything")) == "indeterminate"

def test_stage2_classifies_control_timeout():
    v, label = _stage2(Exception("Control request timeout: initialize"), "")
    assert v == "retry"
    assert label == "known_transient_text"

def test_stage2_classifies_rate_limit_in_stderr():
    v, label = _stage2(Exception("stream closed"), "429 too many requests\n")
    assert v == "rate_limit"

def test_diag_log_writes_one_json_per_attempt(tmp_path):
    # Smoke test on the RotatingFileHandler wiring.
    ...
```

### Pros

- **Two stages reflect actual cost asymmetry.** Class+exit-code dispatch is `O(1)`; regex sniff is `O(N)` over stderr_tail. For 95% of failures (`CLIConnectionError`, `CLINotFoundError`) we never touch a regex.
- **Best observability.** Full per-attempt history goes to a structured JSON log file. An operator can `jq` over `retry-diag.log` to answer "how often did rate-limit fire in the last week?" without parsing the daily log. The daily-log `FLUSH_ERROR` line stays terse (operator-readable) and points at the diag log for drill-down.
- **Bounded log growth.** `RotatingFileHandler(maxBytes=2_000_000, backupCount=3)` caps total disk at ~8MB. **Explicitly fixes** the round-2 plan's per-PID stderr file growth/GC problem.
- **Single shared log eliminates interleaving issues.** `logging` already serializes via the GIL + handler-level lock; multiple `batch-flush.py` workers writing JSON lines concurrently are safe.
- **Compositional.** Stage 1 / stage 2 are independently testable. Future contributors who want to add e.g. an `OOMKilled` heuristic touch only stage 2.

### Cons

- **More moving parts.** Three concepts to keep in head: stage 1 (class), stage 2 (regex), and the diagnostic logger. A1's table is flatter.
- **The diag log is a NEW file.** It's bounded and rotated, but it's still a new artifact the user has to know about. The triage path is "read FLUSH_ERROR in daily log → see pid → grep retry-diag.log for that pid". Two-step instead of one-step. Worth it for the structured query payoff, but unavoidably more friction than A1/A2.
- **`logging.handlers.RotatingFileHandler` is not async-safe across processes on Windows.** If two `batch-flush.py` workers hit the same rotation boundary simultaneously, the rotation can lose lines. Mitigation: `delay=True` + best-effort accept. The lines we'd lose are diagnostic-only, not the actual flush output. Tolerable.
- **Slightly more code.** ~180 lines vs ~140 (A1) vs ~80 (A2). Still well under the 1-hour budget.
- **Stage-1/stage-2 boundary can be wrong.** If a new SDK release subclasses `Exception` with a more-specific class (say, `ControlRequestTimeoutError`), stage 1 misses it because the class is unknown. Stage 2 catches it via text. So the result is correct, but the design hint ("if it has a useful class, prefer that") is silently ignored.

### Effort Estimate

**M (~60-75 min)** — at the upper edge of the 1-hour budget. The extra time is in the diag-log wiring and its test. Cuttable to S if we ship without the rotating handler in v1 and add it as a followup codebug (still better than per-PID files).

### Risk Profile

- **Most-likely failure:** the rotating handler misconfigures and silently drops lines. Worst case: lost diagnostics, but flush behavior itself is unaffected (the daily-log inline FLUSH_ERROR is independent of the diag log).
- **Worst case:** the diag log fills the disk because rotation breaks. Cap is ~8MB; even on a small VM this is invisible. Vs. the round-2 design's unbounded per-PID stderr files which could grow indefinitely.
- **Hidden risk:** `_stage1` / `_stage2` ordering. If a future maintainer adds a class to `_TRANSIENT_CLASSES` that should *also* honor a rate-limit text match, stage 1 returns `retry` and stage 2 never runs. Mitigation: docstring + a unit test that asserts the boundary is intentional.

---

## My Recommendation

**Ship A1 ("Sniff Table").** Reasoning:

1. **It directly addresses the two observed dominant failure modes.** `Control request timeout` and `ProcessError(exit_code=1)` both get explicit, named rows with verdict+backoff. The 2026-04-12 cluster is absorbed (3 attempts × 2-8s backoff covers the typical CLI re-init window) and the 2026-05-13 cluster is absorbed (3 attempts × 3-12s for `process_exit_1`).
2. **Auditability beats parsimony when correctness matters.** A2's `if`-tree is shorter, but the rules-table version is what a future maintainer (or the user, at 2am) can actually defend. "Show me what we retry on" is one `grep _Rule scripts/retry.py`.
3. **Inline FLUSH_ERROR diagnostics, no new files.** A3's structured diag log is genuinely better observability — but A1's `FLUSH_ERROR: ... | attempts=N | rule=X | stderr_tail=...` line already gives the operator everything they need to triage *and* surfaces in the daily log they already read. The marginal value of jq-able JSON over a string they'd already see is low for a single-user single-machine system. Per success criterion #5 ("no new growth/orphan problem"), A1 is strictly better — zero new files.
4. **Test surface is the same shape as A2/A3.** `classify()` is a pure function. The test cases (`test_control_timeout_retries`, `test_process_exit_1_retries`, `test_auth_fail_fast`) read exactly the same way regardless of which option we ship.
5. **Fits in the budget.** S/M — comfortably under 1 hour. A3 is at the edge.
6. **Extensibility cost is one row.** When the SDK changes wording (which it will), a future Claude can submit a one-line patch with a unit test, and the diff is grep-obvious.

A2's appeal is real — minimum abstraction — but the implicit-ordering risk is genuinely worse for a function that gates production failures. A3's observability is real, but for a single-user system the structured-log payoff doesn't justify the new file artifact when A1's inline string carries the same signal.

**Fallback:** if the Judge prioritizes observability over parsimony, ship A3 but **make the rotating diag log a v2 (followup codebug) and keep v1 to A1's inline-only diagnostics**. That preserves the "≤ 1 hour" gate without giving up the structured-log upgrade path.

Either way: **do NOT ship a typed-whitelist retry helper**. The SDK does not have a typed error hierarchy. Pretending it does is what put us in this design council.
