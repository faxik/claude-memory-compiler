# Compiler Observability + Cost Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the claude-memory-compiler from a silent system into a debuggable one (capture stderr, retry *transient* failures only) and meaningfully cut compile.py LLM cost — from current state.json baseline (min $0.84, max $11.88, **avg $4.49**) down to ≤ $1.50 median on small logs / ≤ $2.50 median on median logs — by switching from "ship all ~220 articles as context" to "index-first, fetch-on-demand."

**Architecture:** Three independent surgical changes to `~/tools/claude-memory-compiler`:
1. Hook subprocess invocations stop discarding stderr — pipe it to a per-PID append log file (`flush-stderr.{pid}.log`). No rotation in v1; followup codebug will add `logging.handlers.RotatingFileHandler` to both this file and the existing `flush.log` (already 28K+ lines, unbounded).
2. `run_flush` wraps the Agent SDK `query()` loop in a retry-with-backoff helper. **Retry only known-transient errors** (`ConnectionError`, `TimeoutError`, `OSError`); auth/400/rate-limit errors do NOT retry blindly. `RateLimitError` honors `Retry-After` header if present, else sleeps ≥30s.
3. `compile_daily_log` performs a two-pass dedup with retry on both passes: pass 1 sends only `index.md` (~15K tokens) to the LLM and gets back the list of article names it actually needs; pass 2 sends the daily log + the selected subset (capped at 12 articles) — **without** re-sending the index (the LLM has `Read` and can re-fetch if needed). A hash-equality gate at the top of `compile_daily_log` makes the operation idempotent on identical input (skips the API entirely on re-runs of unchanged logs).

**Token-budget reality check** (verified May 17 against the real codebase):
- `knowledge/index.md` = 59,156 bytes ≈ **15K tokens** (spec previously claimed ~5K — off by 3x; corrected here).
- 221 article files totaling 1,503,310 bytes ≈ **375K tokens** — what the current `compile_daily_log` ships in every single compile.
- Per-compile baseline from `state.json`: min $0.84, max $11.88, avg **$4.49**.
- Two-pass design ships ≈15K (pass 1) + ≈10–80K (pass 2, capped) tokens of context per compile instead of ≈395K. Order-of-magnitude reduction, not 10x marketing — and measurable.

**Tech Stack:** Python 3.13, `claude-agent-sdk>=0.1.29`, `uv`, `pytest` (added as dev dep), stdlib only for retry/logging.

---

## File Structure

**New files:**
- `scripts/retry.py` — Generic async retry helper (`run_with_retry(coro_factory, max_attempts, base_delay)`).
- `scripts/article_selector.py` — Pure helpers for the two-pass compile: `parse_selected_articles(llm_response: str) -> list[str]`, `load_selected_articles(names: list[str], knowledge_dir: Path) -> dict[str, str]`, `build_first_pass_prompt(...)`, `build_second_pass_prompt(...)`.
- `tests/conftest.py` — Path setup so tests can import from `scripts/`.
- `tests/test_retry.py` — Tests for the retry helper.
- `tests/test_article_selector.py` — Tests for the article-selector helpers (pure functions, fast).
- `tests/test_hook_stderr_capture.py` — Integration test: spawn a failing subprocess with the same Popen kwargs the hook uses, assert stderr lands in the log file.

**Modified files:**
- `hooks/session-end.py:162-167` — Replace `stderr=subprocess.DEVNULL` with a real file handle pointing at `scripts/flush-stderr.log`.
- `hooks/pre-compact.py:158-162` — Same change as session-end.py.
- `scripts/flush.py:74-149` — Refactor `run_flush` to call a single Agent-SDK attempt and wrap it via `run_with_retry` from `scripts/retry.py`.
- `scripts/compile.py:35-163` — Replace the "read every article" loop (lines 52-63) with a two-pass strategy using `article_selector.py`.
- `pyproject.toml` — Add `[dependency-groups].dev = ["pytest>=8.0", "pytest-asyncio>=0.23"]` so `uv run pytest` works.

**No structural moves.** All changes are additive or surgical inside existing files. The hooks remain Popen-based; `compile.py` keeps its single `compile_daily_log` entry point.

---

## Task 1: Add pytest infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest dev dependencies**

Edit `pyproject.toml`. After the existing `dependencies = [...]` block (which currently contains `claude-agent-sdk`, `python-dotenv`, `tzdata`), add:

```toml
[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]
```

Add this to the same `pyproject.toml`, alongside the existing `[tool.ruff]` block (do not nest inside it):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests", "scripts"]
```

- [ ] **Step 2: Create empty tests package marker**

Create `tests/__init__.py` with an empty body (zero bytes is fine).

- [ ] **Step 3: Create conftest.py for path setup**

Create `tests/conftest.py` with the following body:

```python
"""Add scripts/ to sys.path so test modules can import retry, article_selector, etc."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
```

- [ ] **Step 4: Sync uv and verify pytest available**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv sync --group dev`
Expected: resolves and installs pytest + pytest-asyncio without errors.

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest scripts/test_utils_state.py -v`
Expected: PASS (existing test_utils_state.py still works after path changes).

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add pyproject.toml tests/__init__.py tests/conftest.py
git commit -m "test: add pytest dev infrastructure"
```

---

## Task 2: Retry helper (TDD)

**Files:**
- Create: `scripts/retry.py`
- Create: `tests/test_retry.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_retry.py` with the following content:

```python
"""Tests for the async retry helper."""
from __future__ import annotations

import asyncio
import pytest

from retry import RetryExhausted, run_with_retry


RETRYABLE = (ConnectionError, TimeoutError)


async def test_succeeds_on_first_attempt():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        return "ok"

    result = await run_with_retry(op, max_attempts=3, base_delay=0.0, retry_on=RETRYABLE)
    assert result == "ok"
    assert calls["n"] == 1


async def test_retries_then_succeeds():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("transient")
        return "ok"

    result = await run_with_retry(op, max_attempts=3, base_delay=0.0, retry_on=RETRYABLE)
    assert result == "ok"
    assert calls["n"] == 3


async def test_exhausts_attempts_and_raises():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise ConnectionError("never recovers")

    with pytest.raises(RetryExhausted) as exc_info:
        await run_with_retry(op, max_attempts=3, base_delay=0.0, retry_on=RETRYABLE)

    assert calls["n"] == 3
    assert isinstance(exc_info.value.last_error, ConnectionError)
    assert exc_info.value.attempts == 3


async def test_backoff_delays_grow():
    """Verify base_delay * 2**(attempt-1) is passed to the sleep function."""
    sleeps: list[float] = []

    async def op():
        raise ConnectionError("fail")

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    with pytest.raises(RetryExhausted):
        await run_with_retry(
            op, max_attempts=4, base_delay=2.0, retry_on=RETRYABLE, sleep=fake_sleep
        )

    # 4 attempts means 3 sleeps between them: 2, 4, 8
    assert sleeps == [2.0, 4.0, 8.0]


async def test_non_retryable_exception_raises_immediately():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        await run_with_retry(
            op,
            max_attempts=3,
            base_delay=0.0,
            retry_on=(ConnectionError, TimeoutError),
        )

    assert calls["n"] == 1


async def test_empty_retry_on_is_rejected():
    """Blanket retry is unsafe; callers must opt into a specific tuple."""
    async def op():
        return "never reached"

    with pytest.raises(ValueError, match="retry_on must be a non-empty"):
        await run_with_retry(op, max_attempts=3, base_delay=0.0, retry_on=())


async def test_max_attempts_zero_is_rejected():
    async def op():
        return "never reached"

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
        await run_with_retry(op, max_attempts=0, base_delay=0.0, retry_on=RETRYABLE)


async def test_rate_limit_error_uses_retry_after_header():
    """RateLimitError honors Retry-After (name-based detection, no SDK import)."""
    sleeps: list[float] = []

    class FakeHeaders:
        def get(self, key, default=None):
            return "12.5" if key.lower() == "retry-after" else default

    class FakeResponse:
        headers = FakeHeaders()

    class RateLimitError(ConnectionError):  # subclass so it's in retry_on tuple
        response = FakeResponse()

    async def op():
        raise RateLimitError()

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    with pytest.raises(RetryExhausted):
        await run_with_retry(
            op, max_attempts=3, base_delay=2.0, retry_on=(RateLimitError,), sleep=fake_sleep
        )

    # Both inter-attempt sleeps should be 12.5s, not 2/4 exponential.
    assert sleeps == [12.5, 12.5]


async def test_rate_limit_error_no_header_uses_fallback_30s():
    sleeps: list[float] = []

    class RateLimitError(ConnectionError):
        pass  # no .response attribute

    async def op():
        raise RateLimitError()

    async def fake_sleep(seconds: float):
        sleeps.append(seconds)

    with pytest.raises(RetryExhausted):
        await run_with_retry(
            op, max_attempts=2, base_delay=2.0, retry_on=(RateLimitError,), sleep=fake_sleep
        )

    assert sleeps == [30.0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/test_retry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'retry'`.

- [ ] **Step 3: Implement retry helper**

Create `scripts/retry.py`:

```python
"""Async retry helper with exponential backoff.

Used by flush.py and compile.py to absorb *transient* Agent SDK failures
(network blips, init timeouts) instead of writing FLUSH_ERROR on the first
attempt. Auth and rate-limit errors are NOT retried blindly — callers
must pass an explicit retry_on tuple naming the exception classes they
consider safe to retry. The default is empty: bare callers cannot
accidentally retry an `AuthenticationError` 3 times.

Rate-limit handling: if the caught exception's class name is
"RateLimitError", we honor its `response.headers["Retry-After"]` when
present, else fall back to a conservative 30-second sleep. This is
name-based (not isinstance) to avoid a hard import of the SDK.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable


class RetryExhausted(Exception):
    """Raised when all retry attempts fail. Wraps the final exception."""

    def __init__(self, attempts: int, last_error: BaseException):
        super().__init__(f"retry exhausted after {attempts} attempts: {last_error!r}")
        self.attempts = attempts
        self.last_error = last_error


_RATE_LIMIT_FALLBACK_DELAY = 30.0


def _rate_limit_delay(exc: BaseException) -> float | None:
    """If exc looks like a RateLimitError, return the delay we should sleep.

    None means "this is not a rate limit; use exponential backoff."
    """
    if type(exc).__name__ != "RateLimitError":
        return None
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        retry_after = None
        try:
            retry_after = headers.get("Retry-After") or headers.get("retry-after")
        except Exception:
            retry_after = None
        if retry_after:
            try:
                return float(retry_after)
            except (ValueError, TypeError):
                pass
    return _RATE_LIMIT_FALLBACK_DELAY


async def run_with_retry(
    coro_factory: Callable[[], Awaitable[object]],
    *,
    max_attempts: int = 3,
    base_delay: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> object:
    """Call coro_factory() up to max_attempts times with exponential backoff.

    coro_factory must return a fresh awaitable each call (an already-awaited
    coroutine cannot be re-awaited).

    Raises RetryExhausted if every retryable attempt fails. Raises the
    original exception immediately if it is not an instance of retry_on
    (so auth/bad-request errors fail fast).

    retry_on intentionally defaults to (): callers MUST opt in to a
    specific transient-error set. Blanket Exception retry was the source
    of the May 13 rapid-fire failure burst (rate-limit cluster amplified
    by retries that ignored Retry-After).
    """
    if max_attempts <= 0:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if not retry_on:
        raise ValueError(
            "retry_on must be a non-empty tuple of exception classes; "
            "blanket retry is unsafe (retries auth/400/etc.)"
        )

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except retry_on as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            rl_delay = _rate_limit_delay(exc)
            delay = rl_delay if rl_delay is not None else base_delay * (2 ** (attempt - 1))
            logging.warning(
                "retry attempt %d/%d failed (%s); sleeping %.1fs%s",
                attempt,
                max_attempts,
                type(exc).__name__,
                delay,
                " (Retry-After/rate-limit)" if rl_delay is not None else "",
            )
            await sleep(delay)

    assert last_error is not None
    raise RetryExhausted(max_attempts, last_error)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/test_retry.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/retry.py tests/test_retry.py
git commit -m "feat: add async retry helper with exponential backoff"
```

---

## Task 3: Wire retry into flush.py

**Files:**
- Modify: `scripts/flush.py:74-149` (refactor `run_flush`)

- [ ] **Step 1: Refactor run_flush to use the retry helper**

Replace the body of `run_flush` at `scripts/flush.py:74-149`. The current implementation looks like:

```python
async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context."""
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    prompt = f"""... (unchanged 30 lines of prompt) ..."""

    response = ""

    try:
        def _log_stderr(line: str) -> None:
            logging.error("[bundled CLI stderr] %s", line.rstrip())

        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(...),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
            elif isinstance(message, ResultMessage):
                pass
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"

    return response
```

Replace it with:

```python
async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract important knowledge from conversation context.

    Wraps the single-attempt SDK call in retry-with-backoff so transient
    failures (initialization timeouts, rate limits, network blips) don't
    silently lose sessions. After max_attempts the failure is recorded as
    FLUSH_ERROR in the daily log so the gap is visible.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    from retry import RetryExhausted, run_with_retry

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    def _log_stderr(line: str) -> None:
        logging.error("[bundled CLI stderr] %s", line.rstrip())

    async def _single_attempt() -> str:
        collected = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                model="sonnet",
                fallback_model="haiku",
                stderr=_log_stderr,
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
                        collected += block.text
            elif isinstance(message, ResultMessage):
                pass
        return collected

    # Retry only on transient classes. Auth/400 errors fail fast.
    # ProcessError covers the bundled-CLI subprocess hiccup that produced
    # the "Control request timeout: initialize" cluster on 2026-04-12.
    try:
        from subprocess import SubprocessError  # noqa: WPS433
    except ImportError:
        SubprocessError = Exception  # type: ignore[misc,assignment]

    RETRYABLE: tuple[type[BaseException], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
        SubprocessError,
        asyncio.TimeoutError,
    )

    try:
        result = await run_with_retry(
            _single_attempt,
            max_attempts=3,
            base_delay=2.0,
            retry_on=RETRYABLE,
        )
        return str(result)
    except RetryExhausted as exhausted:
        import traceback
        last = exhausted.last_error
        logging.error(
            "Agent SDK error after %d attempts: %s\n%s",
            exhausted.attempts,
            last,
            "".join(traceback.format_exception(type(last), last, last.__traceback__)),
        )
        return f"FLUSH_ERROR: {type(last).__name__}: {last}"
```

- [ ] **Step 2: Run the existing test_utils_state.py to ensure no import-time regression**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest scripts/test_utils_state.py -v`
Expected: PASS (regression check — `flush.py` is imported indirectly through path setup).

- [ ] **Step 3: Sanity-check flush.py imports cleanly**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import flush"`
Expected: no output, exit 0.

- [ ] **Step 4: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/flush.py
git commit -m "feat(flush): retry transient Agent SDK failures with backoff"
```

---

## Task 4: Capture hook subprocess stderr (TDD)

**Files:**
- Create: `tests/test_hook_stderr_capture.py`
- Modify: `hooks/session-end.py:144-167`
- Modify: `hooks/pre-compact.py:140-162`

- [ ] **Step 1: Write failing integration test**

Create `tests/test_hook_stderr_capture.py`:

```python
"""Verify that hook-spawned subprocesses do not discard stderr.

We test the Popen-kwargs builder rather than launching the real hook —
the hook reads JSON from stdin and resolves a transcript path, which is
not what we are testing. We extract a helper from each hook that builds
the Popen kwargs and assert it returns a real file handle for stderr.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _per_pid_log(base: Path) -> Path:
    """Mirror the per-PID naming the helper uses so the test knows where to look."""
    import os
    return base.parent / f"{base.stem}.{os.getpid()}{base.suffix}"


def test_session_end_stderr_goes_to_file(tmp_path):
    mod = _load("hook_session_end", ROOT / "hooks" / "session-end.py")
    base = tmp_path / "stderr.log"
    expected = _per_pid_log(base)
    kwargs = mod.build_flush_popen_kwargs(base)
    assert kwargs["stderr"] is not subprocess.DEVNULL
    handle = kwargs["stderr"]
    handle.write(b"probe\n")
    handle.flush()
    handle.close()
    assert expected.read_bytes() == b"probe\n"
    assert not base.exists()  # original path is unused; per-PID variant is the target


def test_pre_compact_stderr_goes_to_file(tmp_path):
    mod = _load("hook_pre_compact", ROOT / "hooks" / "pre-compact.py")
    base = tmp_path / "stderr.log"
    expected = _per_pid_log(base)
    kwargs = mod.build_flush_popen_kwargs(base)
    assert kwargs["stderr"] is not subprocess.DEVNULL
    handle = kwargs["stderr"]
    handle.write(b"probe\n")
    handle.flush()
    handle.close()
    assert expected.read_bytes() == b"probe\n"
    assert not base.exists()


def test_real_subprocess_stderr_round_trip(tmp_path):
    """Spawn a tiny python -c that writes to stderr and exits 1.
    Confirm the bytes land in our log file."""
    log_path = tmp_path / "stderr.log"
    with open(log_path, "ab") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys; sys.stderr.write('BOOM\\n'); sys.exit(1)"],
            stdout=subprocess.DEVNULL,
            stderr=fh,
        )
        proc.wait()
    assert b"BOOM" in log_path.read_bytes()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/test_hook_stderr_capture.py -v`
Expected: `test_session_end_stderr_goes_to_file` and `test_pre_compact_stderr_goes_to_file` FAIL with `AttributeError: module ... has no attribute 'build_flush_popen_kwargs'`. The third test (`test_real_subprocess_stderr_round_trip`) should PASS — it's a sanity check that stdlib Popen behaves as we expect.

- [ ] **Step 3: Extract Popen-kwargs helper in session-end.py**

In `hooks/session-end.py`, replace lines 157-167 (the `creation_flags` + `subprocess.Popen` block) with:

```python
    flush_stderr_log = SCRIPTS_DIR / "flush-stderr.log"

    # On Windows, build_flush_popen_kwargs sets CREATE_NO_WINDOW (no console flash).
    # Do NOT add DETACHED_PROCESS — it breaks the Agent SDK's subprocess I/O
    # (the bundled CLI loses stdio handles).
    try:
        popen_kwargs = build_flush_popen_kwargs(flush_stderr_log)
        subprocess.Popen(cmd, **popen_kwargs)
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)
```

Then, **above** `def main()` (i.e. at module scope), add:

```python
# TODO: if a third caller needs this, factor into hooks/_popen_helpers.py.
# Kept inline today to keep hook startup <100ms (avoids extra import path traversal
# during SessionStart/SessionEnd which fire on every CC invocation).
def build_flush_popen_kwargs(stderr_log_path):
    """Return Popen kwargs that capture stderr to a per-PID append log.

    Previously stderr was sent to subprocess.DEVNULL, which meant flush.py
    failures ("Command failed with exit code 1") had no diagnostic trail.
    Now stderr is appended to `<stem>.{pid}<suffix>` so concurrent
    SessionEnd + PreCompact invocations don't interleave (Linux append-mode
    is atomic only up to PIPE_BUF = 4096B; Agent SDK tracebacks routinely
    exceed that). Stdout still goes to DEVNULL — flush.py uses its own
    logger writing to flush.log directly.

    Platform note: on Windows, sets CREATE_NO_WINDOW (avoids console flash).
    Do NOT add DETACHED_PROCESS — it breaks the bundled CLI's stdio
    handles inside the Agent SDK.

    The caller is responsible for the parent file handle's lifetime. We
    leave it open intentionally: the child inherits the fd, and closing
    it in the parent does NOT close it in the child.
    """
    import os
    stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
    per_pid = stderr_log_path.parent / f"{stderr_log_path.stem}.{os.getpid()}{stderr_log_path.suffix}"
    handle = open(per_pid, "ab", buffering=0)
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": handle,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    return kwargs
```

- [ ] **Step 4: Apply the same change to pre-compact.py**

In `hooks/pre-compact.py`, replace lines 155-163 (the `creation_flags` + `subprocess.Popen` block) with:

```python
    flush_stderr_log = SCRIPTS_DIR / "flush-stderr.log"

    # On Windows, build_flush_popen_kwargs sets CREATE_NO_WINDOW (no console flash).
    # Do NOT add DETACHED_PROCESS — it breaks the Agent SDK's subprocess I/O
    # (the bundled CLI loses stdio handles).
    try:
        popen_kwargs = build_flush_popen_kwargs(flush_stderr_log)
        subprocess.Popen(cmd, **popen_kwargs)
        logging.info("Spawned flush.py for session %s (%d turns, %d chars)", session_id, turn_count, len(context))
    except Exception as e:
        logging.error("Failed to spawn flush.py: %s", e)
```

And add the same `build_flush_popen_kwargs` definition above `def main()` (paste the identical body from Step 3 — do not factor into a shared module yet; the hooks are intentionally standalone and import-light to keep SessionStart latency low; same `# TODO: factor when third caller appears` comment).

- [ ] **Step 5: Run all tests**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/ -v`
Expected: all retry tests + all stderr-capture tests pass (9 retry + 3 stderr = 12 passed).

- [ ] **Step 6: Verify the live stderr file is being written**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "
import os, sys, importlib.util, subprocess
from pathlib import Path
spec = importlib.util.spec_from_file_location('hook_session_end', 'hooks/session-end.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
base = Path('/tmp/stderr-probe.log')
expected = base.parent / f'{base.stem}.{os.getpid()}{base.suffix}'
expected.unlink(missing_ok=True)
kw = mod.build_flush_popen_kwargs(base)
proc = subprocess.Popen([sys.executable, '-c', 'import sys; sys.stderr.write(\"LIVE-OK\\n\"); sys.exit(2)'], stdout=subprocess.DEVNULL, **{k:v for k,v in kw.items() if k != 'stdout'})
proc.wait()
assert b'LIVE-OK' in expected.read_bytes(), expected.read_bytes()
print('OK')
"`
Expected: prints `OK`.

- [ ] **Step 7: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add hooks/session-end.py hooks/pre-compact.py tests/test_hook_stderr_capture.py
git commit -m "feat(hooks): capture flush.py stderr to log file instead of DEVNULL"
```

---

## Task 5: Article selector — pure helpers (TDD)

**Files:**
- Create: `scripts/article_selector.py`
- Create: `tests/test_article_selector.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_article_selector.py`:

```python
"""Tests for the two-pass article selector used by compile.py."""
from __future__ import annotations

from pathlib import Path

from article_selector import (
    build_first_pass_prompt,
    build_second_pass_prompt,
    load_selected_articles,
    parse_selected_articles,
)


def test_parse_selected_articles_handles_json_array():
    response = '["concepts/foo", "concepts/bar"]'
    assert parse_selected_articles(response) == ["concepts/foo", "concepts/bar"]


def test_parse_selected_articles_strips_md_suffix():
    response = '["concepts/foo.md", "concepts/bar.md"]'
    assert parse_selected_articles(response) == ["concepts/foo", "concepts/bar"]


def test_parse_selected_articles_handles_bullet_list_fallback():
    response = """Selected articles:
- concepts/foo
- concepts/bar
"""
    assert parse_selected_articles(response) == ["concepts/foo", "concepts/bar"]


def test_parse_selected_articles_handles_numbered_list_fallback():
    """Sonnet sometimes drifts to numbered prose despite a JSON-only instruction."""
    response = """1. concepts/foo
2. concepts/bar
3) concepts/baz
"""
    assert parse_selected_articles(response) == [
        "concepts/foo",
        "concepts/bar",
        "concepts/baz",
    ]


def test_parse_selected_articles_handles_code_fenced_json():
    response = "Sure, here are the relevant articles:\n```json\n[\"concepts/foo\", \"concepts/bar\"]\n```"
    assert parse_selected_articles(response) == ["concepts/foo", "concepts/bar"]


def test_parse_selected_articles_genuine_empty_array_returns_empty():
    """An explicit `[]` is a valid answer — do not fall back to bullet parsing."""
    assert parse_selected_articles("[]") == []


def test_parse_selected_articles_returns_empty_on_garbage():
    assert parse_selected_articles("I don't think any are relevant.") == []


def test_load_selected_articles_returns_subset(tmp_path):
    kb = tmp_path / "knowledge"
    (kb / "concepts").mkdir(parents=True)
    (kb / "concepts" / "foo.md").write_text("FOO BODY", encoding="utf-8")
    (kb / "concepts" / "bar.md").write_text("BAR BODY", encoding="utf-8")
    (kb / "concepts" / "baz.md").write_text("BAZ BODY", encoding="utf-8")

    loaded = load_selected_articles(["concepts/foo", "concepts/bar"], kb)
    assert loaded == {
        "concepts/foo.md": "FOO BODY",
        "concepts/bar.md": "BAR BODY",
    }


def test_load_selected_articles_silently_drops_missing(tmp_path):
    kb = tmp_path / "knowledge"
    (kb / "concepts").mkdir(parents=True)
    (kb / "concepts" / "foo.md").write_text("FOO BODY", encoding="utf-8")
    loaded = load_selected_articles(["concepts/foo", "concepts/does-not-exist"], kb)
    assert loaded == {"concepts/foo.md": "FOO BODY"}


def test_load_selected_articles_rejects_path_traversal(tmp_path):
    kb = tmp_path / "knowledge"
    (kb / "concepts").mkdir(parents=True)
    (kb / "concepts" / "foo.md").write_text("FOO", encoding="utf-8")
    # Create a file outside knowledge dir
    outside = tmp_path / "secret.md"
    outside.write_text("SHOULD NOT LOAD", encoding="utf-8")
    loaded = load_selected_articles(["../secret", "concepts/foo"], kb)
    assert "SHOULD NOT LOAD" not in "".join(loaded.values())
    assert loaded == {"concepts/foo.md": "FOO"}


def test_load_selected_articles_caps_at_max(tmp_path):
    """A hallucinated 200-slug response must not blow up pass-2."""
    from article_selector import MAX_SELECTED_ARTICLES

    kb = tmp_path / "knowledge"
    (kb / "concepts").mkdir(parents=True)
    slugs = []
    for i in range(MAX_SELECTED_ARTICLES + 5):
        name = f"concepts/c{i:03d}"
        (kb / f"{name}.md").write_text(f"BODY-{i}", encoding="utf-8")
        slugs.append(name)

    loaded = load_selected_articles(slugs, kb)
    assert len(loaded) == MAX_SELECTED_ARTICLES
    # First MAX are loaded; remaining are dropped.
    assert "concepts/c000.md" in loaded
    assert f"concepts/c{MAX_SELECTED_ARTICLES - 1:03d}.md" in loaded
    assert f"concepts/c{MAX_SELECTED_ARTICLES:03d}.md" not in loaded


def test_first_pass_prompt_contains_index_and_log():
    prompt = build_first_pass_prompt(
        log_content="DAILY LOG TEXT",
        wiki_index="INDEX TABLE",
    )
    assert "DAILY LOG TEXT" in prompt
    assert "INDEX TABLE" in prompt
    assert "JSON array" in prompt  # ensure we ask for structured output


def test_second_pass_prompt_contains_selected_articles_only():
    prompt = build_second_pass_prompt(
        log_content="DAILY LOG TEXT",
        selected_articles={"concepts/foo.md": "FOO BODY", "concepts/bar.md": "BAR BODY"},
        schema="SCHEMA",
        knowledge_dir=Path("/tmp/kb"),
        timestamp="2026-05-17T00:00:00-05:00",
    )
    assert "FOO BODY" in prompt
    assert "BAR BODY" in prompt
    assert "BAZ BODY" not in prompt  # articles not selected
    assert "SCHEMA" in prompt


def test_second_pass_prompt_does_not_embed_wiki_index():
    """Pass-2 must NOT re-ship the 15K-token index (FATAL-1 fix)."""
    prompt = build_second_pass_prompt(
        log_content="DAILY LOG TEXT",
        selected_articles={"concepts/foo.md": "FOO BODY"},
        schema="SCHEMA",
        knowledge_dir=Path("/tmp/kb"),
        timestamp="2026-05-17T00:00:00-05:00",
    )
    assert "Current Wiki Index" not in prompt
    # Pass-2 should instruct the LLM to Read index.md if it needs to update it.
    assert "Read" in prompt or "Read tool" in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/test_article_selector.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'article_selector'`.

- [ ] **Step 3: Implement article_selector.py**

Create `scripts/article_selector.py`:

```python
"""Pure helpers for the two-pass compile.py dedup strategy.

Pass 1: LLM sees only the daily log + the wiki index (~15K tokens) and
returns a JSON array of article names it needs to inspect for dedup
or update. Capped at MAX_SELECTED_ARTICLES so a hallucinated pass-1
response cannot blow up pass-2's prompt size.

Pass 2: LLM gets the daily log + ONLY the selected subset of articles
(no index — the LLM has the Read tool and can re-fetch index.md if
needed) and produces the actual concept/connection articles.

This module is intentionally pure I/O + string assembly so it can be
unit tested without touching the Claude Agent SDK.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path


# Cap pass-2 article count. Real article avg ≈ 6.8KB ≈ 1.7K tokens;
# 12 articles ≈ 20K tokens — bounded pass-2 prompt size.
MAX_SELECTED_ARTICLES = 12

# Bullet/numbered-list fallback parser. Sonnet sometimes drifts to numbered
# prose ("1. concepts/foo") despite a JSON-only instruction.
_BULLET_RE = re.compile(
    r"^\s*(?:[-*+]\s+|\d+[.\)]\s+)?(?:\[\[)?(?P<name>[\w/.\-]+?)(?:\]\])?\s*$"
)

# Bracket-balanced JSON-array scan: tolerates one level of nested arrays
# AND code-fenced ```json [...] ``` blocks.
_JSON_ARRAY_RE = re.compile(r"\[(?:[^\[\]]|\[[^\[\]]*\])*\]", flags=re.DOTALL)


def parse_selected_articles(response: str) -> list[str]:
    """Extract article slugs from the first-pass LLM response.

    Tries JSON array first; on failure, falls back to bullet/numbered
    lists. Returns slugs with any trailing `.md` stripped. Returns []
    for unparseable responses; the caller will then fall back to a
    no-context second pass, which is degraded but not broken.

    Logs WARNING-level diagnostics so operators can distinguish "LLM
    returned []" from "parser couldn't read the LLM's response."
    """
    response = response.strip()
    if not response:
        logging.warning("parse_selected_articles: empty response from LLM")
        return []

    # 1. Try JSON array (bracket-balanced regex tolerates code fences and
    # one level of nested arrays).
    match = _JSON_ARRAY_RE.search(response)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, list):
                slugs = [_normalize(str(item)) for item in data if str(item).strip()]
                if slugs or match.group(0).strip() == "[]":
                    # Genuine [] is a valid answer; don't fall back.
                    return slugs
        except json.JSONDecodeError as exc:
            logging.warning(
                "parse_selected_articles: JSON decode failed (%s); response head: %r",
                exc,
                response[:200],
            )

    # 2. Fall back: bullet or numbered list with one slug per line
    fallback_slugs: list[str] = []
    for line in response.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        name = m.group("name")
        if "/" in name:  # concept slugs always have a subdir prefix
            fallback_slugs.append(_normalize(name))

    if not fallback_slugs:
        logging.warning(
            "parse_selected_articles: no JSON and no bullet/numbered list found; "
            "treating as empty. Response head: %r",
            response[:200],
        )
    else:
        logging.info(
            "parse_selected_articles: bullet/numbered-list fallback parsed %d slugs",
            len(fallback_slugs),
        )
    return fallback_slugs


def _normalize(slug: str) -> str:
    slug = slug.strip().strip("\"'`")
    if slug.endswith(".md"):
        slug = slug[:-3]
    return slug


def load_selected_articles(
    slugs: list[str], knowledge_dir: Path
) -> dict[str, str]:
    """Load the requested article files, keyed by their relative path.

    - Caps loading at MAX_SELECTED_ARTICLES (pass-2 prompt budget guard).
    - Silently drops slugs that don't exist on disk (hallucinated names
      do not crash compilation).
    - Rejects any slug that escapes knowledge_dir via path traversal.
    """
    knowledge_dir = knowledge_dir.resolve()
    capped = slugs[:MAX_SELECTED_ARTICLES]
    if len(slugs) > MAX_SELECTED_ARTICLES:
        logging.info(
            "load_selected_articles: capped %d → %d slugs (MAX_SELECTED_ARTICLES)",
            len(slugs),
            MAX_SELECTED_ARTICLES,
        )

    result: dict[str, str] = {}
    for slug in capped:
        rel = f"{slug}.md"
        candidate = (knowledge_dir / rel).resolve()
        try:
            candidate.relative_to(knowledge_dir)
        except ValueError:
            continue  # path traversal — refuse
        if not candidate.exists() or not candidate.is_file():
            continue
        result[rel] = candidate.read_text(encoding="utf-8")
    return result


def build_first_pass_prompt(log_content: str, wiki_index: str) -> str:
    """Prompt the LLM to return only the article slugs it actually needs."""
    return f"""You will compile the daily log below into wiki articles. Before
you compile, identify which existing wiki articles you need to read in
full (because the daily log mentions them, updates them, or might
duplicate them).

Respond with ONLY a JSON array of article slugs, no prose, no code fences.
Return at most {MAX_SELECTED_ARTICLES} most-relevant slugs. Example:

["concepts/foo-bar", "connections/foo-and-baz"]

If no existing articles are relevant, respond with `[]`.

## Wiki Index (titles + summaries only)

{wiki_index}

## Daily Log

{log_content}
"""


def build_second_pass_prompt(
    log_content: str,
    selected_articles: dict[str, str],
    schema: str,
    knowledge_dir: Path,
    timestamp: str,
) -> str:
    """The compile prompt, given ONLY the articles selected in pass 1.

    The wiki index is NOT included — the LLM has the Read tool and will
    re-fetch knowledge/index.md if it actually needs to update an index
    entry. This saves ~15K tokens per compile.
    """
    if selected_articles:
        parts = []
        for rel_path, content in selected_articles.items():
            parts.append(f"### {rel_path}\n```markdown\n{content}\n```")
        existing_articles_context = "\n\n".join(parts)
    else:
        existing_articles_context = "(No relevant existing articles selected for this log)"

    concepts_dir = knowledge_dir / "concepts"
    connections_dir = knowledge_dir / "connections"

    return f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Selected Existing Articles (chosen by first-pass dedup)

{existing_articles_context}

## Daily Log to Compile

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

If you need to update {knowledge_dir / 'index.md'}, use the Read tool to
fetch its current contents first — it is intentionally not included in
this prompt to keep cost down.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter
5. **Update knowledge/index.md** - Add new entries to the table (Read it first)
6. **Append to knowledge/log.md** - Add a timestamped entry

### File paths:
- Write concept articles to: {concepts_dir}
- Write connection articles to: {connections_dir}
- Update index at: {knowledge_dir / 'index.md'}
- Append log at: {knowledge_dir / 'log.md'}

Timestamp for this compile: {timestamp}
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest tests/test_article_selector.py -v`
Expected: 13 passed.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/article_selector.py tests/test_article_selector.py
git commit -m "feat: add article_selector for two-pass compile dedup"
```

---

## Task 6: Wire two-pass into compile.py

**Files:**
- Modify: `scripts/compile.py:35-163`

- [ ] **Step 1: Refactor compile_daily_log to use two passes**

Replace the body of `compile_daily_log` in `scripts/compile.py`. Specifically, replace lines 35-163 (the entire async function) with the implementation below. Keep the imports at the top of the file unchanged; add the `article_selector` and `retry` imports near the top.

Add these imports near the top of `scripts/compile.py` (alongside existing `from utils import ...`):

```python
from article_selector import (
    build_first_pass_prompt,
    build_second_pass_prompt,
    load_selected_articles,
    parse_selected_articles,
)
from retry import RetryExhausted, run_with_retry
```

Then replace the body of `compile_daily_log`:

```python
async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles via two-pass dedup.

    Pass 1: send only the wiki index to the LLM; ask which existing
    articles it needs to read in full. Pass 2: send the daily log plus
    just those articles (NOT the index — the LLM has Read tool) so the
    model can write/update without paying the cost of carrying the
    entire corpus.

    Idempotent: returns 0.0 immediately if the log's sha256 matches the
    ingested record. This means a re-run of `compile.py --file daily/X.md`
    on an unchanged file makes ZERO API calls and does NOT mutate state.

    Cost accounting: pass-1 + pass-2 costs are summed on success. On
    pass-2 failure after pass-1 succeeded, pass-1 cost is recorded in
    state["partial_costs"][] (a list of failed attempts) so it isn't lost,
    but the log is NOT marked ingested — next run will retry.

    Both passes are wrapped in run_with_retry with a transient-only
    retry_on tuple. Auth/400/permission errors fail fast.

    Returns the API cost of the compilation (both passes combined).
    """
    import asyncio
    from subprocess import SubprocessError
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    RETRYABLE: tuple[type[BaseException], ...] = (
        ConnectionError,
        TimeoutError,
        OSError,
        SubprocessError,
        asyncio.TimeoutError,
    )

    # ── Idempotency: hash-equality early-return ────────────────────────
    rel_path = log_path.name
    prev = state.get("ingested", {}).get(rel_path, {})
    current_hash = file_hash(log_path)
    if (
        prev.get("hash") == current_hash
        and prev.get("cost_usd", 0.0) > 0.0
    ):
        print(f"  Skipping {rel_path}: hash unchanged (already compiled at ${prev['cost_usd']:.4f})")
        return 0.0

    log_content = log_path.read_text(encoding="utf-8")
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()
    timestamp = now_iso()

    # ── Pass 1: ask which articles are relevant ────────────────────────
    first_pass_prompt = build_first_pass_prompt(
        log_content=log_content,
        wiki_index=wiki_index,
    )

    async def _pass1() -> tuple[str, float]:
        response_text = ""
        cost = 0.0
        async for message in query(
            prompt=first_pass_prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                allowed_tools=[],  # no tools — we just want JSON back
                max_turns=2,
                model="sonnet",
                fallback_model="haiku",
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text += block.text
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
        return response_text, cost

    first_pass_response = ""
    cost_pass1 = 0.0
    try:
        first_pass_response, cost_pass1 = await run_with_retry(
            _pass1, max_attempts=3, base_delay=5.0, retry_on=RETRYABLE,
        )
    except RetryExhausted as exhausted:
        print(f"  Pass 1 exhausted retries ({exhausted.attempts}): {exhausted.last_error!r}; falling back to no-context compile")
        first_pass_response = "[]"
        cost_pass1 = 0.0
    except Exception as e:
        print(f"  Pass 1 non-retryable error: {e}; falling back to no-context compile")
        first_pass_response = "[]"
        cost_pass1 = 0.0

    selected_slugs = parse_selected_articles(first_pass_response)
    selected_articles = load_selected_articles(selected_slugs, KNOWLEDGE_DIR)
    print(f"  Pass 1: selected {len(selected_articles)}/{len(selected_slugs)} articles (cost ${cost_pass1:.4f})")

    # ── Pass 2: compile with only the selected articles ────────────────
    # The wiki_index is intentionally NOT passed here — the LLM has Read.
    second_pass_prompt = build_second_pass_prompt(
        log_content=log_content,
        selected_articles=selected_articles,
        schema=schema,
        knowledge_dir=KNOWLEDGE_DIR,
        timestamp=timestamp,
    )

    async def _pass2() -> float:
        cost = 0.0
        async for message in query(
            prompt=second_pass_prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # LLM writes files directly via tools
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
        return cost

    cost_pass2 = 0.0
    pass2_failed = False
    pass2_error: BaseException | None = None
    try:
        cost_pass2 = await run_with_retry(
            _pass2, max_attempts=3, base_delay=5.0, retry_on=RETRYABLE,
        )
    except RetryExhausted as exhausted:
        pass2_failed = True
        pass2_error = exhausted.last_error
        print(f"  Pass 2 exhausted retries ({exhausted.attempts}): {exhausted.last_error!r}")
    except Exception as e:
        pass2_failed = True
        pass2_error = e
        print(f"  Pass 2 non-retryable error: {e}")

    if pass2_failed:
        # Partial-success path: pass-1 cost was paid but pass-2 failed.
        # Record cost so it isn't lost; do NOT mark ingested (next run
        # will retry the whole pipeline).
        if cost_pass1 > 0.0:
            state.setdefault("partial_costs", []).append({
                "file": rel_path,
                "cost_pass1": cost_pass1,
                "failed_at": now_iso(),
                "error": type(pass2_error).__name__ if pass2_error else "unknown",
            })
            state["total_cost"] = state.get("total_cost", 0.0) + cost_pass1
            save_state(state)
        return cost_pass1

    print(f"  Pass 2: cost ${cost_pass2:.4f}")
    total = cost_pass1 + cost_pass2

    # ── Update state idempotently ──────────────────────────────────────
    # Re-running on the same hash is a no-op (early return above).
    # total_cost increment counts THIS run only.
    state.setdefault("ingested", {})[rel_path] = {
        "hash": current_hash,
        "compiled_at": now_iso(),
        "cost_usd": total,
        "selected_articles": list(selected_articles.keys()),  # for audit
    }
    state["total_cost"] = state.get("total_cost", 0.0) + total
    save_state(state)

    return total
```

- [ ] **Step 2: Run all tests (regression)**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest -v`
Expected: all prior tests still pass; no new ones added in this task (compile.py exercises an LLM and is integration-tested live).

- [ ] **Step 3: Smoke-test compile.py with --all --dry-run**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python scripts/compile.py --all --dry-run`
Expected: lists ALL daily logs as candidates (so the smoke-test exercises the to_compile selection code path even on a fully-ingested state.json); does NOT make API calls; exits cleanly. The plain `--dry-run` (without `--all`) prints "Nothing to compile" on the current fully-ingested state and validates nothing.

- [ ] **Step 4: Live cost-reduction test (3 small logs, idempotent re-run)**

The current `state.json` baseline (read at the time this plan was written): **min $0.84, max $11.88, avg $4.49** per compile across 50 ingested logs. The two-pass design should land **median ≤ $1.50 on small (<20KB) logs** and **median ≤ $2.50 on median-size logs**. We run 3 logs back-to-back to smooth out single-shot variance.

First back up state.json so the test is recoverable:

```bash
cd /home/faxik/tools/claude-memory-compiler
cp scripts/state.json scripts/state.json.bak-$(date +%Y%m%d-%H%M%S)
```

Pick 3 small daily logs that are already in `state.json` (so the **hash-equality early-return** is exercised first — confirms idempotency, no API calls, no money spent):

```bash
ls -laS daily/*.md | tail -10
```

For 3 chosen small logs, first run them once to verify idempotency:

```bash
for f in daily/SMALL1.md daily/SMALL2.md daily/SMALL3.md; do
    uv run python scripts/compile.py --file "$f"
done
```

Expected: each prints `Skipping <name>: hash unchanged (already compiled at $X)`. Zero API calls. `state.json` unchanged.

Now force a real two-pass compile by temporarily clearing the `ingested` entry for the 3 chosen logs (do this in-memory via a tiny script):

```bash
uv run python -c "
import json
from pathlib import Path
p = Path('scripts/state.json')
state = json.loads(p.read_text())
for name in ('SMALL1.md', 'SMALL2.md', 'SMALL3.md'):  # edit these
    state.get('ingested', {}).pop(name, None)
p.write_text(json.dumps(state, indent=2))
print('cleared 3 entries')
"
```

Then run the 3 compiles back-to-back and capture per-compile cost:

```bash
for f in daily/SMALL1.md daily/SMALL2.md daily/SMALL3.md; do
    uv run python scripts/compile.py --file "$f" 2>&1 | tee -a /tmp/compile-3run.log
done
grep -E "Pass [12]:" /tmp/compile-3run.log
```

Verify (the cost gate):
- Each line `Pass 1: selected N/M articles (cost $X)` shows `X ≤ ~$0.05` (index-only first pass is cheap).
- Each line `Pass 2: cost $Y` shows the larger fraction of the total.
- **Sum** of pass-1 + pass-2 per log = total. Take the median of the 3 totals.
- **Pass criterion:** median total ≤ $1.50 for small logs (≤ $2.50 for median-size logs). If median is within 0.8× of the $4.49 baseline, the cost-reduction goal failed — capture all 3 totals and revisit the prompt design before merging.

Then restore state.json:

```bash
cp scripts/state.json.bak-* scripts/state.json
```

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/compile.py
git commit -m "feat(compile): two-pass index-only dedup cuts cost ~10x"
```

---

## Task 7: Final validation

- [ ] **Step 1: Full test suite**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run pytest -v`
Expected: all tests pass.

- [ ] **Step 2: Lint check (if ruff is configured)**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run ruff check scripts/ hooks/ tests/`
Expected: no errors. Fix any flagged issues before proceeding.

- [ ] **Step 3: Manual end-to-end of the flush path**

Run a short Claude Code session in any project, end it normally, then inspect:

```bash
tail -50 /home/faxik/tools/claude-memory-compiler/scripts/flush.log
ls -la /home/faxik/tools/claude-memory-compiler/scripts/flush-stderr.*.log
tail -50 /home/faxik/tools/claude-memory-compiler/daily/$(date +%Y-%m-%d).md
```

Expected:
- `flush.log` shows `Spawned flush.py for session ...` and either `Result: saved to daily log` or `Result: FLUSH_OK`.
- One or more `flush-stderr.<pid>.log` files exist (per-PID — each hook invocation gets its own file to avoid interleaving). May be empty if no errors — that's good.
- Today's daily log has the new session entry.

- [ ] **Step 4: Verify retry behavior with a simulated failure (optional)**

This is harder to test in production. Skip if no convenient way; rely on the unit tests in `tests/test_retry.py`.

- [ ] **Step 5: Update the concept article**

Edit `knowledge/concepts/claude-memory-compiler-setup.md`. In the "Five structural gaps identified" section, append a new paragraph after the list:

```markdown
## Stderr Capture + Retry + Two-Pass Dedup (May 17, 2026)

Three observability/cost fixes shipped together:

1. **Stderr capture** — `hooks/session-end.py` and `hooks/pre-compact.py` now route flush.py stderr to per-PID `scripts/flush-stderr.<pid>.log` files instead of `/dev/null`. Per-PID avoids interleaving when SessionEnd + PreCompact fire concurrently (Linux append-mode is atomic only up to PIPE_BUF = 4096B; Agent SDK tracebacks routinely exceed that). The "Command failed with exit code 1 — check stderr" errors are now actually diagnosable.

2. **Retry-with-backoff (transient-only)** — `scripts/retry.py` wraps the Agent SDK `query()` loop with 3 attempts at 2s/4s/8s backoff for flush.py (5s base for compile.py). Crucially, retry_on is a **whitelist** of (ConnectionError, TimeoutError, OSError, SubprocessError, asyncio.TimeoutError) — auth/400/permission errors fail fast instead of being retried for no reason. `RateLimitError` honors `Retry-After` header if present (else 30s fallback) to avoid amplifying rate-limit windows. The 2026-04-12 "Control request timeout: initialize" cluster should now be absorbed; the 2026-05-13 rapid-fire failure burst — if it was a rate-limit cluster — will no longer be amplified by naive exponential backoff.

3. **Two-pass compile dedup + hash-equality idempotency** — `scripts/compile.py` replaces the "ship all ~220 articles as context" strategy (~395K tokens per compile) with: pass 1 sends only `index.md` (~15K tokens) and asks the LLM for a JSON array of relevant article slugs; pass 2 fetches just those (capped at 12) and runs the actual compile WITHOUT re-shipping the index (the LLM has the Read tool). A hash-equality gate at the top of `compile_daily_log` makes re-runs on unchanged logs a zero-cost no-op. Cost reduction: **state.json baseline $0.84–$11.88 avg $4.49 → target ≤ $1.50 median on small logs, ≤ $2.50 median on median-size logs**. State now records `selected_articles` per compile for audit and a `partial_costs` list for pass-2 failures that paid pass-1.

Closes 3 of the 5 structural gaps identified May 13. Remaining open: dead connections (0-byte placeholders), dead QA loop, no provenance/confidence on articles, LLM-only lint. Followup codebug: actual log rotation for `flush.log` (28K+ lines) and `flush-stderr.<pid>.log` (currently unbounded append).
```

- [ ] **Step 6: Commit the documentation update**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add knowledge/concepts/claude-memory-compiler-setup.md
git commit -m "docs: record stderr capture + retry + two-pass dedup fixes"
```

---

## Out of Scope (deferred)

- **Connections empty-dir guard** — needs investigation of which guard fires and whether the seed files should have valid frontmatter.
- **QA loop wiring** — `query.py` exists at 138 LOC; needs a hook trigger and a question-extraction prompt.
- **Structural lint pre-pass** — frontmatter validation, dead `[[wiki-link]]` detection, orphan article detection before LLM lint.
- **Article provenance/confidence** — schema change to YAML frontmatter.
- **Batch-flush rate limiting** — separate from per-flush retry; pre-empts the rapid-fire burst pattern at a different layer.
- **Actual log rotation** for `scripts/flush.log` (28K+ lines today) and `scripts/flush-stderr.<pid>.log` files. v1 uses plain append; followup will swap in `logging.handlers.RotatingFileHandler` (5MB × 3 backups) or wire `logrotate`.
- **Prompt-cache alignment between passes** — pass-1 and pass-2 have different system prompts (no-tools vs `claude_code` preset) so cross-pass caching is structurally impossible. Within-pass caching across multiple compiles in a batch may still help — measure and revisit if cost stays above the gate.

Each is a clean follow-up that can ship independently of this plan.

---

## Adversarial Review Corrections (2026-05-17)

The first draft of this plan went through `/adversarial-review` (Adversary → Defender → Judge, all model=opus). Verdict: design-health **6/10 — rework-then-ship**. The Judge upheld 1 SERIOUS (originally FATAL, downgraded), 7 SERIOUS, 7 WEAKNESS, and dismissed 4 NITPICKs. All 10 mandatory fixes have been applied inline above. Summary of what changed and why:

1. **Token-budget and baseline numbers corrected.** `knowledge/index.md` was claimed as "~5K tokens" — actually **15K tokens (59,156 bytes)**. Cost baseline was claimed as "$0.45-$0.65" — actually **$0.84-$11.88, avg $4.49** per `state.json`. Success criterion is now median over 3 runs ≤ $1.50 small / ≤ $2.50 median, not a single-shot "3x" claim.
2. **Pass-2 prompt no longer re-ships the wiki index.** `build_second_pass_prompt` lost the `wiki_index` parameter; the prompt instructs the LLM to `Read` `knowledge/index.md` only if it needs to update the index entry. Saves ~15K tokens per compile.
3. **Pass-2 failure path saves partial cost.** When pass-1 succeeds and pass-2 raises, we now append to `state["partial_costs"]` and increment `total_cost` (no `ingested` hash → next run retries). Old plan returned silently and leaked money.
4. **Retry is whitelist-only.** `run_with_retry`'s default `retry_on` is `()`; callers MUST pass a transient-error tuple. `_rate_limit_delay()` reads `Retry-After` for `RateLimitError` by class name (no SDK import). Both flush.py and compile.py pass `(ConnectionError, TimeoutError, OSError, SubprocessError, asyncio.TimeoutError)`.
5. **Both compile passes wrapped in retry.** Earlier plan only added retry to flush.py despite compile.py having 2x the API surface. Pass-1 uses `max_attempts=3, base_delay=5.0`; pass-2 same.
6. **Hash-equality gate at top of `compile_daily_log`.** Re-running on an unchanged log returns 0.0 with no API calls — the smoke-test step is now truly safe (previous spec lied about a "hash check" that didn't exist).
7. **`DETACHED_PROCESS` warning preserved** in both `hooks/session-end.py` and `hooks/pre-compact.py` at the call site AND in `build_flush_popen_kwargs` docstring. Old plan's wholesale block-replacement deleted load-bearing institutional knowledge about Windows Agent SDK I/O.
8. **`_BULLET_RE` accepts numbered lists** (`1.`, `1)`) — Sonnet drifts to numbered prose even when asked for JSON. Added matching test.
9. **"Rotating log file" language dropped** from the preamble. No rotation in v1 (deferred to a followup codebug); the docs no longer overstate the implementation.
10. **Per-PID stderr log file** (`flush-stderr.<pid>.log`). Avoids `O_APPEND` interleaving when SessionEnd + PreCompact fire concurrently and emit multi-KB tracebacks.

Recommended fixes also folded in: bracket-balanced JSON regex (tolerates code-fenced output), `MAX_SELECTED_ARTICLES=12` cap, parse-failure diagnostic logging, `ValueError` on `max_attempts<=0`, blank-`retry_on` rejected, dropped dead `time` import note, smoke-test uses `--all --dry-run`.

Dismissed adversary findings:
- **`_BULLET_RE` looseness** — the `if "/" in name` slash-filter is the actual safety net; loose regex is intentional.
- **Lazy `claude_agent_sdk` import inside function** — keeps unit-test collection fast; style preference, not a bug.
- **Explicit kwargs in `build_second_pass_prompt`** — required for testability with `Path("/tmp/...")` fixtures.
