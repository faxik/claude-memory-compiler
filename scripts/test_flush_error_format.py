"""Tests for _build_flush_error_response (Slice 1 observability).

Uses unittest (stdlib) — pyproject.toml has no pytest dep and the
problem brief's constraint #6 forbids adding one. The helper under
test is synchronous, so plain unittest.TestCase is the right base
class (no asyncio involvement).
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
