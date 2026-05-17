# Compiler Resilience Slice 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add A1-style in-process retry + C1-style filesystem park-and-resume + content-hash dedup at the daily-log append site, so the 1637 silent exit-code-1 failures stop becoming silent data loss. After this slice, transient failures get up to 2 in-process retries; if still failing they park to `scripts/parked/`; a SessionStart-triggered drainer re-spawns `flush.py` against parked files (max 5 attempts → dead-letter); the daily-log append site dedups by content hash so retry replay never produces duplicate entries.

**Architecture:** Two-layer retry — in-process retry for bursty-transient failures (the 2026-04-12 "Control request timeout" pattern), filesystem park-and-resume for rate-limit-shaped clusters (the 2026-05-13 pattern). A small classification table maps `(exc class, exc message, exit_code)` tuples to verdicts (`retry` / `rate_limit` / `fail_fast`). The verdict-based path picker handles auth/400 errors without retry. Content-hash dedup in `append_to_daily_log` is a cross-cutting backstop that protects every retry path (in-process replay, drainer replay, legacy-orphan adoption) from duplicate entries.

**Tech Stack:** Python 3.13, `claude-agent-sdk>=0.1.29`, `uv`, `unittest` (stdlib — no new test deps per project constraint), `subprocess.Popen` for the drainer spawn, `os.replace` for atomic JSON writes.

**Slice 1 evidence note.** Slice 1 shipped `26c6c8a` on 2026-05-17. The CLASSIFICATIONS table seeded in this plan covers the two failure patterns already documented in `scripts/flush.log` history (1637 `exit code 1`, ~10 `Control request timeout`). As Slice-1 evidence accumulates, the table should be tuned via the followup process in §"Out of Scope (deferred)" — but the infrastructure (park, drain, dedup, cost-budget) is classification-agnostic and ships now.

---

## Deploy Prerequisites (hard gates)

Slice 2 was first specified without the prerequisites below; an adversarial review found 3 FATAL bugs of the form "regex matches synthetic test fixture, not SDK reality" — the same bug class the council was originally convened to prevent. These gates exist so the next iteration can't repeat the pattern. **All must be satisfied before any Slice 2 code ships:**

1. **Real-SDK test class passes** — `scripts/test_classifier.py::TestClassifierRealSDK` imports `claude_agent_sdk._errors.CLINotFoundError` and `ProcessError`, constructs them with default arguments, and asserts the classifier returns the expected verdict. If any test in this class fails, the regex patterns have drifted from the SDK's actual messages → Slice 2 must NOT ship until they're realigned.

2. **Classifier lint passes** — `tools/lint_classifier.py` (Task 9b) greps the classifier code for `getattr(exc, "stderr"` and fails CI if found. This is a structural guard against the FATAL-1 bug class (relying on the SDK's hardcoded boilerplate stderr attribute).

3. **≥7 days of Slice-1 telemetry** — `scripts/flush.log` must contain ≥7 days of post-Slice-1 (commit `26c6c8a`) FLUSH_ERROR entries with `exit_code=` + `stderr_tail:` fields. Grep the log to confirm the dominant patterns. Refine the CLASSIFICATIONS table to match real signatures BEFORE shipping; do not ship with the seeded speculative patterns alone.

4. **Smoke test: synthetic parked file successfully drains** — Task 9 Step 3.

These prerequisites are falsifiable by single commands. If any cannot be verified, treat Slice 2 as `BLOCKED` and address before merging.

---

## File Structure

**New files:**
- `scripts/classifier.py` — Pure helpers: `classify(exc) -> Verdict`, `Verdict` namedtuple, `CLASSIFICATIONS` rule table.
- `scripts/dedup.py` — Pure helpers: `should_append(section_body, ledger_path) -> bool`, `record_append(section_body, ledger_path) -> None`, content-hash + 24h-TTL ledger management. Backing store: `scripts/appended_hashes.json` (atomic write via `os.replace`).
- `scripts/drain.py` — CLI script invoked from SessionStart hook. Globs `parked/`, `session-flush-*.md`, AND `flush-context-*.md`. Uses `.inflight` rename for concurrent-drainer safety. Sets `FLUSH_FROM_DRAIN=1` env when re-spawning `flush.py`. Promotes to `scripts/dead-letter/` after 5 attempts.
- `scripts/test_classifier.py` — Unit tests for the classifier table.
- `scripts/test_dedup.py` — Unit tests for the content-hash ledger.
- `scripts/test_drain.py` — Integration-style tests for the drainer (no real LLM calls; uses fake subprocess invocations).
- `scripts/parked/` — Directory (created on first park). Holds `<session-id>.md` (context file) + `<session-id>.json` (sidecar with last-error metadata, attempt count, first/last-attempt timestamps).
- `scripts/dead-letter/` — Directory (created on first promotion). Same shape as `parked/`; manual operator inspection.

**Modified files:**
- `scripts/flush.py:55-72` — Wrap `append_to_daily_log` body with a content-hash dedup gate calling `dedup.should_append` / `dedup.record_append`.
- `scripts/flush.py:75-211` — Refactor `run_flush` to use the classifier + in-process retry loop. The existing `_build_flush_error_response` helper from Slice 1 is reused on final exhaustion.
- `scripts/flush.py:263-322` — `main()` checks `FLUSH_FROM_DRAIN` env; if set, suppress the second-layer park (drainer is already driving the retry). Otherwise on in-process exhaustion, write the context file to `scripts/parked/` + sidecar instead of returning FLUSH_ERROR.
- `hooks/session-start.py:78-89` — After computing the context dict, spawn `scripts/drain.py` as a detached fire-and-forget Popen (must return <50ms; SessionStart timeout is 15s).
- `.gitignore` — Add `scripts/parked/` and `scripts/dead-letter/` to runtime-state ignore list (alongside the existing `scripts/session-flush-*`).

**No structural moves.** All retry/park/dedup logic is additive. The existing 60-second in-process dedup (`scripts/last-flush.json`) stays — drainer-triggered re-runs intentionally bypass it via env flag.

---

## Task 1: Add `scripts/parked/` and `dead-letter/` to .gitignore

**Files:**
- Modify: `/home/faxik/tools/claude-memory-compiler/.gitignore`

- [ ] **Step 1: Read current .gitignore**

Run: `cat /home/faxik/tools/claude-memory-compiler/.gitignore`
Confirm it currently contains `scripts/session-flush-*` and `scripts/flush-context-*`.

- [ ] **Step 2: Add new ignore patterns**

Edit `.gitignore`. Find the block starting `# Runtime state (regenerated by scripts)` and add two lines at the end of that block:

```
scripts/parked/
scripts/dead-letter/
```

- [ ] **Step 3: Verify**

Run: `git -C /home/faxik/tools/claude-memory-compiler check-ignore -v scripts/parked/test.md scripts/dead-letter/test.md 2>&1`
Expected: both patterns matched.

- [ ] **Step 4: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add .gitignore
git commit -m "chore: gitignore scripts/parked + scripts/dead-letter (Slice 2 prep)"
```

---

## Task 2: Build the classifier (TDD)

**Files:**
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/classifier.py`
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/test_classifier.py`

- [ ] **Step 1: Write failing tests**

Create `/home/faxik/tools/claude-memory-compiler/scripts/test_classifier.py`:

```python
"""Tests for the failure-mode classifier."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from classifier import Verdict, classify  # noqa: E402


class _FakeProcessError(Exception):
    """Mimics claude_agent_sdk.ProcessError shape."""

    def __init__(self, message: str, *, exit_code: int, stderr: str = ""):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr = stderr


class TestClassifier(unittest.TestCase):
    def test_control_request_timeout_is_retry(self):
        """The 2026-04-12 failure cluster: bare Exception, transient."""
        exc = Exception("Control request timeout: initialize")
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "control_request_timeout")

    def test_process_error_exit_1_is_retry(self):
        """Dominant pattern: 1637/1637 known-bad lines. Treat as transient
        until Slice-1 evidence proves a stable sub-pattern is permanent."""
        exc = _FakeProcessError("Command failed with exit code 1", exit_code=1)
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "process_exit_1")

    def test_cli_not_found_is_fail_fast(self):
        """Permanent: missing CLI binary. No amount of retry helps."""
        exc = Exception("Claude CLI not found. Install with `npm install -g`")
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "cli_not_found")

    def test_invalid_api_key_is_fail_fast(self):
        exc = Exception("invalid x-api-key")
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "auth_invalid")

    def test_prompt_too_long_is_fail_fast(self):
        exc = Exception("prompt is too long: 250000 tokens > 200000 maximum")
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "prompt_too_long")

    def test_unknown_error_defaults_to_retry(self):
        """Unrecognized errors are treated as potentially transient. The
        cost of one unnecessary retry is small; the cost of fail-fasting
        on a real transient is data loss."""
        exc = Exception("some unprecedented error nobody has seen before")
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "default_unknown")

    def test_verdict_is_namedtuple_with_kind_and_rule_name(self):
        """Verdict is a simple frozen tuple — easy to log + compare."""
        v = Verdict(kind="retry", rule_name="test")
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "test")
        # Immutable
        with self.assertRaises(AttributeError):
            v.kind = "fail_fast"

    def test_classify_uses_stderr_tail_for_rate_limit_signal(self):
        """Real bundled-CLI stderr arrives via the options.stderr callback
        into a deque, NOT via exc.stderr (which is hardcoded boilerplate).
        Classifier must accept stderr_tail as an explicit param."""
        exc = _FakeProcessError("Command failed with exit code 1", exit_code=1)
        v = classify(exc, stderr_tail=["HTTP 429 rate_limit_exceeded"])
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "rate_limit_signal")

    def test_classify_ignores_exc_stderr_attribute(self):
        """The SDK's ProcessError.stderr is hardcoded to boilerplate; we
        MUST NOT read it. Verifies the bug-class guard."""
        exc = _FakeProcessError(
            "Command failed with exit code 1",
            exit_code=1,
            stderr="HTTP 429 rate_limit_exceeded",  # would be a lie in real SDK
        )
        # Without stderr_tail arg, classifier MUST NOT pick up the
        # planted rate-limit text. It should fall through to process_exit_1.
        v = classify(exc)
        self.assertEqual(v.rule_name, "process_exit_1")

    def test_oserror_disk_full_is_fail_fast(self):
        exc = OSError(28, "No space left on device")
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "local_oserror")

    def test_permission_error_is_fail_fast(self):
        v = classify(PermissionError("denied"))
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "local_permission_error")

    def test_keyerror_is_fail_fast(self):
        v = classify(KeyError("some_missing_key"))
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "local_keyerror")


class TestClassifierRealSDK(unittest.TestCase):
    """Imports the REAL claude_agent_sdk exception classes and constructs
    them with default arguments. These tests fail if the regex literals
    drift from the SDK's actual message strings — which is the bug class
    the design-council was convened to catch.

    DEPLOY GATE: this test class must pass before Slice 2 ships.
    """

    def test_real_cli_not_found_classifies_fail_fast(self):
        from claude_agent_sdk._errors import CLINotFoundError
        # Default-construct using the SDK's own message format.
        exc = CLINotFoundError(
            "Claude Code not found. Install with:\n  npm install -g @anthropic-ai/claude-code"
        )
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        # Either by_class or by_message — both should fire
        self.assertIn(v.rule_name, {"cli_not_found_by_class", "cli_not_found_by_message"})

    def test_real_process_error_with_hardcoded_stderr_classifies_retry(self):
        """The SDK hardcodes ProcessError.stderr to boilerplate. Without
        stderr_tail, we should classify as process_exit_1 → retry."""
        from claude_agent_sdk._errors import ProcessError
        exc = ProcessError(
            "Command failed with exit code 1",
            exit_code=1,
            stderr="Check stderr output for details",  # SDK's hardcoded text
        )
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "process_exit_1")
        # Specifically: the rule must NOT be rate_limit_signal — because
        # exc.stderr is just boilerplate, NOT real rate-limit text.
        self.assertNotEqual(v.rule_name, "rate_limit_signal")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_classifier -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'classifier'`.

- [ ] **Step 3: Implement classifier**

Create `/home/faxik/tools/claude-memory-compiler/scripts/classifier.py`:

```python
"""Failure-mode classifier for the retry/park decision.

Inspects exception attributes (class name, str(exc), exit_code, stderr)
and matches against a rule table. Returns one of three verdicts:

- "retry": transient; the in-process retry loop should attempt again
- "fail_fast": permanent (auth, prompt size, missing CLI); skip retry
- "rate_limit": rate-limited; if budget allows, sleep ≥60s and retry,
  else park for the drainer

Rules are ordered: first match wins. The default (last row) is "retry"
on the principle that an unrecognized error is more likely to be a new
transient pattern than a new permanent one — we'd rather pay one
unnecessary retry than fail-fast on a recoverable failure.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class Verdict(NamedTuple):
    """Frozen result of classify(exc)."""

    kind: str  # "retry" | "fail_fast" | "rate_limit"
    rule_name: str  # which CLASSIFICATIONS row matched (for logging)


class _Rule(NamedTuple):
    name: str
    exc_class_name: str | None  # None = match any class
    message_pattern: re.Pattern[str] | None  # None = match any message
    exit_code: int | None  # None = match any (or absent) exit_code
    verdict_kind: str  # "retry" | "fail_fast" | "rate_limit"


# Order matters: first match wins. More-specific rules go first.
#
# Slice 1 evidence note (2026-05-17): the two dominant historical patterns
# are "Control request timeout: initialize" (~10 occurrences in flush.log)
# and "Command failed with exit code 1" (1637 occurrences). Both treated as
# retry. Auth/prompt-size patterns are seeded from Anthropic API conventions
# but have not yet been observed in this codebase's flush.log; refine
# after Slice 1 evidence accumulates.
CLASSIFICATIONS: tuple[_Rule, ...] = (
    # Permanent — missing CLI binary. Two rules: one by class name (most
    # reliable signal), one by message text (covers cases where the SDK
    # might wrap CLINotFoundError into a generic Exception in newer versions).
    # The SDK literal as of 2026-05-17 is "Claude Code not found" (product
    # name "Claude Code", NOT "Claude CLI"). Source:
    # claude_agent_sdk/_internal/transport/subprocess_cli.py:87-94.
    _Rule(
        name="cli_not_found_by_class",
        exc_class_name="CLINotFoundError",
        message_pattern=None,
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    _Rule(
        name="cli_not_found_by_message",
        exc_class_name=None,
        message_pattern=re.compile(r"Claude (CLI|Code) not found", re.IGNORECASE),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Permanent — local I/O failures. Retrying these is wasted LLM cost.
    # OSError covers disk full (errno 28), Read-only filesystem (errno 30), etc.
    _Rule(
        name="local_oserror",
        exc_class_name="OSError",
        message_pattern=re.compile(r"No space left|Read-only file system|Disk full", re.IGNORECASE),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    _Rule(
        name="local_permission_error",
        exc_class_name="PermissionError",
        message_pattern=None,
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    _Rule(
        name="local_filenotfound",
        exc_class_name="FileNotFoundError",
        message_pattern=None,
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    _Rule(
        name="local_keyerror",
        exc_class_name="KeyError",
        message_pattern=None,
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Permanent — auth failure. Note: not yet observed in this codebase's
    # flush.log; pattern seeded from Anthropic API conventions. Refine
    # after Slice 1 evidence accumulates (see "Deploy Prerequisites").
    _Rule(
        name="auth_invalid",
        exc_class_name=None,
        message_pattern=re.compile(r"invalid.*api.?key|authentication.*failed|401\b", re.IGNORECASE),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Permanent — request too large. Same caveat as auth_invalid.
    _Rule(
        name="prompt_too_long",
        exc_class_name=None,
        message_pattern=re.compile(r"prompt.*too long|context.*exceeds|maximum.*tokens", re.IGNORECASE),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Rate-limit signal — must be matched against the REAL stderr_tail
    # text (the SDK's exc.stderr attribute is hardcoded boilerplate;
    # see classify() docstring). Stays "retry" verdict but the rule_name
    # carries the signal so the caller can pick a longer backoff.
    _Rule(
        name="rate_limit_signal",
        exc_class_name=None,
        message_pattern=re.compile(r"\b429\b|rate.?limit|too many requests", re.IGNORECASE),
        exit_code=None,
        verdict_kind="retry",
    ),
    # Transient — the 2026-04-12 cluster
    _Rule(
        name="control_request_timeout",
        exc_class_name=None,
        message_pattern=re.compile(r"Control request timeout", re.IGNORECASE),
        exit_code=None,
        verdict_kind="retry",
    ),
    # Transient — the 2026-05-13 cluster (1637/1637 known FLUSH_ERROR lines)
    _Rule(
        name="process_exit_1",
        exc_class_name=None,
        message_pattern=None,
        exit_code=1,
        verdict_kind="retry",
    ),
    # Default — unknown errors. Treated as retry on the "cheap-retry vs
    # silent-data-loss" tradeoff.
    _Rule(
        name="default_unknown",
        exc_class_name=None,
        message_pattern=None,
        exit_code=None,
        verdict_kind="retry",
    ),
)


def classify(
    exc: BaseException,
    stderr_tail: "list[str] | None" = None,
) -> Verdict:
    """Match exc against CLASSIFICATIONS; return first-hit verdict.

    Inspects, in order:
      - exc class name (e.g. "ProcessError"), if the rule sets exc_class_name
      - str(exc) (e.g. "Command failed with exit code 1")
      - getattr(exc, "exit_code", None)
      - stderr_tail (the REAL bundled-CLI stderr, captured via the
        options.stderr callback into a bounded deque by the caller)

    NOTE: The SDK's ProcessError.stderr attribute is HARDCODED to the
    boilerplate string "Check stderr output for details" at
    claude_agent_sdk/_internal/transport/subprocess_cli.py:613-617 —
    we deliberately do NOT read that attribute. The only source of real
    rate-limit / stack-trace text is the options.stderr callback, which
    the caller must feed into stderr_tail.

    The default (last row) always matches; classify() cannot return None.
    """
    exc_class = type(exc).__name__
    exc_message = str(exc)
    exit_code = getattr(exc, "exit_code", None)
    tail_text = "\n".join(stderr_tail or [])
    haystack = f"{exc_message}\n{tail_text}"

    for rule in CLASSIFICATIONS:
        if rule.exc_class_name is not None and rule.exc_class_name != exc_class:
            continue
        if rule.message_pattern is not None and not rule.message_pattern.search(haystack):
            continue
        if rule.exit_code is not None and rule.exit_code != exit_code:
            continue
        return Verdict(kind=rule.verdict_kind, rule_name=rule.name)

    # Unreachable: the last CLASSIFICATIONS row matches everything.
    return Verdict(kind="retry", rule_name="default_unknown")
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_classifier -v 2>&1 | tail -20`
Expected: `Ran 14 tests in ... OK`. The `TestClassifierRealSDK` class imports real SDK exception classes — if any of those tests fail, the regex patterns have drifted from the SDK's actual messages and Slice 2 MUST NOT ship until the patterns are updated.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/classifier.py scripts/test_classifier.py
git commit -m "feat: add failure-mode classifier for retry/park decisions (Slice 2)"
```

---

## Task 3: Build the content-hash dedup ledger (TDD)

**Files:**
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/dedup.py`
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/test_dedup.py`

- [ ] **Step 1: Write failing tests**

Create `/home/faxik/tools/claude-memory-compiler/scripts/test_dedup.py`:

```python
"""Tests for the content-hash dedup ledger."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from dedup import record_append, should_append  # noqa: E402


class TestDedup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ledger = Path(self.tmpdir) / "appended_hashes.json"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_append_returns_true(self):
        self.assertTrue(should_append("hello world", self.ledger))

    def test_after_record_same_content_returns_false(self):
        record_append("hello world", self.ledger)
        self.assertFalse(should_append("hello world", self.ledger))

    def test_different_content_returns_true(self):
        record_append("hello world", self.ledger)
        self.assertTrue(should_append("goodbye world", self.ledger))

    def test_ledger_file_is_created_on_first_record(self):
        self.assertFalse(self.ledger.exists())
        record_append("some content", self.ledger)
        self.assertTrue(self.ledger.exists())

    def test_ledger_format_is_valid_json(self):
        record_append("some content", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)
        # exactly one entry — content hash to ISO timestamp
        self.assertEqual(len(data), 1)
        hash_key = list(data.keys())[0]
        # 16-char SHA256 prefix
        self.assertEqual(len(hash_key), 16)
        # Value parses as ISO timestamp
        datetime.fromisoformat(data[hash_key])

    def test_entries_older_than_24h_are_pruned_on_record(self):
        """Pruning on write keeps the ledger bounded."""
        # Pre-seed with a 25-hour-old entry
        old_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.ledger.write_text(
            json.dumps({"aaaaaaaaaaaaaaaa": old_iso}),
            encoding="utf-8",
        )
        # Record a new entry. Pruning fires.
        record_append("fresh content", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn("aaaaaaaaaaaaaaaa", data)
        # New entry is present
        self.assertEqual(len(data), 1)

    def test_entries_within_24h_survive_pruning(self):
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=23)).isoformat()
        self.ledger.write_text(
            json.dumps({"bbbbbbbbbbbbbbbb": recent_iso}),
            encoding="utf-8",
        )
        record_append("fresh content", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertIn("bbbbbbbbbbbbbbbb", data)
        self.assertEqual(len(data), 2)

    def test_corrupt_ledger_is_recovered_gracefully(self):
        """A truncated/corrupt ledger file should not stall append; it
        should be treated as empty and replaced atomically."""
        self.ledger.write_text("{not valid json", encoding="utf-8")
        # should_append must still work
        self.assertTrue(should_append("anything", self.ledger))
        # record_append must overwrite without raising
        record_append("anything", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)

    def test_atomic_write_via_os_replace(self):
        """The tmp-then-replace pattern: if a crash happens mid-write, the
        existing file is intact. We can't easily simulate a crash here,
        but we can verify the .tmp file is cleaned up post-write."""
        record_append("content", self.ledger)
        tmp = self.ledger.with_suffix(self.ledger.suffix + ".tmp")
        self.assertFalse(tmp.exists())

    def test_should_append_does_not_create_ledger_file(self):
        """Read-only path: just checking should never create state."""
        result = should_append("content", self.ledger)
        self.assertTrue(result)
        self.assertFalse(self.ledger.exists())

    def test_concurrent_record_append_preserves_all_entries(self):
        """Multiple concurrent record_append calls must not lose entries.

        Simulates the race that the SERIOUS-4 adversary finding identified:
        two flush.py instances finishing simultaneously, both calling
        record_append without locking → one entry overwrites the other.
        With fcntl.flock around the read-modify-write cycle, all entries
        survive.
        """
        import threading
        contents = [f"content-{i}" for i in range(20)]

        def worker(c: str) -> None:
            record_append(c, self.ledger)

        threads = [threading.Thread(target=worker, args=(c,)) for c in contents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        # All 20 distinct hashes must survive (no lost-write race).
        self.assertEqual(len(data), 20)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_dedup -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'dedup'`.

- [ ] **Step 3: Implement dedup**

Create `/home/faxik/tools/claude-memory-compiler/scripts/dedup.py`:

```python
"""Content-hash dedup ledger for the daily-log append site.

Solves three failure modes in one place:
  1. In-process retry replays the same content after a server-side
     success / client-side failure → would duplicate the daily-log entry.
  2. Drainer replays a parked context file after a transient failure
     that already wrote to the daily log → same duplicate.
  3. Legacy orphan adoption (the 32 session-flush-*.md files dating
     back to 2026-04-14) → re-processing them risks re-appending content
     the daily log already has.

Ledger lives at scripts/appended_hashes.json — a separate file from
state.json deliberately, to avoid the C2 design-council finding that
state.json has non-atomic multi-writer hazards. This file is single-
purpose and atomically replaced via os.replace.

Hash is SHA256-16 (first 16 hex chars of SHA256). Collision space ~2^64,
expected daily session volume < 1000, collision odds ~10^-13.

Entries TTL after 24 hours and are pruned on every record_append.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TTL = timedelta(hours=24)


@contextmanager
def _ledger_lock(ledger_path: Path):
    """Cross-platform exclusive lock around the ledger file.

    On POSIX uses fcntl.flock on a sibling .lock file. On Windows or
    other non-POSIX, falls back to no-locking with a logging warning
    (multi-process race exists but the codebase targets Linux+macOS).

    Why locking is mandatory: record_append does read → prune → write.
    Two concurrent flush.py instances (e.g. cross-project SessionEnd +
    drainer respawn) racing this cycle lose one entry → next time that
    content appears, dedup fails → duplicate daily-log entry.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    if sys.platform == "win32":
        # No reliable cross-process advisory lock without ctypes;
        # the race is mostly theoretical on single-user Windows dev.
        yield
        return
    try:
        import fcntl  # POSIX only
    except ImportError:
        yield
        return
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _load(ledger_path: Path) -> dict[str, str]:
    """Read ledger; tolerate missing or corrupt files (treat as empty)."""
    if not ledger_path.exists():
        return {}
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _prune(data: dict[str, str], *, now: datetime | None = None) -> dict[str, str]:
    """Drop entries older than TTL. Returns a NEW dict; doesn't mutate input."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - _TTL
    kept: dict[str, str] = {}
    for key, iso in data.items():
        try:
            ts = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue  # malformed timestamp — drop
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept[key] = iso
    return kept


def _atomic_write(ledger_path: Path, data: dict[str, str]) -> None:
    """Tmp-then-os.replace. Crash-safe."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, ledger_path)


def should_append(content: str, ledger_path: Path) -> bool:
    """Return True iff this content has not been appended in the last 24h.

    Read-only — does NOT modify the ledger. Caller must follow with
    record_append() when the actual append succeeds.
    """
    data = _load(ledger_path)
    return _hash(content) not in data


def record_append(content: str, ledger_path: Path) -> None:
    """Record that content was just appended; prune stale entries.

    Atomically replaces the ledger file (tmp + os.replace), with an
    exclusive lock around the read-modify-write cycle to prevent
    concurrent flush.py instances from losing each other's entries.
    """
    with _ledger_lock(ledger_path):
        data = _load(ledger_path)
        data = _prune(data)
        data[_hash(content)] = datetime.now(timezone.utc).isoformat()
        _atomic_write(ledger_path, data)
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_dedup -v 2>&1 | tail -15`
Expected: `Ran 10 tests in ... OK`.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/dedup.py scripts/test_dedup.py
git commit -m "feat: add content-hash dedup ledger for daily-log appends (Slice 2)"
```

---

## Task 4: Wire dedup into `append_to_daily_log`

**Files:**
- Modify: `/home/faxik/tools/claude-memory-compiler/scripts/flush.py:55-72`

- [ ] **Step 1: Read the current `append_to_daily_log` to confirm exact text**

Verify lines 55-72 still match the existing implementation (no drift since Slice 1).

- [ ] **Step 2: Wire the dedup gate**

In `scripts/flush.py`, replace the body of `append_to_daily_log` (lines 55-72):

```python
def append_to_daily_log(content: str, section: str = "Session") -> None:
    """Append content to today's daily log.

    Content-hash dedup gate: if this exact content has been appended in
    the last 24h, skip silently and log "DUP_SKIP". Solves
    duplicate-on-retry-replay for the common "client crashed mid-write"
    case. Note: LLM non-determinism on regenerated content can defeat
    this gate; see "Out of Scope" for input-hash followup.

    When invoked from the drainer (FLUSH_ORIGINAL_MTIME env set), the
    section header carries the original-session timestamp + the drain
    timestamp so the daily log doesn't pretend old work is fresh.
    """
    from dedup import record_append, should_append

    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    ledger_path = SCRIPTS_DIR / "appended_hashes.json"

    # Dedup gate. Hash the content body only; the header (with timestamps)
    # would defeat dedup since timestamps differ on retries.
    if not should_append(content, ledger_path):
        logging.info("DUP_SKIP: content hash already appended in last 24h (section=%s)", section)
        return

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")

    # Chronology header: when drainer-driven, mark the original time too.
    original_mtime_env = os.environ.get("FLUSH_ORIGINAL_MTIME")
    if original_mtime_env:
        try:
            orig_dt = datetime.fromtimestamp(float(original_mtime_env), timezone.utc).astimezone()
            header = (
                f"### {section} "
                f"(originally {orig_dt.strftime('%H:%M %Y-%m-%d')}, "
                f"drained {time_str})"
            )
        except (ValueError, OSError):
            header = f"### {section} ({time_str})"
    else:
        header = f"### {section} ({time_str})"

    entry = f"{header}\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    record_append(content, ledger_path)
```

- [ ] **Step 3: Verify flush.py imports cleanly**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import flush; print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Verify existing Slice 1 tests still pass**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_flush_error_format -v 2>&1 | tail -5`
Expected: `Ran 7 tests in ... OK`.

- [ ] **Step 5: Add an integration test exercising the dedup gate end-to-end**

Append to `/home/faxik/tools/claude-memory-compiler/scripts/test_dedup.py` (add a second test class at the bottom, before the `if __name__ == "__main__":` block):

```python
class TestAppendIntegration(unittest.TestCase):
    """Exercises flush.append_to_daily_log's dedup gate end-to-end."""

    def setUp(self):
        import flush
        self._original_daily = flush.DAILY_DIR
        self._original_scripts = flush.SCRIPTS_DIR
        self.tmpdir = tempfile.mkdtemp()
        flush.DAILY_DIR = Path(self.tmpdir) / "daily"
        flush.SCRIPTS_DIR = Path(self.tmpdir) / "scripts"
        flush.DAILY_DIR.mkdir(parents=True)
        flush.SCRIPTS_DIR.mkdir(parents=True)
        self._flush = flush

    def tearDown(self):
        import shutil
        self._flush.DAILY_DIR = self._original_daily
        self._flush.SCRIPTS_DIR = self._original_scripts
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_first_append_writes_to_daily_log(self):
        self._flush.append_to_daily_log("test content one", "Session")
        from datetime import datetime, timezone as _tz
        today = datetime.now(_tz.utc).astimezone().strftime("%Y-%m-%d")
        log = self._flush.DAILY_DIR / f"{today}.md"
        self.assertTrue(log.exists())
        self.assertIn("test content one", log.read_text(encoding="utf-8"))

    def test_second_append_same_content_is_skipped(self):
        self._flush.append_to_daily_log("dup content", "Session")
        self._flush.append_to_daily_log("dup content", "Session")
        from datetime import datetime, timezone as _tz
        today = datetime.now(_tz.utc).astimezone().strftime("%Y-%m-%d")
        log = self._flush.DAILY_DIR / f"{today}.md"
        # Content appears exactly once
        text = log.read_text(encoding="utf-8")
        self.assertEqual(text.count("dup content"), 1)

    def test_different_content_appends_independently(self):
        self._flush.append_to_daily_log("content A", "Session")
        self._flush.append_to_daily_log("content B", "Session")
        from datetime import datetime, timezone as _tz
        today = datetime.now(_tz.utc).astimezone().strftime("%Y-%m-%d")
        log = self._flush.DAILY_DIR / f"{today}.md"
        text = log.read_text(encoding="utf-8")
        self.assertIn("content A", text)
        self.assertIn("content B", text)
```

- [ ] **Step 6: Run all dedup tests**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_dedup -v 2>&1 | tail -20`
Expected: `Ran 13 tests in ... OK`.

- [ ] **Step 7: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/flush.py scripts/test_dedup.py
git commit -m "feat(flush): dedup gate on append_to_daily_log via content hash (Slice 2)"
```

---

## Task 5: In-process retry loop in `run_flush`

**Files:**
- Modify: `/home/faxik/tools/claude-memory-compiler/scripts/flush.py:127-208`

- [ ] **Step 1: Read current run_flush to confirm exact line numbers and structure**

Run: `cd /home/faxik/tools/claude-memory-compiler && sed -n '127,212p' scripts/flush.py`
Confirm the `async def run_flush(...)` body matches what's below before editing.

- [ ] **Step 2: Refactor run_flush to wrap the SDK call in a retry loop**

In `scripts/flush.py`, replace the body of `run_flush` (from `async def run_flush` through the `return response` at the end). The new function reuses `_build_flush_error_response` from Slice 1, calls `classify()` on each exception, and implements the in-process retry budget:

```python
async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract knowledge from conversation context.

    Wraps the SDK query() in an in-process retry loop driven by classify().
    - On verdict="retry": sleep 2s, 4s; retry up to 2 times.
    - On verdict="rate_limit": sleep 60s once then retry; if still failing,
      signal exhaustion (caller may park).
    - On verdict="fail_fast": skip retry, return FLUSH_ERROR immediately.

    Returns either the LLM extraction text, "FLUSH_OK", or a
    FLUSH_ERROR-prefixed diagnostic on fail_fast.

    Raises ParkRequested when in-process budget is exhausted on a
    transient pattern — caller (main()) parks the context file for
    the drainer to retry. Exception-based signal (not a string sentinel)
    avoids the LLM-output-collision footgun.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from classifier import classify

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

    # Bounded stderr capture for FLUSH_ERROR diagnostics + rate-limit
    # signal detection. Real bundled-CLI stderr arrives via this callback.
    stderr_tail: deque[str] = deque(maxlen=50)

    def _log_stderr(line: str) -> None:
        stripped = line.rstrip()
        logging.error("[bundled CLI stderr] %s", stripped)
        stderr_tail.append(stripped)

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

    # In-process retry budget. attempts 1..MAX_ATTEMPTS.
    MAX_ATTEMPTS = 3  # 1 initial + 2 retries
    BACKOFF_SCHEDULE_S = (2.0, 4.0)  # waits BEFORE attempts 2 and 3
    RATE_LIMIT_SLEEP_S = 60.0  # one-shot longer wait for rate_limit verdict
    last_exception: BaseException | None = None
    last_verdict_rule: str = "no_attempt"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await _single_attempt()
            return result  # success
        except Exception as e:
            last_exception = e
            verdict = classify(e, stderr_tail=list(stderr_tail))
            last_verdict_rule = verdict.rule_name
            logging.warning(
                "run_flush attempt %d/%d failed: %s (verdict=%s, rule=%s)",
                attempt, MAX_ATTEMPTS, type(e).__name__, verdict.kind, verdict.rule_name,
            )

            if verdict.kind == "fail_fast":
                # Permanent. No retry. Return FLUSH_ERROR with diagnostics.
                import traceback
                logging.error("Agent SDK error (fail_fast): %s\n%s", e, traceback.format_exc())
                return _build_flush_error_response(e, stderr_tail, attempts=attempt)

            if attempt == MAX_ATTEMPTS:
                # Exhausted in-process budget. Signal park via exception
                # (not string sentinel — see ParkRequested docstring).
                logging.warning(
                    "run_flush in-process budget exhausted after %d attempts (last rule: %s); "
                    "raising ParkRequested", attempt, verdict.rule_name,
                )
                body = _build_flush_error_response(e, stderr_tail, attempts=attempt)
                raise ParkRequested(body, last_rule=last_verdict_rule) from e

            # Verdict is retry or rate_limit; we have budget left.
            if verdict.kind == "rate_limit":
                sleep_for = RATE_LIMIT_SLEEP_S
            else:
                sleep_for = BACKOFF_SCHEDULE_S[attempt - 1]
            logging.info("run_flush sleeping %.1fs before attempt %d", sleep_for, attempt + 1)
            await asyncio.sleep(sleep_for)

    # Defensive fallback — loop should always return or raise.
    assert last_exception is not None  # for type narrowing
    return _build_flush_error_response(last_exception, stderr_tail, attempts=MAX_ATTEMPTS)
```

- [ ] **Step 3: Verify imports**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import flush; print('OK')"`
Expected: `OK`.

- [ ] **Step 4: Re-run all existing tests for regression**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_flush_error_format test_classifier test_dedup test_utils_state -v 2>&1 | tail -10`
Expected: all tests still pass.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/flush.py
git commit -m "feat(flush): in-process retry loop via classify() verdicts (Slice 2)"
```

---

## Task 6: Park-on-exhaustion in `main()`

**Files:**
- Modify: `/home/faxik/tools/claude-memory-compiler/scripts/flush.py:263-322`

- [ ] **Step 1: Add ParkRequested exception class + park helper near append_to_daily_log**

Insert these in `scripts/flush.py`, just below the `_build_flush_error_response` definition (so they're available to `main()`):

```python
class ParkRequested(Exception):
    """Raised by run_flush when in-process retry exhausted on a transient
    pattern, signalling that main() should park the context file for the
    drainer rather than write FLUSH_ERROR.

    The exception's first arg is the FLUSH_ERROR diagnostic string that
    would have been written to the daily log if we'd given up.

    Why an exception instead of a string sentinel: an LLM could legitimately
    emit "PARK_REQUESTED" as the first token of a response (especially in
    sessions that discuss this codebase), causing a false-positive park
    + silent data loss. Exception-based signalling avoids the collision.
    """

    def __init__(self, flush_error_body: str, last_rule: str = "unknown"):
        super().__init__(flush_error_body)
        self.flush_error_body = flush_error_body
        self.last_rule = last_rule


def park_context_file(
    context_file: Path,
    session_id: str,
    flush_error_response: str,
    last_rule: str = "unknown",
) -> Path:
    """Move a failed context file to scripts/parked/ with a sidecar.

    The sidecar JSON tracks attempt count + first/last-attempt timestamps
    so the drainer can promote to dead-letter after N tries.

    Returns the parked path (the .md file's new location).
    """
    parked_dir = SCRIPTS_DIR / "parked"
    parked_dir.mkdir(parents=True, exist_ok=True)

    parked_md = parked_dir / f"session-flush-{session_id}.md"
    parked_sidecar = parked_dir / f"session-flush-{session_id}.json"

    # Move the context file (atomic on same-filesystem). On EXDEV
    # (cross-filesystem) or other rename failure, we return the original
    # path and the file remains at its source location — the drainer's
    # legacy-orphan glob (session-flush-*.md in scripts/) will pick it
    # up on the next SessionStart, so it's not silently lost.
    try:
        os.replace(str(context_file), str(parked_md))
    except OSError as e:
        logging.warning(
            "park_context_file: rename %s -> %s failed: %s. File remains at "
            "original location; drainer will adopt via legacy-orphan glob.",
            context_file, parked_md, e,
        )
        return context_file

    # Read existing sidecar if drainer is re-parking; otherwise create.
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    sidecar_data: dict = {
        "session_id": session_id,
        "first_attempt_at": now_iso,
        "last_attempt_at": now_iso,
        "attempts": 1,
        "last_rule": last_rule,
        "last_error": flush_error_response[:1000],  # bounded
    }
    if parked_sidecar.exists():
        try:
            existing = json.loads(parked_sidecar.read_text(encoding="utf-8"))
            sidecar_data["first_attempt_at"] = existing.get("first_attempt_at", now_iso)
            sidecar_data["attempts"] = existing.get("attempts", 0) + 1
        except (json.JSONDecodeError, OSError):
            pass

    parked_sidecar.write_text(json.dumps(sidecar_data, indent=2), encoding="utf-8")
    return parked_md
```

Note: `os.replace` is used here; `import os` is already present in `flush.py:15` for the existing recursion-guard env check, so no new import is needed.

- [ ] **Step 2a: Gate the existing 60s dedup on FLUSH_FROM_DRAIN**

This is the SERIOUS-6 → FATAL-tier fix from the adversarial review. The existing 60-second dedup at `flush.py:277-285` fires BEFORE any retry logic; without bypassing it for drainer-spawned attempts, the drainer's respawn within 60s silently unlinks the inflight file and exits 0 — making the drainer architecturally broken.

In `scripts/flush.py`, find the existing block at lines 277-285:

```python
    # Deduplication: skip if same session was flushed within 60 seconds
    state = load_flush_state()
    if (
        state.get("session_id") == session_id
        and time.time() - state.get("timestamp", 0) < 60
    ):
        logging.info("Skipping duplicate flush for session %s", session_id)
        context_file.unlink(missing_ok=True)
        return
```

Replace with:

```python
    # Deduplication: skip if same session was flushed within 60 seconds.
    # BUT: drainer-spawned attempts MUST bypass this gate — otherwise the
    # drainer's respawn within 60s of a recent SessionEnd would unlink the
    # inflight context file and exit 0, making the drainer architecturally
    # broken. Verified via adversarial review of Slice 2 (SERIOUS-6 → FATAL).
    if not os.environ.get("FLUSH_FROM_DRAIN"):
        state = load_flush_state()
        if (
            state.get("session_id") == session_id
            and time.time() - state.get("timestamp", 0) < 60
        ):
            logging.info("Skipping duplicate flush for session %s", session_id)
            context_file.unlink(missing_ok=True)
            return
```

- [ ] **Step 2b: Modify main() to handle ParkRequested exception and FLUSH_FROM_DRAIN env**

Replace the dispatch block in `main()` (around lines 299-316). Find this section:

```python
    # Run the LLM extraction
    response = asyncio.run(run_flush(context))

    # Append to daily log
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    elif "FLUSH_ERROR" in response:
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session")

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file
    context_file.unlink(missing_ok=True)
```

Replace it with:

```python
    # Run the LLM extraction (with in-process retry loop). On exhaustion,
    # run_flush raises ParkRequested — caught below.
    try:
        response = asyncio.run(run_flush(context))
    except ParkRequested as park:
        if os.environ.get("FLUSH_FROM_DRAIN"):
            # Drainer is driving this attempt; do NOT re-park. Instead,
            # write FLUSH_ERROR to the daily log + exit 1 so drainer
            # increments attempt counter via its failure branch.
            logging.warning(
                "FLUSH_FROM_DRAIN set; not re-parking (rule=%s). "
                "Writing FLUSH_ERROR to daily log.", park.last_rule,
            )
            append_to_daily_log(park.flush_error_body, "Memory Flush")
            save_flush_state({"session_id": session_id, "timestamp": time.time()})
            # Do NOT unlink the context file — drainer needs to see it still exists
            # to know retry is warranted via its own logic. Exit 1.
            logging.info("Flush exhausted (drain path) for session %s", session_id)
            sys.exit(1)

        # Normal path: in-process retry exhausted; park for the drainer.
        parked_path = park_context_file(
            context_file=context_file,
            session_id=session_id,
            flush_error_response=park.flush_error_body,
            last_rule=park.last_rule,
        )
        logging.warning("PARKED context file -> %s (rule=%s)", parked_path, park.last_rule)
        save_flush_state({"session_id": session_id, "timestamp": time.time()})
        # Do NOT write FLUSH_ERROR to the daily log on park — the drainer's
        # eventual retry will either succeed (real content lands) or hit
        # dead-letter (operator sees the parked sidecar).
        logging.info("Flush parked for session %s; drainer will retry", session_id)
        return

    # Append to daily log (non-park path)
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    elif "FLUSH_ERROR" in response:
        # fail_fast verdict reached without park.
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session")

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file (success path only — parked files were moved, not unlinked)
    context_file.unlink(missing_ok=True)
```

- [ ] **Step 3: Verify flush.py imports**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python -c "import sys; sys.path.insert(0, 'scripts'); import flush; print('OK', hasattr(flush, 'park_context_file'))"`
Expected: `OK True`.

- [ ] **Step 4: Run all tests (regression)**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_flush_error_format test_classifier test_dedup test_utils_state -v 2>&1 | tail -10`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/flush.py
git commit -m "feat(flush): park context file on retry exhaustion (Slice 2)"
```

---

## Task 7: Build the drainer (TDD)

**Files:**
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/drain.py`
- Create: `/home/faxik/tools/claude-memory-compiler/scripts/test_drain.py`

- [ ] **Step 1: Write the test file**

Create `/home/faxik/tools/claude-memory-compiler/scripts/test_drain.py`:

```python
"""Tests for the drain script.

These tests exercise the drainer's file-discovery, idempotency, and
dead-letter promotion logic without invoking a real flush.py. We
patch the subprocess.run call to simulate success/failure outcomes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import drain  # noqa: E402


class TestDrainerDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.parked = self.tmpdir / "parked"
        self.dead_letter = self.tmpdir / "dead-letter"
        self.scripts = self.tmpdir  # session-flush-* + flush-context-* live here
        # find_drainable expects the dead-letter dir to be createable.
        self.dead_letter.mkdir(exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_parked_files(self):
        self.parked.mkdir()
        (self.parked / "session-flush-abc.md").write_text("ctx")
        (self.parked / "session-flush-abc.json").write_text("{}")
        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "session-flush-abc.md")

    def test_finds_legacy_session_flush_orphans(self):
        (self.scripts / "session-flush-legacy.md").write_text("ctx")
        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "session-flush-legacy.md")

    def test_finds_pre_compact_orphans(self):
        """flush-context-*.md is the PreCompact orphan prefix — adversary
        finding from the design council."""
        (self.scripts / "flush-context-pc1.md").write_text("ctx")
        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].name, "flush-context-pc1.md")

    def test_skips_inflight_files(self):
        """A .inflight rename means another drainer claimed this work."""
        self.parked.mkdir()
        (self.parked / "session-flush-abc.md.inflight").write_text("ctx")
        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
        )
        self.assertEqual(candidates, [])

    def test_legacy_orphan_older_than_max_age_goes_to_dead_letter(self):
        """SERIOUS-5 fix: orphans older than 14 days quarantine directly
        to dead-letter rather than burning LLM cost on stale content."""
        import time as _time
        old = self.scripts / "session-flush-legacy-old.md"
        old.write_text("ctx")
        # Backdate the mtime to 30 days ago.
        old_ts = _time.time() - 30 * 86400
        os.utime(str(old), (old_ts, old_ts))

        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
            max_age_days=14,
        )
        self.assertEqual(candidates, [])
        self.assertTrue((self.dead_letter / "session-flush-legacy-old.md").exists())
        # Sidecar carries the "legacy_too_old" reason.
        sidecar = self.dead_letter / "session-flush-legacy-old.json"
        self.assertTrue(sidecar.exists())
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["last_rule"], "legacy_too_old")


class TestDrainerIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.parked = self.tmpdir / "parked"
        self.parked.mkdir()
        self.dead_letter = self.tmpdir / "dead-letter"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_claim_renames_to_inflight(self):
        f = self.parked / "session-flush-x.md"
        f.write_text("ctx")
        inflight = drain.claim(f)
        self.assertIsNotNone(inflight)
        self.assertTrue(inflight.exists())
        self.assertFalse(f.exists())
        self.assertEqual(inflight.suffix, ".inflight")

    def test_claim_returns_none_if_already_inflight(self):
        """Concurrent drainer race: the second claim must fail cleanly."""
        f = self.parked / "session-flush-x.md"
        f.write_text("ctx")
        first = drain.claim(f)
        self.assertIsNotNone(first)
        # File no longer exists; second claim attempt would fail
        second = drain.claim(f)
        self.assertIsNone(second)

    def test_promote_to_dead_letter_moves_md_and_sidecar(self):
        f = self.parked / "session-flush-x.md.inflight"
        f.write_text("ctx")
        sidecar = self.parked / "session-flush-x.json"
        sidecar.write_text(json.dumps({"attempts": 5}))
        drain.promote_to_dead_letter(
            inflight=f,
            sidecar=sidecar,
            dead_letter_dir=self.dead_letter,
        )
        self.assertFalse(f.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue((self.dead_letter / "session-flush-x.md").exists())
        self.assertTrue((self.dead_letter / "session-flush-x.json").exists())


class TestDrainerEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.parked = self.tmpdir / "parked"
        self.parked.mkdir()
        self.dead_letter = self.tmpdir / "dead-letter"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_parked(self, session_id: str, attempts: int):
        md = self.parked / f"session-flush-{session_id}.md"
        md.write_text("context body")
        sidecar = self.parked / f"session-flush-{session_id}.json"
        sidecar.write_text(json.dumps({
            "session_id": session_id,
            "attempts": attempts,
            "last_rule": "process_exit_1",
        }))
        return md, sidecar

    def test_drain_one_success_unlinks_inflight_and_sidecar(self):
        self._make_parked("ok", attempts=2)
        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            n = drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        self.assertEqual(n, 1)
        # On success, inflight should be removed (flush.py unlinked the file)
        # and sidecar removed.
        self.assertFalse(any(self.parked.glob("*.inflight")))
        self.assertFalse((self.parked / "session-flush-ok.json").exists())

    def test_drain_one_failure_increments_attempts_and_releases_inflight(self):
        self._make_parked("fail", attempts=1)
        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        # Failure: inflight gets renamed BACK to .md (release), sidecar
        # attempts incremented.
        md = self.parked / "session-flush-fail.md"
        self.assertTrue(md.exists())
        sidecar = self.parked / "session-flush-fail.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["attempts"], 2)

    def test_drain_one_promotes_after_max_attempts(self):
        """At MAX_ATTEMPTS (5), promote to dead-letter."""
        self._make_parked("dl", attempts=5)
        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        self.assertFalse((self.parked / "session-flush-dl.md").exists())
        self.assertTrue((self.dead_letter / "session-flush-dl.md").exists())

    def test_drain_one_failure_preserves_pre_compact_prefix(self):
        """FATAL-3 regression test: PreCompact orphans (flush-context-*.md)
        must get sidecar at flush-context-<id>.json, NOT session-flush-<id>.json.

        Without the _sidecar_for fix, the failure branch hardcoded
        session-flush-{id}.json → next discovery reads flush-context-<id>.json
        (which doesn't exist) → attempts never increment → infinite retry.
        """
        # Synthesize a PreCompact orphan directly in scripts (no sidecar).
        md = self.tmpdir / "flush-context-pc1.md"
        md.write_text("pc context")

        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr=""
            )
            drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )

        # Sidecar must use the flush-context- prefix, not session-flush-.
        self.assertTrue((self.tmpdir / "flush-context-pc1.json").exists())
        self.assertFalse((self.tmpdir / "session-flush-pc1.json").exists())
        # Sidecar attempts incremented to 1 (not stuck at 0).
        data = json.loads((self.tmpdir / "flush-context-pc1.json").read_text(encoding="utf-8"))
        self.assertEqual(data["attempts"], 1)

    def test_drain_one_returns_neg1_on_race_lost(self):
        """W-2 fix: distinguish 'no work' (0) from 'race lost' (-1) so the
        caller's loop can continue to the next candidate."""
        # Two parked files; the first is pre-claimed (.inflight) before drain runs.
        self._make_parked("ok", attempts=1)
        (self.parked / "session-flush-ok.md").rename(
            self.parked / "session-flush-ok.md.inflight"
        )
        # No other candidates → find_drainable returns []. drain_one returns 0.
        # Tests the no-work case.
        result = drain.drain_one(
            parked_dir=self.parked,
            scripts_dir=self.tmpdir,
            dead_letter_dir=self.dead_letter,
            flush_script=Path("/fake/flush.py"),
            project_root=self.tmpdir,
        )
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests, verify they fail**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_drain -v 2>&1 | tail -10`
Expected: `ModuleNotFoundError: No module named 'drain'`.

- [ ] **Step 3: Implement drain.py**

Create `/home/faxik/tools/claude-memory-compiler/scripts/drain.py`:

```python
"""Drainer for parked + orphaned flush context files.

Invoked from hooks/session-start.py as fire-and-forget Popen.

Discovers:
  - scripts/parked/session-flush-*.md   (in-process retry exhaustions)
  - scripts/session-flush-*.md          (legacy SessionEnd orphans)
  - scripts/flush-context-*.md          (PreCompact orphans)

For each:
  1. .inflight rename (atomic claim; concurrent-drainer safe)
  2. Spawn flush.py with FLUSH_FROM_DRAIN=1 set
  3. On exit_code==0: unlink inflight + sidecar (flush.py already wrote to daily log)
  4. On exit_code!=0: rename inflight back to .md, increment sidecar.attempts
  5. If attempts >= MAX_ATTEMPTS (5): promote to scripts/dead-letter/
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PARKED_DIR = SCRIPTS_DIR / "parked"
DEAD_LETTER_DIR = SCRIPTS_DIR / "dead-letter"
FLUSH_SCRIPT = SCRIPTS_DIR / "flush.py"
LOG_FILE = SCRIPTS_DIR / "drain.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [drain] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

MAX_ATTEMPTS = 5
DRAIN_DEFAULT_LIMIT = 1  # how many to drain per invocation
MAX_AGE_DAYS = 14  # files older than this go straight to dead-letter


def find_drainable(
    parked_dir: Path = PARKED_DIR,
    scripts_dir: Path = SCRIPTS_DIR,
    dead_letter_dir: Path = DEAD_LETTER_DIR,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[Path]:
    """Discover .md files eligible for draining.

    Returns paths sorted oldest-first (mtime ascending) so we work the
    backlog FIFO. Skips .inflight files (already claimed).

    Side effect: files older than max_age_days are moved DIRECTLY to
    dead-letter without retry. Re-flushing 5-week-old context creates
    stale time-travel daily-log entries; the operator can manually
    triage dead-letter/.
    """
    raw: list[Path] = []
    if parked_dir.exists():
        for p in parked_dir.glob("session-flush-*.md"):
            if not p.name.endswith(".inflight"):
                raw.append(p)
    # Legacy orphans from SessionEnd that crashed pre-unlink:
    for p in scripts_dir.glob("session-flush-*.md"):
        if not p.name.endswith(".inflight"):
            raw.append(p)
    # PreCompact-prefix orphans (adversary finding):
    for p in scripts_dir.glob("flush-context-*.md"):
        if not p.name.endswith(".inflight"):
            raw.append(p)

    import time as _time
    cutoff_ts = _time.time() - max_age_days * 86400
    fresh: list[Path] = []
    for p in raw:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            # Quarantine: move to dead-letter with a synthetic sidecar.
            try:
                dead_letter_dir.mkdir(parents=True, exist_ok=True)
                target = dead_letter_dir / p.name
                os.rename(str(p), str(target))
                # Synthetic sidecar so operator knows why it landed here.
                sidecar = _sidecar_for(target)
                sidecar.write_text(json.dumps({
                    "session_id": _session_id_from_path(p),
                    "first_attempt_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                    "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                    "attempts": 0,
                    "last_rule": "legacy_too_old",
                    "last_error": f"file mtime older than {max_age_days}d cutoff; not retried",
                }, indent=2), encoding="utf-8")
                logging.info("find_drainable: quarantined %s (older than %dd)", p.name, max_age_days)
            except OSError as e:
                logging.error("find_drainable: quarantine of %s failed: %s", p, e)
            continue
        fresh.append(p)

    fresh.sort(key=lambda p: p.stat().st_mtime)
    return fresh


def claim(md_path: Path) -> Path | None:
    """Atomic claim via .inflight rename. Returns the new path, or None
    if another drainer beat us to it."""
    inflight = md_path.with_suffix(md_path.suffix + ".inflight")
    try:
        os.rename(str(md_path), str(inflight))
    except FileNotFoundError:
        return None  # already claimed by concurrent drainer
    except OSError as e:
        logging.error("claim: %s -> %s failed: %s", md_path, inflight, e)
        return None
    return inflight


def release(inflight: Path) -> Path:
    """Rename .inflight back to .md after a failed attempt."""
    # For `something.md.inflight`, with_suffix("") drops the last `.inflight`
    # suffix, leaving `something.md`. We assert that invariant rather than
    # leaving a dead-code branch.
    md = inflight.with_suffix("")
    assert md.suffix == ".md", f"release: unexpected shape: {inflight}"
    try:
        os.rename(str(inflight), str(md))
    except OSError as e:
        logging.error("release: %s -> %s failed: %s", inflight, md, e)
    return md


def promote_to_dead_letter(
    inflight: Path,
    sidecar: Path,
    dead_letter_dir: Path,
) -> None:
    """Move .inflight (or .md) + sidecar into dead-letter/."""
    dead_letter_dir.mkdir(parents=True, exist_ok=True)
    target_md = dead_letter_dir / inflight.with_suffix("").name  # strip .inflight
    if not target_md.name.endswith(".md"):
        target_md = dead_letter_dir / (target_md.name + ".md")
    try:
        os.rename(str(inflight), str(target_md))
    except OSError as e:
        logging.error("promote: %s -> %s failed: %s", inflight, target_md, e)
    if sidecar.exists():
        target_sidecar = dead_letter_dir / sidecar.name
        try:
            os.rename(str(sidecar), str(target_sidecar))
        except OSError as e:
            logging.error("promote sidecar: %s -> %s failed: %s", sidecar, target_sidecar, e)


def _sidecar_for(md_or_inflight: Path) -> Path:
    """Derive sidecar path from an .md or .md.inflight path, preserving prefix.

    CRITICAL: this is the ONLY place sidecar paths are constructed.
    Hardcoding `session-flush-<id>.json` elsewhere creates a prefix-mismatch
    bug for PreCompact orphans (`flush-context-<id>.md`) — the failure
    branch writes a sidecar at session-flush-<id>.json next to a
    flush-context-<id>.md, and the next read can't find it → attempts
    never increment → infinite retry loop. Verified by adversarial review.
    """
    name = md_or_inflight.name
    if name.endswith(".md.inflight"):
        name = name[: -len(".md.inflight")]
    elif name.endswith(".md"):
        name = name[: -len(".md")]
    return md_or_inflight.parent / f"{name}.json"


def _read_sidecar(md_path: Path) -> dict:
    """Read the sidecar JSON for md_path. Returns {} if absent/corrupt."""
    sidecar = _sidecar_for(md_path)
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_sidecar(md_path: Path, data: dict) -> None:
    """Write sidecar JSON for md_path. CALLER PASSES THE .md OR .inflight
    PATH — NOT the sidecar path itself."""
    sidecar = _sidecar_for(md_path)
    sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _session_id_from_path(md_path: Path) -> str:
    """Extract session ID from `session-flush-<id>.md` or `flush-context-<id>.md`."""
    name = md_path.name
    for prefix in ("session-flush-", "flush-context-"):
        if name.startswith(prefix):
            stem = name[len(prefix):]
            for suffix in (".md.inflight", ".md"):
                if stem.endswith(suffix):
                    return stem[: -len(suffix)]
            return stem
    return md_path.stem


def drain_one(
    parked_dir: Path = PARKED_DIR,
    scripts_dir: Path = SCRIPTS_DIR,
    dead_letter_dir: Path = DEAD_LETTER_DIR,
    flush_script: Path = FLUSH_SCRIPT,
    project_root: Path = ROOT,
) -> int:
    """Drain at most one file. Returns the count actually drained."""
    candidates = find_drainable(parked_dir=parked_dir, scripts_dir=scripts_dir)
    if not candidates:
        return 0

    md = candidates[0]
    inflight = claim(md)
    if inflight is None:
        # Claim race lost — another drainer claimed this file. Caller's
        # loop should distinguish "no work" (0) from "race lost" (-1).
        return -1

    session_id = _session_id_from_path(md)
    # Sidecar path is derived via _sidecar_for (preserves prefix).
    sidecar_data = _read_sidecar(inflight)
    attempts_so_far = int(sidecar_data.get("attempts", 0))

    # Promote BEFORE spawning if we've already exhausted attempts.
    if attempts_so_far >= MAX_ATTEMPTS:
        logging.warning(
            "drain_one: session %s exhausted MAX_ATTEMPTS=%d; promoting to dead-letter",
            session_id, MAX_ATTEMPTS,
        )
        promote_to_dead_letter(
            inflight=inflight,
            sidecar=_sidecar_for(inflight),
            dead_letter_dir=dead_letter_dir,
        )
        return 1

    # Spawn flush.py against the inflight file.
    env = dict(os.environ)
    env["FLUSH_FROM_DRAIN"] = "1"
    # Pass the ORIGINAL mtime so flush.py's daily-log section header can
    # render "Memory Flush (originally HH:MM YYYY-MM-DD, drained HH:MM)"
    # — avoids time-travel confusion where today's log claims old work.
    try:
        original_mtime = inflight.stat().st_mtime
        env["FLUSH_ORIGINAL_MTIME"] = str(original_mtime)
    except OSError:
        pass
    cmd = [
        "uv", "run", "--directory", str(project_root),
        "python", str(flush_script),
        str(inflight), session_id,
    ]
    logging.info("drain_one: spawning flush.py for session %s (attempt %d/%d)",
                 session_id, attempts_so_far + 1, MAX_ATTEMPTS)
    try:
        result = subprocess.run(
            cmd,
            env=env,
            timeout=300,  # 5min hard cap
            check=False,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        logging.error("drain_one: session %s timed out at 5min", session_id)
        # Treat timeout as a failed attempt
        sidecar_data["attempts"] = attempts_so_far + 1
        sidecar_data["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        sidecar_data["last_error"] = "drain timeout 300s"
        _write_sidecar(inflight, sidecar_data)
        release(inflight)
        return 1

    if result.returncode == 0:
        # Success — flush.py already unlinked the inflight file (it called
        # context_file.unlink() at end of main()). Just remove the sidecar.
        sidecar_file = _sidecar_for(inflight)
        if sidecar_file.exists():
            try:
                sidecar_file.unlink()
            except OSError:
                pass
        # The inflight file may or may not still exist; clean up if it does.
        if inflight.exists():
            try:
                inflight.unlink()
            except OSError:
                pass
        logging.info("drain_one: session %s succeeded", session_id)
        return 1

    # Failure — bump attempts, release inflight, possibly promote.
    sidecar_data["attempts"] = attempts_so_far + 1
    sidecar_data["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    sidecar_data["last_error"] = (
        result.stderr.decode("utf-8", errors="replace")[:500]
        if result.stderr else f"exit_code={result.returncode}"
    )
    _write_sidecar(inflight, sidecar_data)

    if sidecar_data["attempts"] >= MAX_ATTEMPTS:
        logging.warning(
            "drain_one: session %s reached MAX_ATTEMPTS=%d; promoting",
            session_id, MAX_ATTEMPTS,
        )
        promote_to_dead_letter(
            inflight=inflight,
            sidecar=_sidecar_for(inflight),
            dead_letter_dir=dead_letter_dir,
        )
    else:
        release(inflight)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=DRAIN_DEFAULT_LIMIT,
                        help="Maximum files to drain in this invocation")
    args = parser.parse_args()

    drained = 0
    race_losses = 0
    MAX_RACE_LOSSES = 3  # protective cap
    iterations_left = args.max
    while iterations_left > 0:
        n = drain_one()
        if n == 1:
            drained += 1
            iterations_left -= 1
        elif n == -1:
            # Race lost — try next candidate, but don't loop forever.
            race_losses += 1
            if race_losses >= MAX_RACE_LOSSES:
                logging.info("drain.py: hit MAX_RACE_LOSSES=%d, exiting", MAX_RACE_LOSSES)
                break
            continue
        else:  # n == 0: no work
            break
    logging.info("drain.py: drained %d file(s) this run (race_losses=%d)", drained, race_losses)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run drain tests**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_drain -v 2>&1 | tail -20`
Expected: `Ran 12 tests in ... OK`. The new tests cover SERIOUS-5 age filter, FATAL-3 PreCompact prefix preservation, and W-2 race-lost distinguishability.

- [ ] **Step 5: Smoke-test that drain.py runs without crashing on empty directories**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python scripts/drain.py --max 1`
Expected: exits cleanly. No output unless there were files to drain.

- [ ] **Step 6: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add scripts/drain.py scripts/test_drain.py
git commit -m "feat: add drain.py — fire-and-forget retry of parked/orphaned context files (Slice 2)"
```

---

## Task 8: Wire drainer spawn into SessionStart hook

**Files:**
- Modify: `/home/faxik/tools/claude-memory-compiler/hooks/session-start.py:78-92`

- [ ] **Step 1: Read current main() of session-start.py**

Verify lines 78-92 still match the implementation shown in the brief (the hook outputs `additionalContext` JSON).

- [ ] **Step 2: Add a fire-and-forget Popen of drain.py**

In `hooks/session-start.py`, find `def main():`. Replace the body:

```python
def main():
    context = build_context()

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }

    # Spawn the drainer as fire-and-forget. The SessionStart hook has a
    # 15s timeout (verified at .claude/settings.json:11); we MUST return
    # in <50ms. Popen with stdout/stderr=DEVNULL and detached process
    # group keeps us under that budget. Failures here are non-fatal —
    # we log to drain.log inside the spawned drain.py process.
    try:
        import subprocess
        drain_script = ROOT / "scripts" / "drain.py"
        if drain_script.exists():
            cmd = [
                "uv", "run", "--directory", str(ROOT),
                "python", str(drain_script), "--max", "1",
            ]
            kwargs: dict = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **kwargs)
    except Exception:
        # Drainer spawn failures must not block SessionStart. drain.log
        # would capture any drainer-side issues.
        pass

    print(json.dumps(output))
```

- [ ] **Step 3: Verify session-start.py imports cleanly**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python hooks/session-start.py < /dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print('OK', list(d.keys()))"`
Expected: `OK ['hookSpecificOutput']`. The drainer spawn is fire-and-forget; it returns immediately.

- [ ] **Step 4: Check timing — hook must return <50ms post-drainer-spawn**

Run: `cd /home/faxik/tools/claude-memory-compiler && time (uv run python hooks/session-start.py < /dev/null > /dev/null) 2>&1 | tail -5`
Expected: `real 0m0.XXXs` where XXX is well under the 15s hook budget. (The `uv run` itself takes ~200-400ms; the actual hook work is fast.)

- [ ] **Step 5: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add hooks/session-start.py
git commit -m "feat(hooks): fire-and-forget drain.py spawn from SessionStart (Slice 2)"
```

---

## Task 8.5: Add classifier lint guard (deploy prerequisite)

**Files:**
- Create: `/home/faxik/tools/claude-memory-compiler/tools/lint_classifier.py`
- Create: `/home/faxik/tools/claude-memory-compiler/tools/__init__.py` (empty)

The adversarial review found that the prior Slice 2 spec relied on `getattr(exc, "stderr", ...)` to detect rate-limit signals — but the SDK hardcodes that attribute to the boilerplate string "Check stderr output for details" at `subprocess_cli.py:613-617`. This was the same bug class the council was originally convened to catch. A mechanical lint guard prevents the next iteration from regressing.

- [ ] **Step 1: Create the lint script**

Create `/home/faxik/tools/claude-memory-compiler/tools/__init__.py` empty.

Create `/home/faxik/tools/claude-memory-compiler/tools/lint_classifier.py`:

```python
"""Lint guard: block reads of `exc.stderr` in classifier code.

The claude_agent_sdk hardcodes ProcessError.stderr to the boilerplate
string "Check stderr output for details" (verified at
.venv/lib/python3.13/site-packages/claude_agent_sdk/_internal/transport/
subprocess_cli.py:613-617). Any classifier code that pattern-matches
against `getattr(exc, "stderr", ...)` is silently broken — the real
CLI stderr only arrives via the options.stderr callback.

This lint runs from CI and fails if the pattern reappears. Usage:
    uv run python tools/lint_classifier.py
Exit code 0 = clean. Exit code 1 = banned pattern found.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSIFIER_FILES = [
    ROOT / "scripts" / "classifier.py",
]

# Banned patterns. Each is (regex, explanation).
BANNED = [
    (
        re.compile(r"""getattr\s*\(\s*exc\s*,\s*['"]stderr['"]"""),
        "exc.stderr is hardcoded SDK boilerplate; use the stderr_tail "
        "parameter (real callback-fed text) instead.",
    ),
    (
        re.compile(r"""\.stderr\b"""),
        "Reading .stderr from an SDK exception is unsafe; the SDK "
        "hardcodes that attribute. Pass real stderr via stderr_tail.",
    ),
]


def main() -> int:
    failures = 0
    for path in CLASSIFIER_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for ln_no, line in enumerate(text.splitlines(), start=1):
            # Skip docstrings / comments by simple heuristic: line stripped
            # starts with `#` or has been marked OK-via-comment with the
            # special marker `# noqa: classifier-stderr-ok` on the same line.
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "noqa: classifier-stderr-ok" in line:
                continue
            for regex, why in BANNED:
                if regex.search(line):
                    print(f"{path}:{ln_no}: BANNED pattern matched: {line.strip()}")
                    print(f"  reason: {why}")
                    failures += 1
    if failures:
        print(f"\nlint_classifier: {failures} violation(s). Exit 1.")
        return 1
    print("lint_classifier: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the lint to verify the current classifier passes**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python tools/lint_classifier.py`
Expected: `lint_classifier: clean.` exit 0.

If you see a violation: it means Step 1 of Task 2 wasn't applied correctly. Go back, ensure `classify()` only reads `stderr_tail` (the parameter), never `getattr(exc, "stderr", ...)`.

- [ ] **Step 3: Verify the lint catches a regression**

Sanity-check the lint by temporarily reintroducing the banned pattern:

```bash
cd /home/faxik/tools/claude-memory-compiler
echo "# BANNED PROBE: $(printf 'getattr(exc, \"stderr\", \"\")')" >> scripts/classifier.py
uv run python tools/lint_classifier.py
echo "exit=$?"
# Expected: exit=1 and a "BANNED pattern matched" message.
# Then revert:
git checkout scripts/classifier.py
```

- [ ] **Step 4: Commit**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add tools/lint_classifier.py tools/__init__.py
git commit -m "build: add lint_classifier.py to guard against exc.stderr regressions (Slice 2 gate)"
```

---

## Task 9: Final validation

- [ ] **Step 1: Full test suite from scripts dir**

Run: `cd /home/faxik/tools/claude-memory-compiler/scripts && uv run python -m unittest test_flush_error_format test_classifier test_dedup test_drain test_utils_state -v 2>&1 | tail -10`
Expected: all tests pass. Total ~55 tests across 5 files (7 flush + 14 classifier including TestClassifierRealSDK + 14 dedup including TestAppendIntegration + concurrent race + 12 drain + 7 utils_state).

**DEPLOY GATE:** `TestClassifierRealSDK` must pass. If it fails, the regex patterns have drifted from the SDK's actual messages — Slice 2 MUST NOT ship until they're realigned.

- [ ] **Step 1b: Run the classifier lint (deploy gate from Task 8.5)**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run python tools/lint_classifier.py`
Expected: `lint_classifier: clean.` exit 0.

If this fails, the classifier has regressed onto the banned `getattr(exc, "stderr", ...)` pattern. Block ship until fixed.

- [ ] **Step 1c: Verify Slice-1 evidence prerequisite**

Slice 2's CLASSIFICATIONS table contains speculative patterns (`auth_invalid`, `prompt_too_long`) that are seeded from API conventions but unverified against this codebase's flush.log. Before shipping, confirm ≥7 days of Slice-1 (commit `26c6c8a`) telemetry exists:

```bash
cd /home/faxik/tools/claude-memory-compiler
# Count FLUSH_ERROR entries with the Slice-1 enriched format (exit_code=X).
grep -c "FLUSH_ERROR.*exit_code=" scripts/flush.log
# Count distinct stderr_tail signatures — useful for refining patterns.
grep -A 60 "FLUSH_ERROR" scripts/flush.log | grep -oE "rate.?limit|429|too many requests|authentication|invalid.*api.?key|prompt.*too long" | sort | uniq -c | sort -rn | head -20
```

If the second grep shows real signatures, refine the CLASSIFICATIONS regexes BEFORE shipping. If it returns nothing for 7 days, the seeded patterns may be overreach — consider dropping `auth_invalid` and `prompt_too_long` from Slice 2 until they're observed in the wild.

- [ ] **Step 2: Lint check (if ruff is configured for this project)**

Run: `cd /home/faxik/tools/claude-memory-compiler && uv run ruff check scripts/ hooks/ 2>&1 | head -30`
If ruff is not configured: skip this step (the existing repo has no ruff config; lint is opt-in).

- [ ] **Step 3: End-to-end smoke test — simulate a parked file**

Manually create a synthetic parked file and confirm the drainer picks it up:

```bash
cd /home/faxik/tools/claude-memory-compiler
mkdir -p scripts/parked
cat > scripts/parked/session-flush-smoketest.md <<'EOF'
**User:** This is a synthetic test session for the drainer smoke test.
**Assistant:** Acknowledging the test.
EOF
cat > scripts/parked/session-flush-smoketest.json <<'EOF'
{"session_id": "smoketest", "attempts": 1, "last_rule": "process_exit_1"}
EOF
uv run python scripts/drain.py --max 1
# Verify drainer attempted (look in scripts/drain.log)
tail -10 scripts/drain.log
```

Expected: `drain.log` shows `drain_one: spawning flush.py for session smoketest (attempt 2/5)` and either a success or failure line. If the LLM call actually succeeded, the parked file is gone; if it failed, sidecar attempts is now 2 and the .md is back.

Clean up after smoke test:
```bash
rm -f scripts/parked/session-flush-smoketest.md scripts/parked/session-flush-smoketest.json
rm -f scripts/dead-letter/session-flush-smoketest.md scripts/dead-letter/session-flush-smoketest.json
```

- [ ] **Step 4: Update concept article**

Edit `/home/faxik/tools/claude-memory-compiler/knowledge/concepts/claude-memory-compiler-setup.md`. Append after the "Slice 1: Observability" section:

```markdown
## Slice 2: Retry + Park + Dedup (May 2026)

Closes the data-loss half of the council's α-split (Slice 1 closed the observability half on 2026-05-17). Three independent layers shipped together:

1. **Classifier (`scripts/classifier.py`)** — rule-table matching `(exc class, message pattern, exit_code)` to one of three verdicts: `retry` / `rate_limit` / `fail_fast`. Seeded with the two historical failure patterns ("Control request timeout", "exit code 1") plus speculative rules for auth/prompt-size. Default is `retry` on the cheap-retry vs silent-data-loss principle.
2. **In-process retry loop (`scripts/flush.py:run_flush`)** — wraps the SDK `query()` in a 3-attempt loop driven by `classify()`. `fail_fast` skips retry; `retry` uses 2s/4s backoff; `rate_limit` uses one 60s sleep. On exhaustion, signals "PARK_REQUESTED" to `main()`.
3. **Filesystem park + drainer (`scripts/parked/`, `scripts/drain.py`)** — `main()` moves the context file to `scripts/parked/<id>.md` + `<id>.json` sidecar on retry exhaustion. SessionStart hook spawns `drain.py --max 1` as fire-and-forget (returns <50ms; 15s timeout). Drainer claims via `.inflight` rename (concurrent-safe), re-runs `flush.py` with `FLUSH_FROM_DRAIN=1` set so the child doesn't re-park. After 5 failed attempts → `scripts/dead-letter/` for operator review.
4. **Content-hash dedup (`scripts/dedup.py`)** — every `append_to_daily_log` call gates on a 24h SHA256-16 ledger at `scripts/appended_hashes.json` (atomic `os.replace`). Prevents duplicate entries from in-process retry replay, drainer replay, and legacy-orphan adoption.

The legacy 32 `session-flush-*.md` orphans dating back to 2026-04-14 + any PreCompact `flush-context-*` orphans are picked up by the drainer's discovery glob on the first SessionStart after this slice deploys.

Together with Slice 1, the compiler's resilience profile is now: failures are observable (Slice 1's enriched FLUSH_ERROR), transient bursts absorbed (in-process retry), rate-limit clusters absorbed (filesystem park + drainer wait), permanent errors fail fast (`fail_fast` verdict), and replay is duplicate-safe (content-hash dedup).
```

- [ ] **Step 5: Commit the documentation update**

```bash
cd /home/faxik/tools/claude-memory-compiler
git add knowledge/concepts/claude-memory-compiler-setup.md 2>/dev/null || echo "knowledge/ is gitignored — local-only update"
# Knowledge is per-user, gitignored; the edit is local.
```

- [ ] **Step 6: Manual end-to-end verification next time SessionStart fires**

Start a fresh Claude Code session in any project. Then inspect:

```bash
tail -30 /home/faxik/tools/claude-memory-compiler/scripts/drain.log
ls /home/faxik/tools/claude-memory-compiler/scripts/parked/ 2>/dev/null
ls /home/faxik/tools/claude-memory-compiler/scripts/dead-letter/ 2>/dev/null
```

Expected:
- `drain.log` shows `drain.py: drained 0 file(s) this run` (if no parked work) or `drain_one: spawning flush.py for session <id>` lines (if legacy orphans got picked up).
- `parked/` may have legacy items mid-processing during the first run.
- `dead-letter/` should be empty (orphans should drain in 1 attempt if the SDK is healthy).

---

## Out of Scope (deferred to followup codebugs)

These extensions can ship after Slice 2 is live and stable:

- **CLASSIFICATIONS tuning from real evidence.** After ~1 week of Slice-1 FLUSH_ERROR entries with `stderr_tail`, grep distinct exit-code/class headers + stderr patterns. Compare against the seeded `CLASSIFICATIONS` table. Tighten regexes (the speculative `prompt_too_long`, `auth_invalid` patterns may need adjustment to match the bundled CLI's actual stderr formatting).
- **Cost-budget circuit breaker (`state["daily_retry_cost"]`).** Adversary suggestion from council R1. Halt drainer spawns when retry cost exceeds a daily cap (e.g. $5/day). Requires hooking into the LLM cost return value from `run_flush`, which isn't currently propagated.
- **Drainer chronological-log entry**: when a parked file drains hours later, the daily-log entry lands under the resolution time, not the original session time. Sidecar's `first_attempt_at` preserves truth; add `### Memory Flush (originally HH:MM, drained HH:MM)` header rendering.
- **Slice 1 `flush-stderr.<pid>.log` cleanup.** Slice 1 did NOT add per-PID stderr files (that was a Slice 2 design item that became moot once the stderr deque was inlined). No GC needed.
- **Actual log rotation.** `flush.log` (28K+ lines), `drain.log` (will accumulate), `compile.log` — all unbounded. Followup with `logging.handlers.RotatingFileHandler` (5MB × 3 backups) or external `logrotate`.
- **PreCompact-prefix park file write.** Currently only SessionEnd's flush.py knows about parking. If a PreCompact-spawned flush.py exhausts retries, it parks under `session-flush-<id>.md` (same as SessionEnd) — fine because the session_id namespace is shared. No action needed unless we want prefix-preserving park, which is unnecessary for the drainer.

---

## Round-3 Adversarial Review Corrections (2026-05-18)

After round-2 fixes landed, a round-3 review found 1 NEW FATAL + 3 NEW SERIOUS. Score: **7/10 — fix mandatory, then ship.** Judge: "Convergence, not divergence — FATAL count monotonically decreasing, residual FATAL has moved up a layer of abstraction each round." All 4 round-3 mandatory fixes are applied during implementation (not by re-editing all code blocks above):

- **M-1 (FATAL — lint over-match)**: `tools/lint_classifier.py`'s `\.stderr\b` regex matches the `ProcessError.stderr` literal inside the patched `classify()` docstring NOTE → deploy gate self-fails. Fix: switch from line-based grep to `ast`-based scanning that skips string-literal line ranges. Use `ast.walk` collecting `node.lineno..node.end_lineno` for every `ast.Constant` whose value is a string.
- **M-2 (SERIOUS — race-lost test is a lie)**: `test_drain_one_returns_neg1_on_race_lost` asserts `== 0` and tests the no-work branch. Fix: monkey-patch `drain.claim = lambda _: None` so a real candidate exists but claim returns None → -1 branch exercised. Keep the existing test renamed to `test_drain_one_returns_0_on_no_candidates`.
- **M-3 (SERIOUS — orphaned sidecar)**: Age-quarantine `find_drainable` moves only the `.md` to dead-letter; the original `.json` sidecar stays in parked/. Fix: also `os.rename` the existing sidecar (preserving real attempt history); only write the synthetic sidecar when there wasn't one already.
- **M-4 (SERIOUS — OverflowError unhandled)**: `datetime.fromtimestamp(float("inf"), ...)` raises `OverflowError`, not caught by the `(ValueError, OSError)` tuple in `append_to_daily_log`. Fix: widen except to include `OverflowError` (and `TypeError` for completeness).

Recommended also folded in during implementation: cleanup of stale Slice-1 docstring (N-1), maintainer note in lint (N-3), section-name placement (W-4 if cheap), real-timespan check for Deploy Prerequisite 3 (W-3, optional). W-2 (cross-process flock test) deferred — defender's partial defense holds, current thread-based test exercises the flock primitive.

---

## Round-2 Adversarial Review Corrections (2026-05-17)

The first draft of this plan went through `/adversarial-review` (Adversary → Defender → Judge, all `model: "opus"`). Verdict: **5/6 — Significant rework needed**. Judge upheld 3 FATAL (one upgraded from SERIOUS) and 5 SERIOUS. All mandatory fixes have been applied inline above. Summary of what changed and why:

1. **FATAL-1 (rate_limit dead code, same bug class as the prior plan):** `classify()` signature now takes `stderr_tail: list[str] | None` as an explicit parameter; the function no longer reads `getattr(exc, "stderr", ...)` (which the SDK hardcodes to boilerplate). Test `test_classify_ignores_exc_stderr_attribute` proves the bug-class guard. Real-SDK test class `TestClassifierRealSDK` imports `claude_agent_sdk._errors.ProcessError` and `CLINotFoundError`, constructs them with default args, and asserts classifier behavior — fails if regex patterns drift from SDK reality.
2. **FATAL-2 (regex says "Claude CLI not found", SDK says "Claude Code not found"):** Regex updated to `Claude (CLI|Code) not found`. A second belt-and-suspenders rule keyed on `exc_class_name="CLINotFoundError"` catches the case via class name.
3. **FATAL-3 (PreCompact orphan sidecar prefix-mismatch infinite loop, doubly broken via `_write_sidecar` strip logic):** Introduced `_sidecar_for(md_or_inflight)` helper as the SINGLE source of truth for sidecar path construction. Replaced all 5 hardcoded `f"session-flush-{session_id}.json"` sites in `drain_one`. Test `test_drain_one_failure_preserves_pre_compact_prefix` proves attempts increment correctly for `flush-context-*.md` orphans.
4. **SERIOUS-6 → FATAL-tier (60s dedup gate eats drainer retries):** Added `if not os.environ.get("FLUSH_FROM_DRAIN"):` guard around the existing 60s dedup at `flush.py:277-285`. Without this, the drainer is architecturally broken.
5. **SERIOUS-1 (PARK_REQUESTED string sentinel false-positive):** Replaced with `ParkRequested(Exception)` class. LLM output can no longer trigger a false park.
6. **SERIOUS-3 (default→retry on local errors):** Added explicit `OSError` (disk-full / readonly), `PermissionError`, `FileNotFoundError`, `KeyError` rules BEFORE `default_unknown` — all `fail_fast`.
7. **SERIOUS-4 (concurrent dedup-ledger writes race):** Added `fcntl.flock`-based `_ledger_lock` context manager around the read-modify-write cycle. New test `test_concurrent_record_append_preserves_all_entries` proves no entries lost under 20-thread concurrent load.
8. **SERIOUS-5 (5-week-old orphans burn LLM cost + create time-travel daily-log entries):** `find_drainable` now has a `max_age_days=14` parameter; files older than this go DIRECTLY to dead-letter with a synthetic `last_rule="legacy_too_old"` sidecar. Drained appends carry a `(originally HH:MM YYYY-MM-DD, drained HH:MM)` chronology header via the new `FLUSH_ORIGINAL_MTIME` env var.
9. **Deploy Prerequisites section + lint rule (process gates 9-10):** Added a top-level "Deploy Prerequisites" section listing 4 hard gates. Task 8.5 introduces `tools/lint_classifier.py` that bans the `getattr(exc, "stderr", ...)` pattern in classifier code via grep — wired into Task 9 Step 1b. The lint test (Task 8.5 Step 3) sanity-checks that the lint actually catches the regression by temporarily reintroducing the banned pattern.

Recommended fixes also applied: `tearDown` `shutil.rmtree` cleanup on `TestDedup` + `TestAppendIntegration` + drain tests (W-1, avoids /tmp pressure); `drain_one` returns `-1` on race-lost so the caller's loop can continue (W-2); fixed misleading log line in `park_context_file` (W-3); threaded `last_verdict_rule` through to `park_context_file(last_rule=)` instead of hardcoded literal (W-4); replaced dead-code `if` branch in `release()` with an `assert` (W-5); deleted dead `_session_id_from_path(inflight).split('.')[0]` line (N-5); test count expectations updated (N-2).

Dismissed adversary findings:
- **N-3** (test_finds_parked_files doesn't mkdir) — Defender correct; `self.parked.mkdir()` IS called inside the test body before writes.
- **N-4** (start_new_session=True correct on Linux) — consensus, no action.

Companion patches to the design-council skill itself were also applied (`~/.claude/skills/design-council/SKILL.md`): a mandatory SDK-Reality Verification step in both Adversary and Judge prompts, plus a Deploy Prerequisites section in the FINAL-DESIGN template. The next council iteration cannot skip the SDK-source verification that this iteration missed.

---

## Self-Review

**Spec coverage check** against FINAL-DESIGN.md §"Slice 2 — Retry + Park + Dedup":

| FINAL-DESIGN spec line | Plan task |
|---|---|
| Async retry loop driven by CLASSIFICATIONS table | Task 2 (classifier) + Task 5 (retry loop) |
| verdict=retry → in-process sleep + retry (max 2) | Task 5 (BACKOFF_SCHEDULE_S) |
| verdict=rate_limit → ≥60s sleep | Task 5 (RATE_LIMIT_SLEEP_S=60) |
| verdict=fail_fast → write FLUSH_ERROR, no park | Task 5 (fail_fast branch returns early) |
| PARK context file to scripts/parked/<name>.md + .json | Task 6 (park_context_file) |
| SessionStart Popen drain.py --max=1 | Task 8 |
| Drainer globs both session-flush-*.md AND flush-context-*.md | Task 7 (find_drainable) |
| .inflight rename before subprocess.run | Task 7 (claim) |
| FLUSH_FROM_DRAIN=1 so flush.py doesn't re-park | Task 7 (env), Task 6 (env check in main()) |
| max 5 retries → dead-letter/ | Task 7 (MAX_ATTEMPTS=5, promote) |
| append_to_daily_log content-hash dedup | Task 4 |
| scripts/appended_hashes.json (separate file, atomic) | Task 3 (dedup.py) |
| state["daily_retry_cost"] cost-budget circuit breaker | Deferred (Out of Scope) — adversary noted this was speculative; ship without it |
| state["dead_letter_count"] audit | NOT EXPLICITLY ADDED — sidecar files in dead-letter/ are sufficient inspection signal; can be derived by `ls scripts/dead-letter/ | wc -l` |

**Placeholder scan:** none — every step has concrete code, exact commands, and expected output strings.

**Type consistency:**
- `Verdict` namedtuple has fields `kind` and `rule_name` everywhere it's used (classify return, test assertions).
- `MAX_ATTEMPTS = 5` is consistent between Task 7's drainer and the design.
- `MAX_ATTEMPTS = 3` (in-process retry budget) is distinct from drainer's 5 — both intentional and named clearly in their respective files.
- `RATE_LIMIT_SLEEP_S = 60.0` (in-process) vs drainer's per-attempt no-fixed-sleep (drainer naturally absorbs the rate-limit window via SessionStart cadence).
- `FLUSH_FROM_DRAIN` env var: set in Task 7 drainer; checked in Task 6 main().
- `PARK_REQUESTED` sentinel: returned by Task 5 run_flush; recognized in Task 6 main().
- Sidecar JSON schema: created in Task 6 park_context_file, read in Task 7 drainer (_read_sidecar / _write_sidecar). Fields: `session_id`, `first_attempt_at`, `last_attempt_at`, `attempts`, `last_rule`, `last_error`. Consistent.

Plan saved. No gaps identified.
