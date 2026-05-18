"""Knowledge-base structural lint pre-pass.

Deterministic, $0-cost checks that run BEFORE the existing LLM-only
`scripts/lint.py` so the expensive LLM pass only sees structurally
valid input. Three classes of check:

  1. Frontmatter validation (FATAL):
     - Article must start with `---` on line 1.
     - Article must have a closing `---` later.
     - Body between fences must be non-empty.
  2. Dead-link detection (FATAL):
     - Every `[[wikilink]]` in any article must point to an existing
       `.md` file under `knowledge/` (e.g. `[[concepts/foo]]` →
       `knowledge/concepts/foo.md`).
  3. Orphan article detection (WARN):
     - An article with ZERO inbound `[[wikilinks]]` from any other
       article AND NOT mentioned in `knowledge/index.md` is a possible
       orphan. Logged but exit 0 — some articles are intentionally
       standalone.

Usage:
  python tools/lint_kb.py knowledge/concepts/          # all checks
  python tools/lint_kb.py --check frontmatter <path>   # one check only
  python tools/lint_kb.py --check dead-links <path>
  python tools/lint_kb.py --check orphans <path>

Exit codes:
  0  — no FATAL issues (warnings OK)
  1  — one or more FATAL issues
  2  — bad invocation / target directory missing
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWLEDGE_DIR = ROOT / "knowledge"

_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")
_FRONTMATTER_DELIM = "---"


def _walk_md(target: Path) -> list[Path]:
    """Return sorted list of *.md files under target (recursive)."""
    if target.is_file() and target.suffix == ".md":
        return [target]
    if target.is_dir():
        return sorted(target.rglob("*.md"))
    return []


def check_frontmatter(target: Path) -> list[str]:
    """Return list of FATAL-level violation messages."""
    violations: list[str] = []
    for path in _walk_md(target):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            violations.append(f"{path}: cannot read — {e}")
            continue
        lines = text.splitlines()
        if not lines:
            violations.append(f"{path}: empty file (no frontmatter)")
            continue
        if lines[0].strip() != _FRONTMATTER_DELIM:
            violations.append(
                f"{path}:1: missing frontmatter — first line is "
                f"{lines[0]!r}, expected '---'"
            )
            continue
        # Find the closing fence.
        close_lineno: int | None = None
        for idx in range(1, len(lines)):
            if lines[idx].strip() == _FRONTMATTER_DELIM:
                close_lineno = idx + 1  # 1-based
                break
        if close_lineno is None:
            violations.append(
                f"{path}: malformed frontmatter — opening '---' has no "
                "closing '---'"
            )
            continue
        # Body after frontmatter
        body_lines = lines[close_lineno:]
        body_text = "\n".join(body_lines).strip()
        if not body_text:
            violations.append(
                f"{path}:{close_lineno}: malformed — empty body after frontmatter"
            )
    return violations


def _collect_all_articles() -> set[str]:
    """Return set of all article slugs (relative to knowledge/ root,
    without .md suffix). E.g. 'concepts/autosorter-scrub-planner'."""
    slugs: set[str] = set()
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name in ("index.md", "log.md"):
            continue
        rel = path.relative_to(KNOWLEDGE_DIR).with_suffix("")
        slugs.add(str(rel))
    return slugs


def check_dead_links(target: Path) -> list[str]:
    """Return list of FATAL-level violations (dead wikilinks)."""
    violations: list[str] = []
    all_slugs = _collect_all_articles()
    for path in _walk_md(target):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            violations.append(f"{path}: cannot read — {e}")
            continue
        for match in _WIKILINK_RE.finditer(text):
            link = match.group(1).strip()
            # Strip an optional alias after `|` (Obsidian-style)
            link = link.split("|", 1)[0].strip()
            # Strip an optional anchor #section
            link = link.split("#", 1)[0].strip()
            # Strip a trailing .md if present
            if link.endswith(".md"):
                link = link[:-3]
            if not link:
                continue
            # daily/<date> references are intentional and not lint targets
            if link.startswith("daily/"):
                continue
            if link not in all_slugs:
                # Find the line number of the match
                lineno = text[: match.start()].count("\n") + 1
                violations.append(
                    f"{path}:{lineno}: dead wikilink [[{link}]] — "
                    f"target {KNOWLEDGE_DIR / (link + '.md')} not found"
                )
    return violations


def check_orphans(target: Path) -> list[str]:
    """Return list of WARN-level violations (orphans). Exit-0 issues."""
    # Build inbound-link map across ALL articles
    inbound: dict[str, int] = {}
    index_text = ""
    index_path = KNOWLEDGE_DIR / "index.md"
    if index_path.exists():
        index_text = index_path.read_text(encoding="utf-8")
    for path in KNOWLEDGE_DIR.rglob("*.md"):
        if path.name in ("log.md",):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for match in _WIKILINK_RE.finditer(text):
            link = match.group(1).strip().split("|", 1)[0].split("#", 1)[0]
            if link.endswith(".md"):
                link = link[:-3]
            inbound[link] = inbound.get(link, 0) + 1

    warnings: list[str] = []
    for path in _walk_md(target):
        if path.name in ("index.md", "log.md"):
            continue
        rel = path.relative_to(KNOWLEDGE_DIR).with_suffix("")
        slug = str(rel)
        # Self-references don't count as inbound; but linkers from other
        # articles do. We track total appearances in inbound[slug] — a
        # standalone article only appears via its own self-references in
        # its own body (if any). For simplicity treat slug-count 0 OR
        # the article-not-mentioned-in-index case as orphan candidate.
        link_count = inbound.get(slug, 0)
        in_index = (f"[[{slug}]]" in index_text)
        # Subtract self-references (count of [[<slug>]] within the
        # article itself, if any).
        try:
            self_text = path.read_text(encoding="utf-8")
            self_refs = sum(
                1 for m in _WIKILINK_RE.finditer(self_text)
                if m.group(1).strip().split("|", 1)[0].split("#", 1)[0].rstrip(".md") == slug
            )
        except OSError:
            self_refs = 0
        external_in = link_count - self_refs
        if external_in <= 0 and not in_index:
            warnings.append(
                f"{path}: WARN orphan — zero inbound wikilinks from other "
                "articles AND not mentioned in index.md"
            )
    return warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", help="Path to scan (file or dir)")
    parser.add_argument(
        "--check",
        choices=["frontmatter", "dead-links", "orphans", "all"],
        default="all",
    )
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not target.exists():
        print(f"ERROR: target {target} not found", file=sys.stderr)
        return 2

    fatal: list[str] = []
    warn: list[str] = []

    if args.check in ("frontmatter", "all"):
        fatal.extend(check_frontmatter(target))
    if args.check in ("dead-links", "all"):
        fatal.extend(check_dead_links(target))
    if args.check in ("orphans", "all"):
        warn.extend(check_orphans(target))

    for v in fatal:
        print(f"FATAL: {v}")
    for w in warn:
        print(w)

    if fatal:
        print(
            f"\nlint_kb: {len(fatal)} FATAL violation(s), "
            f"{len(warn)} warning(s). Exit 1.",
            file=sys.stderr,
        )
        return 1

    print(
        f"\nlint_kb: clean ({len(warn)} warning(s); FATAL-clean exit 0).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
