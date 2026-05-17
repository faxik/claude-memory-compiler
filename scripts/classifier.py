"""Failure-mode classifier for the retry/park decision.

Inspects exception attributes (class name, str(exc), exit_code) plus an
optional stderr_tail (real bundled-CLI stderr captured via the
options.stderr callback) and matches against a rule table. Returns one
of three verdicts:

- "retry": transient; the in-process retry loop should attempt again.
- "fail_fast": permanent (auth, prompt size, missing CLI, local I/O);
  skip retry.
- (note: "rate_limit" is intentionally not a separate kind today —
  rate-limit signals route through "retry" with a distinct rule_name so
  the caller can pick a longer backoff if desired.)

Rules are ordered: first match wins. The default (last row) is "retry"
on the principle that an unrecognized error is more likely to be a new
transient pattern than a new permanent one — one wasted retry is
cheaper than fail-fasting on a recoverable failure.
"""
from __future__ import annotations

import re
from typing import NamedTuple


class Verdict(NamedTuple):
    """Frozen result of classify(exc, ...)."""

    kind: str  # "retry" | "fail_fast"
    rule_name: str  # which CLASSIFICATIONS row matched (for logging)


class _Rule(NamedTuple):
    name: str
    exc_class_name: str | None  # None = match any class
    message_pattern: re.Pattern[str] | None  # None = match any message
    exit_code: int | None  # None = match any (or absent) exit_code
    verdict_kind: str  # "retry" | "fail_fast"


# Order matters: first match wins. More-specific rules go first.
#
# Slice 1 evidence note (2026-05-17 → 2026-05-18 deploy window): the two
# dominant historical patterns in this codebase's flush.log are
# "Control request timeout: initialize" (~10 occurrences) and "Command
# failed with exit code 1" (1637 occurrences). Both treated as retry.
# Auth/prompt-size patterns are seeded from Anthropic API conventions
# but have not yet been observed in this codebase's flush.log; refine
# after Slice 1 evidence accumulates.
CLASSIFICATIONS: tuple[_Rule, ...] = (
    # Permanent — missing CLI binary (class-name match first, message
    # pattern as belt-and-suspenders in case SDK message changes again).
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
    # Permanent — auth failure. Not yet observed in this codebase's
    # flush.log; pattern seeded from Anthropic API conventions. Refine
    # after Slice 1 evidence accumulates (see "Deploy Prerequisites").
    _Rule(
        name="auth_invalid",
        exc_class_name=None,
        message_pattern=re.compile(
            r"invalid.*api.?key|authentication.*failed|401\b", re.IGNORECASE
        ),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Permanent — request too large. Same caveat as auth_invalid.
    _Rule(
        name="prompt_too_long",
        exc_class_name=None,
        message_pattern=re.compile(
            r"prompt.*too long|context.*exceeds|maximum.*tokens", re.IGNORECASE
        ),
        exit_code=None,
        verdict_kind="fail_fast",
    ),
    # Rate-limit signal — must be matched against the REAL stderr_tail
    # text (the SDK's exception stderr attribute is hardcoded boilerplate;
    # see classify() docstring). Stays "retry" verdict but the rule_name
    # carries the signal so the caller can pick a longer backoff.
    _Rule(
        name="rate_limit_signal",
        exc_class_name=None,
        message_pattern=re.compile(
            r"\b429\b|rate.?limit|too many requests", re.IGNORECASE
        ),
        exit_code=None,
        verdict_kind="retry",
    ),
    # Transient — the 2026-04-12 cluster.
    _Rule(
        name="control_request_timeout",
        exc_class_name=None,
        message_pattern=re.compile(r"Control request timeout", re.IGNORECASE),
        exit_code=None,
        verdict_kind="retry",
    ),
    # Transient — the 2026-05-13 cluster (1637/1637 known FLUSH_ERROR lines).
    _Rule(
        name="process_exit_1",
        exc_class_name=None,
        message_pattern=None,
        exit_code=1,
        verdict_kind="retry",
    ),
    # Default — unknown errors. Treated as retry on the
    # "cheap-retry vs silent-data-loss" tradeoff.
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
      - exc class name (e.g. "ProcessError"), when a rule sets exc_class_name
      - str(exc) (e.g. "Command failed with exit code 1")
      - getattr(exc, "exit_code", None)
      - stderr_tail (the REAL bundled-CLI stderr, captured via the
        options.stderr callback into a bounded deque by the caller)

    The SDK's exception stderr attribute is hardcoded boilerplate at the
    subprocess transport layer; we deliberately do NOT read it from the
    exception. The only source of real rate-limit or stack-trace text is
    the options.stderr callback, which the caller must feed into the
    stderr_tail parameter.

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
