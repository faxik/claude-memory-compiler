"""Lint guard: block reads of `exc.stderr` in classifier code.

WHY THIS EXISTS

The claude_agent_sdk hardcodes ProcessError.stderr to the boilerplate
string "Check stderr output for details" (verified at
.venv/lib/python3.13/site-packages/claude_agent_sdk/_internal/transport/
subprocess_cli.py:613-617). Any classifier code that pattern-matches
against `getattr(exc, "stderr", ...)` is silently broken — the real
CLI stderr only arrives via the options.stderr callback.

This was the FATAL-1 bug class in rounds 1 and 2 of the adversarial
review. The lint exists so the bug class can't reappear without CI
catching it.

WHY AST (NOT GREP)

Round 3 of the adversarial review caught a meta-instance of the same
bug class in an earlier version of THIS file: a regex `\\.stderr\\b`
matched the `ProcessError.stderr` literal inside the classifier's own
docstring NOTE explaining why we don't read that attribute. To prevent
that, we use `ast` to identify line ranges occupied by string literals
(docstrings, regular string constants) and skip them when scanning
for banned patterns.

USAGE

    uv run python tools/lint_classifier.py

Exit code 0 = clean. Exit code 1 = banned pattern found.

A line can be marked exempt with `# noqa: classifier-stderr-ok`.
"""
from __future__ import annotations

import ast
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
        re.compile(r"""\bexc\.stderr\b"""),
        "Reading exc.stderr from an SDK exception is unsafe; the SDK "
        "hardcodes that attribute. Pass real stderr via stderr_tail.",
    ),
]


def _docstring_lines(text: str) -> set[int]:
    """Return 1-based line numbers occupied by docstrings.

    A docstring in Python is an `Expr` node whose value is a `Constant`
    with a string value, appearing as the FIRST statement of a Module,
    FunctionDef, AsyncFunctionDef, or ClassDef body.

    We deliberately do NOT skip every string constant — that would
    over-skip and cause `foo = getattr(exc, "stderr", "")` to be missed
    (the inline `"stderr"` literal is a Constant but not a docstring).

    Returns set() on parse failure so the lint over-reports rather than
    under-reports — false positives on a syntactically-broken file are
    a smaller harm than missed violations.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()

    lines: set[int] = set()

    def _collect(body: list) -> None:
        if not body:
            return
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
            and first.value.end_lineno is not None
        ):
            for ln in range(first.value.lineno, first.value.end_lineno + 1):
                lines.add(ln)

    # Module-level docstring.
    _collect(tree.body)
    # Class/function-level docstrings.
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _collect(node.body)

    return lines


def main() -> int:
    failures = 0
    for path in CLASSIFIER_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        skip_lines = _docstring_lines(text)
        for ln_no, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if ln_no in skip_lines:
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
