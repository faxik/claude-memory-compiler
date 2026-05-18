"""Audit classifier rules for ground-truth backing.

Deploy gate / advisory tool. For each non-default rule in
`scripts/classifier.py::CLASSIFICATIONS`, the rule must either:

  (a) have at least one real-world match in `scripts/flush.log` (the
      enriched Slice-1 FLUSH_ERROR format with `exit_code=` + indented
      `stderr_tail:` block), OR

  (b) carry a `# UNVERIFIED — speculative` comment in classifier.py
      somewhere in the few lines above its `_Rule(...)` definition.

Default behavior:
  - exit 0 = every rule is grounded OR explicitly speculative.
  - exit 1 = at least one rule is neither.

`--advisory` mode also reports which speculative rules NOW have enough
real-world matches to promote out of UNVERIFIED status (per
`FU-FU1-T0-C` followup target).

Block-aware parsing: Slice 1's FLUSH_ERROR header is the first line of
a multi-line block. We group the indented `stderr_tail:` lines with
their header for pattern matching.

Usage:
  python tools/check_classifier_groundedness.py
  python tools/check_classifier_groundedness.py --advisory
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSIFIER = ROOT / "scripts" / "classifier.py"
FLUSH_LOG = ROOT / "scripts" / "flush.log"

# Match a _Rule(...) instantiation; capture the `name="..."` field.
_RULE_NAME_RE = re.compile(r"""name\s*=\s*['"](?P<name>[^'"]+)['"]""")
_RULE_START_RE = re.compile(r"^\s*_Rule\($")
_COMMENT_RE = re.compile(r"^\s*#")


def _classifier_rules() -> list[tuple[str, int, bool]]:
    """Return [(rule_name, lineno, has_unverified)] for every _Rule in
    CLASSIFICATIONS.

    UNVERIFIED detection: scan the contiguous block of `#`-comment lines
    immediately preceding the `_Rule(` line. Any UNVERIFIED marker
    anywhere in that block counts. Stops at the first non-comment / non-
    blank line. This is robust to long comment blocks (no fixed window).
    """
    text = CLASSIFIER.read_text(encoding="utf-8")
    lines = text.splitlines()
    results: list[tuple[str, int, bool]] = []
    for idx, line in enumerate(lines):
        if not _RULE_START_RE.match(line):
            continue
        # Find the `name=` line within the rule body.
        for body_line in lines[idx : idx + 8]:
            m = _RULE_NAME_RE.search(body_line)
            if m:
                name = m.group("name")
                # Walk upward from idx-1; collect consecutive comment lines.
                # Stop at the first non-comment line (blank lines break the block).
                back = idx - 1
                comment_block: list[str] = []
                while back >= 0:
                    prev = lines[back]
                    if _COMMENT_RE.match(prev):
                        comment_block.append(prev)
                        back -= 1
                    else:
                        break
                has_unverified = any("UNVERIFIED" in c for c in comment_block)
                results.append((name, idx + 1, has_unverified))
                break
    return results


def _flush_log_signatures() -> str:
    """Return all FLUSH_ERROR block content from scripts/flush.log
    concatenated as a single haystack. Block-aware: a FLUSH_ERROR header
    line plus its indented continuation lines form one block."""
    if not FLUSH_LOG.exists():
        return ""
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for line in FLUSH_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        # Header lines look like "<timestamp> <LEVEL> FLUSH_ERROR: ..."
        if "FLUSH_ERROR:" in line:
            if current:
                blocks.append("\n".join(current))
            current = [line]
            in_block = True
            continue
        # Continuation lines for the structured Slice-1 format start with
        # at least 2 spaces (e.g., "  message: ...", "  stderr_tail:",
        # "    <stderr line>").
        if in_block and (line.startswith("  ") or line.startswith("\t")):
            current.append(line)
            continue
        # Non-indented line that's not a FLUSH_ERROR header — end the block.
        if current:
            blocks.append("\n".join(current))
            current = []
        in_block = False
    if current:
        blocks.append("\n".join(current))
    return "\n---\n".join(blocks)


def _rule_message_pattern(rule_name: str) -> re.Pattern[str] | None:
    """Re-extract the message_pattern compiled regex for a named rule.

    We re-parse rather than import classifier.CLASSIFICATIONS directly
    so this script is safe to run even when the classifier module has
    been edited and won't import (e.g., during a worktree session).

    Bounds the search to the SAME _Rule block — stops before the next
    `name=` line or `),\n` closing the rule. Returns None if the rule
    has `message_pattern=None`.
    """
    text = CLASSIFIER.read_text(encoding="utf-8")
    name_match = re.search(
        rf"""name\s*=\s*['"]{re.escape(rule_name)}['"]""", text
    )
    if not name_match:
        return None
    rest = text[name_match.end():]
    # Bound to the current _Rule block: stop at the next `name=` line
    # (= next rule's anchor) OR at the `),\n    _Rule(` boundary.
    bound = len(rest)
    next_name = re.search(r"""name\s*=\s*['"]""", rest)
    if next_name:
        bound = min(bound, next_name.start())
    block = rest[:bound]
    # message_pattern=None within the block → no pattern
    if re.search(r"message_pattern\s*=\s*None", block):
        return None
    pattern_match = re.search(
        r"""message_pattern\s*=\s*re\.compile\(\s*r?['"](?P<pat>[^'"]+)['"]""",
        block,
    )
    if not pattern_match:
        return None
    try:
        return re.compile(pattern_match.group("pat"), re.IGNORECASE)
    except re.error:
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Report speculative rules that now have real-world matches "
        "(advisory only; exits 0).",
    )
    args = parser.parse_args()

    rules = _classifier_rules()
    if not rules:
        print(f"ERROR: no _Rule entries found in {CLASSIFIER}")
        return 2

    signatures = _flush_log_signatures()

    violations: list[str] = []
    advisory_promotions: list[tuple[str, int]] = []

    for name, lineno, has_unverified in rules:
        # Skip rules that don't pattern-match on message (e.g., default_unknown,
        # process_exit_1, class-name-only matches). They're not subject to
        # speculative-regex concerns.
        pattern = _rule_message_pattern(name)
        if pattern is None:
            continue
        if name == "default_unknown":
            continue
        matches = pattern.findall(signatures) if signatures else []
        match_count = len(matches)

        if match_count > 0 and has_unverified:
            advisory_promotions.append((name, match_count))
            continue

        if match_count == 0 and not has_unverified:
            violations.append(
                f"{CLASSIFIER}:{lineno}: rule '{name}' has zero real-world matches "
                f"in flush.log AND no '# UNVERIFIED — speculative' annotation. "
                "Either add the annotation or remove the rule."
            )

    if violations:
        for v in violations:
            print(v)
        print(f"\ncheck_classifier_groundedness: {len(violations)} violation(s).")
        return 1

    if args.advisory and advisory_promotions:
        print("Advisory — speculative rules with real-world matches (can promote):")
        for name, count in advisory_promotions:
            print(f"  {name}: {count} match(es) in flush.log")
        print(
            "\nWhen ≥7 days of Slice-1 telemetry have accumulated, consider "
            "removing the UNVERIFIED annotations on these rules and tightening "
            "their regexes based on the matched signatures."
        )

    print("check_classifier_groundedness: clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
