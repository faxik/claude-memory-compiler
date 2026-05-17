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
        """Dominant pattern: 1637/1637 known-bad lines."""
        exc = _FakeProcessError("Command failed with exit code 1", exit_code=1)
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "process_exit_1")

    def test_cli_not_found_by_class_is_fail_fast(self):
        """Belt-and-suspenders: match by class name even if message changes."""
        class CLINotFoundError(Exception):
            pass
        v = classify(CLINotFoundError("anything"))
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "cli_not_found_by_class")

    def test_cli_not_found_by_message_is_fail_fast(self):
        """Real SDK message says 'Claude Code not found'."""
        exc = Exception("Claude Code not found. Install with: npm install ...")
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "cli_not_found_by_message")

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
        exc = Exception("some unprecedented error nobody has seen before")
        v = classify(exc)
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "default_unknown")

    def test_verdict_is_immutable_namedtuple(self):
        v = Verdict(kind="retry", rule_name="test")
        self.assertEqual(v.kind, "retry")
        with self.assertRaises(AttributeError):
            v.kind = "fail_fast"  # type: ignore[misc]

    def test_classify_uses_stderr_tail_for_rate_limit_signal(self):
        """Real bundled-CLI stderr arrives via the options.stderr callback,
        NOT via exc.stderr (which is hardcoded boilerplate).
        Classifier must accept stderr_tail as an explicit param."""
        exc = _FakeProcessError("Command failed with exit code 1", exit_code=1)
        v = classify(exc, stderr_tail=["HTTP 429 rate_limit_exceeded"])
        self.assertEqual(v.kind, "retry")
        self.assertEqual(v.rule_name, "rate_limit_signal")

    def test_classify_ignores_exc_stderr_attribute(self):
        """SDK's ProcessError.stderr is hardcoded boilerplate. The classifier
        MUST NOT read it. Verifies the FATAL-1 bug-class guard."""
        exc = _FakeProcessError(
            "Command failed with exit code 1",
            exit_code=1,
            stderr="HTTP 429 rate_limit_exceeded",  # would be a lie in real SDK
        )
        # Without stderr_tail arg, classifier MUST NOT pick up the planted
        # rate-limit text. Falls through to process_exit_1.
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

    def test_filenotfound_is_fail_fast(self):
        v = classify(FileNotFoundError("missing"))
        self.assertEqual(v.kind, "fail_fast")
        self.assertEqual(v.rule_name, "local_filenotfound")


class TestClassifierRealSDK(unittest.TestCase):
    """DEPLOY GATE: imports the REAL claude_agent_sdk exception classes
    and constructs them with default arguments. Fails if regex literals
    drift from the SDK's actual messages — the bug class the design
    council was convened to catch."""

    def test_real_cli_not_found_classifies_fail_fast(self):
        from claude_agent_sdk._errors import CLINotFoundError
        exc = CLINotFoundError(
            "Claude Code not found. Install with:\n"
            "  npm install -g @anthropic-ai/claude-code"
        )
        v = classify(exc)
        self.assertEqual(v.kind, "fail_fast")
        # Class-name match should win (rule listed first).
        self.assertEqual(v.rule_name, "cli_not_found_by_class")

    def test_real_process_error_with_hardcoded_stderr_classifies_retry(self):
        """SDK hardcodes ProcessError.stderr to boilerplate. Without
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
        # Must NOT be rate_limit_signal — the boilerplate doesn't match it.
        self.assertNotEqual(v.rule_name, "rate_limit_signal")


if __name__ == "__main__":
    unittest.main()
