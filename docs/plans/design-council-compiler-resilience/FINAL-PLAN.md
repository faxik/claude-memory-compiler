# Implementation Plan: Slice 1 — Compiler Observability

> **Scope:** Slice 1 only (the "ship today, ~1 hour" tactical observability fix from the design council α-split decision). Slice 2 (A1 retry + C1 park + content-hash dedup) is designed in `FINAL-DESIGN.md` but DEFERRED until a few days of Slice-1 evidence accumulates to ground the CLASSIFICATIONS table in real stderr patterns.

**Goal:** Replace the useless `FLUSH_ERROR: Exception: Command failed with exit code 1` daily-log line with a diagnostic-rich format that includes `exit_code`, `stderr_tail`, and an `attempts` field (=1 for now; reserved for Slice 2). Make failures triage-able without changing recovery behavior.

**Acceptance criterion:** After a flush failure, the daily log entry contains enough information that the user can grep for `exit_code=1` and `rate.?limit` patterns in `stderr_tail:` lines without re-running the failed flush.

---

## Files to Create/Modify

| File | Change |
|---|---|
| `scripts/flush.py:74-149` | Refactor `run_flush()` — add bounded stderr deque, enrich the FLUSH_ERROR response string |
| `scripts/test_flush_error_format.py` | New file — 7 `unittest.TestCase` tests covering the new format (synchronous; the helper under test is not async) |

**No new dependencies.** Uses stdlib `collections.deque` and `unittest`. Respects problem brief constraint #6 (no pytest dep).

**No `state.json` schema change.** Slice 1 is pure logging-format enhancement.

**No retry logic added.** Slice 1 is observability only. Failures still occur once and write FLUSH_ERROR. They just write it usefully now.

---

## Implementation Steps

### Step 1 — Modify `_log_stderr` callback in `flush.py` to feed a bounded deque

**File:** `/home/faxik/tools/claude-memory-compiler/scripts/flush.py`

**Locate** (around line 119-120, inside `run_flush`):
```python
        def _log_stderr(line: str) -> None:
            logging.error("[bundled CLI stderr] %s", line.rstrip())
```

**Replace with:**
```python
        # Bounded stderr capture for FLUSH_ERROR diagnostics.
        # The bundled CLI's stderr arrives via this callback (NOT via the
        # exc.stderr attribute — that one is hardcoded boilerplate at
        # claude_agent_sdk/_internal/transport/subprocess_cli.py:613-616).
        # We keep the last 50 lines / ~4KB; that's enough to catch
        # rate-limit text without bloating the daily log.
        stderr_tail: deque[str] = deque(maxlen=50)

        def _log_stderr(line: str) -> None:
            stripped = line.rstrip()
            logging.error("[bundled CLI stderr] %s", stripped)
            stderr_tail.append(stripped)
```

Add `from collections import deque` to the imports near the top of the file (around line 19, alongside `import json`).

### Step 2 — Enrich the FLUSH_ERROR response string

**Locate** (lines 144-147 in `run_flush`):
```python
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = f"FLUSH_ERROR: {type(e).__name__}: {e}"
```

**Replace with:**
```python
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        response = _build_flush_error_response(e, stderr_tail, attempts=1)
```

### Step 3 — Add the `_build_flush_error_response` helper at module scope

**Locate** the top of `flush.py` (just before `def run_flush(context: str) -> str:` at line 74).

**Insert:**
```python
def _build_flush_error_response(
    exc: BaseException,
    stderr_tail: deque[str] | None = None,
    *,
    attempts: int = 1,
) -> str:
    """Build a diagnostic-rich FLUSH_ERROR line for the daily log.

    Captured signals (all best-effort — missing fields are omitted, not
    rendered as 'None'):
      - exc class name (e.g., ProcessError, Exception)
      - first 300 chars of str(exc) ("message:")
      - exc.exit_code if a ProcessError attribute is present
      - exc.stderr if present (note: SDK hardcodes this to boilerplate;
        we record it anyway because Slice 2 may need it)
      - stderr_tail (last 50 lines from the options.stderr callback)
      - attempts field (=1 for Slice 1; Slice 2 wires real retry attempts)

    Format is line-oriented + greppable. The first line still starts with
    'FLUSH_ERROR:' so existing dispatch logic at flush.py:244 still works.
    """
    parts: list[str] = []
    parts.append(f"FLUSH_ERROR: {type(exc).__name__}")

    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    parts.append(f"attempts={attempts}")

    header = " | ".join(parts)
    body: list[str] = [header]

    msg = str(exc)[:300]
    if msg.strip():
        body.append(f"  message: {msg}")

    sdk_stderr = getattr(exc, "stderr", None)
    if sdk_stderr:
        # Known boilerplate from claude_agent_sdk.subprocess_cli:613 is
        # "Check stderr output for details" — record anyway, harmless.
        body.append(f"  sdk_stderr: {str(sdk_stderr)[:200]}")

    if stderr_tail:
        body.append("  stderr_tail:")
        for line in stderr_tail:
            body.append(f"    {line}")

    return "\n".join(body)
```

### Step 4 — Run the existing test to confirm no regression

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -m unittest scripts.test_utils_state -v`
Expected: existing tests pass.

Then sanity-check that `flush.py` imports cleanly:

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import flush"`
Expected: no output, exit 0.

### Step 5 — Add the new test file (TDD-after, since we have no fixture for the live SDK)

**Create:** `/home/faxik/tools/claude-memory-compiler/scripts/test_flush_error_format.py`

```python
"""Tests for _build_flush_error_response (Slice 1 observability).

Uses unittest (stdlib) — pyproject.toml has no pytest dep and the
problem brief's constraint #6 forbids adding one.
"""
from __future__ import annotations

import sys
import unittest
from collections import deque
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from flush import _build_flush_error_response  # noqa: E402


class _FakeProcessError(Exception):
    """Mimics claude_agent_sdk.ProcessError shape without importing it."""

    def __init__(self, message: str, *, exit_code: int, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class TestFlushErrorFormat(unittest.TestCase):
    def test_includes_exit_code_when_process_error(self):
        exc = _FakeProcessError("Command failed with exit code 1", exit_code=1)
        response = _build_flush_error_response(exc, deque(), attempts=1)
        self.assertIn("FLUSH_ERROR: _FakeProcessError", response)
        self.assertIn("exit_code=1", response)
        self.assertIn("attempts=1", response)

    def test_omits_exit_code_for_bare_exception(self):
        exc = Exception("Control request timeout: initialize")
        response = _build_flush_error_response(exc, deque(), attempts=1)
        self.assertIn("FLUSH_ERROR: Exception", response)
        self.assertNotIn("exit_code=", response)
        self.assertIn("message: Control request timeout: initialize", response)

    def test_includes_stderr_tail_when_callback_fed(self):
        tail: deque[str] = deque(maxlen=50)
        for line in ("rate limit hit", "retry-after: 30", "giving up"):
            tail.append(line)
        exc = _FakeProcessError("Command failed", exit_code=1)
        response = _build_flush_error_response(exc, tail, attempts=1)
        self.assertIn("stderr_tail:", response)
        self.assertIn("    rate limit hit", response)
        self.assertIn("    retry-after: 30", response)
        self.assertIn("    giving up", response)

    def test_truncates_stderr_tail_to_50_lines(self):
        # The deque(maxlen=50) does the truncation; verify the renderer
        # writes exactly what's in the deque.
        tail: deque[str] = deque(maxlen=50)
        for i in range(1000):
            tail.append(f"line_{i:04d}")
        exc = _FakeProcessError("Command failed", exit_code=1)
        response = _build_flush_error_response(exc, tail, attempts=1)
        # First retained line should be #950 (0-indexed: deque keeps last 50)
        self.assertIn("    line_0950", response)
        self.assertIn("    line_0999", response)
        # Pre-window lines must be gone
        self.assertNotIn("    line_0000", response)
        self.assertNotIn("    line_0949", response)
        # And we must not have more than 50 indented stderr lines
        indented_count = sum(
            1 for line in response.splitlines() if line.startswith("    line_")
        )
        self.assertEqual(indented_count, 50)

    def test_preserves_existing_regex_parseability(self):
        """Downstream parsers grep for `FLUSH_ERROR:` at the start of a line."""
        exc = Exception("Control request timeout")
        response = _build_flush_error_response(exc, deque(), attempts=1)
        first_line = response.splitlines()[0]
        self.assertTrue(first_line.startswith("FLUSH_ERROR:"))

    def test_truncates_long_message_to_300_chars(self):
        exc = Exception("x" * 500)
        response = _build_flush_error_response(exc, deque(), attempts=1)
        # Should contain 300 x's plus the "message: " prefix, not 500.
        message_line = [l for l in response.splitlines() if l.startswith("  message:")][0]
        # 2 leading spaces + "message: " (9 chars) + 300 x's = 311 chars
        self.assertEqual(len(message_line), 2 + 9 + 300)

    def test_records_sdk_stderr_even_when_boilerplate(self):
        """SDK hardcodes exc.stderr to 'Check stderr output for details'.
        Slice 2 may need this signal; record it now so the field is stable."""
        exc = _FakeProcessError(
            "Command failed",
            exit_code=1,
            stderr="Check stderr output for details",
        )
        response = _build_flush_error_response(exc, deque(), attempts=1)
        self.assertIn("sdk_stderr: Check stderr output for details", response)


if __name__ == "__main__":
    unittest.main()
```

### Step 6 — Run the new tests

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -m unittest scripts.test_flush_error_format -v`
Expected: 7 tests pass.

### Step 7 — Smoke test against a real failure

Trigger a flush failure manually by writing a context file and invoking `flush.py` with an invalid environment (or a deliberately-failing prompt). Inspect today's daily log:

```bash
tail -80 /home/faxik/tools/claude-memory-compiler/daily/$(date +%Y-%m-%d).md
```

Expected: the FLUSH_ERROR section now contains:
- `FLUSH_ERROR: <ClassName> | exit_code=<N> | attempts=1` header line
- `  message: …` body
- `  stderr_tail:` followed by indented stderr lines (or just no `stderr_tail:` block if the callback never fired — e.g., on fast-fail `ProcessError`)

If the manual smoke test isn't practical, skip — the unit tests cover the format.

### Step 8 — Update the concept article

**Edit:** `/home/faxik/tools/claude-memory-compiler/knowledge/concepts/claude-memory-compiler-setup.md`

Append after the existing "Five structural gaps" section:

```markdown
## Slice 1: Observability (May 17, 2026)

Following a design-council deliberation (`docs/plans/design-council-compiler-resilience/`), the previous round-2 retry plan was rejected by the council's adversarial review — type-based retry whitelists cannot work against the SDK's bare-`Exception` raises. The replacement architecture is an α-split:

- **Slice 1 (shipped):** `scripts/flush.py`'s FLUSH_ERROR line now carries `exit_code`, `stderr_tail` (last 50 lines from the SDK's stderr callback), and an `attempts` field. The 1637/1648 (99.3%) `Exception("Command failed with exit code 1")` mystery is no longer a mystery — operators can grep the daily log for rate-limit signatures and triage without re-running.
- **Slice 2 (deferred to next session):** A1-style in-process retry with a classification table grounded in Slice-1 evidence + C1-style filesystem park (`scripts/parked/`) + a SessionStart-triggered drainer + content-hash dedup at `append_to_daily_log`. Full design in `FINAL-DESIGN.md`.

The Slice-2 deferral is itself a design decision: avoiding speculative CLASSIFICATIONS regexes (`r"rate.?limit|429"`) until we have a corpus of real stderr signatures from the user's actual rate-limit events.
```

### Step 9 — Commit

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/flush.py scripts/test_flush_error_format.py knowledge/concepts/claude-memory-compiler-setup.md docs/plans/design-council-compiler-resilience/
git commit -m "feat(flush): structured FLUSH_ERROR with exit_code + stderr_tail (Slice 1)"
```

---

## Testing Strategy

- **Unit tests:** 7 synchronous `unittest.TestCase` tests in `scripts/test_flush_error_format.py` exercise: process-error exit_code, bare-Exception fallback, stderr-tail rendering, 50-line truncation, regex parseability, message truncation, boilerplate sdk_stderr capture. Run via `python -m unittest scripts.test_flush_error_format -v`. (The helper under test is synchronous — no asyncio involvement needed.)
- **Smoke test:** manual trigger (Step 7) — optional; the new format is verifiable without a real failure.
- **Regression test:** existing `scripts/test_utils_state.py` confirms no `flush.py` import-time regression.
- **Production validation:** after merge, monitor `daily/2026-05-*.md` for the first FLUSH_ERROR line. The next time the 2026-04-12-style "Control request timeout" cluster recurs, we'll have a recorded stderr signature to design Slice-2's CLASSIFICATIONS table against.

---

## Rollback Plan

If the new format breaks anything downstream:

```bash
cd /home/faxik/tools/claude-memory-compiler
git revert HEAD
```

The change is additive to a single function. No state.json schema change, no new files in the live data path (the test file is harmless). Revert is safe.

---

## What This Does NOT Do (Slice 2 scope)

These are the intentional non-goals of Slice 1 — see `FINAL-DESIGN.md` for the full Slice 2 design:

- **No retry on transient failures.** A "Control request timeout" still writes FLUSH_ERROR after one attempt — but now operators can see it. Slice 2 will add A1's in-process retry with a CLASSIFICATIONS table grounded in real-world Slice-1 evidence.
- **No park-and-resume.** Failed sessions still leave context files in `scripts/session-flush-*.md` (32 already accumulated since 2026-04-14). Slice 2's drainer will adopt them.
- **No dedup gate on `append_to_daily_log`.** Re-running a flush still re-appends. Slice 2 will gate by content hash.
- **No cost-budget circuit breaker.** A poison-pill that keeps failing in a retry loop could in principle re-bill — but since Slice 1 has no retry, there's no loop. Slice 2 wires the breaker as part of the retry design.

---

## Followup Tracking

After Slice 1 ships, watch the daily log for ~3-5 days. Then for Slice 2:

1. Grep all `FLUSH_ERROR` blocks for distinct `<class_name> | exit_code=<N>` headers — these become the seed CLASSIFICATIONS rows.
2. Read `stderr_tail` for each cluster — extract real regex patterns for rate-limit vs auth vs generic-crash. Replace the speculative `r"rate.?limit|429"` with grounded patterns.
3. Write the Slice 2 plan (separate file, same workspace) and run it through one more `/adversarial-review` pass before implementing.

Slice 2 plan filename target: `docs/plans/design-council-compiler-resilience/FINAL-PLAN-SLICE-2.md` (deferred).
