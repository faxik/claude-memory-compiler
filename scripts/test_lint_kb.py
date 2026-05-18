"""Canary tests for tools/lint_kb.py.

Each test stages a synthetic KB in a temp directory and verifies the
lint fires on the expected defect. T4.E in COMPLETION-CHECKLIST.md
enumerates exactly 4 canaries: missing-frontmatter, malformed-frontmatter,
dead-wikilink, orphan.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINT_KB_PATH = ROOT / "tools" / "lint_kb.py"


def _load_lint_kb_with_knowledge_dir(knowledge_dir: Path):
    """Load the lint_kb module fresh with KNOWLEDGE_DIR pointed at tmp."""
    spec = importlib.util.spec_from_file_location("lint_kb_under_test", LINT_KB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lint_kb_under_test"] = mod
    spec.loader.exec_module(mod)
    mod.KNOWLEDGE_DIR = knowledge_dir
    return mod


class TestLintKB(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.kb = self.tmpdir / "knowledge"
        self.concepts = self.kb / "concepts"
        self.concepts.mkdir(parents=True)
        # Index file present but empty (for the orphan check)
        (self.kb / "index.md").write_text(
            "# Knowledge Base Index\n\n", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _good_article(self, slug: str, body: str = "Body content.") -> Path:
        path = self.concepts / f"{slug}.md"
        path.write_text(
            f"---\ntitle: {slug}\n---\n\n{body}\n",
            encoding="utf-8",
        )
        return path

    # T4.E.1
    def test_canary_missing_frontmatter(self):
        bad = self.concepts / "no-frontmatter.md"
        bad.write_text("Just text, no --- block.\n", encoding="utf-8")
        lint = _load_lint_kb_with_knowledge_dir(self.kb)
        violations = lint.check_frontmatter(self.concepts)
        self.assertTrue(
            any("missing frontmatter" in v for v in violations),
            f"expected 'missing frontmatter' violation, got: {violations}",
        )

    # T4.E.2
    def test_canary_malformed_frontmatter(self):
        bad = self.concepts / "unclosed.md"
        bad.write_text(
            "---\ntitle: unclosed\nNo closing fence ever.\n",
            encoding="utf-8",
        )
        lint = _load_lint_kb_with_knowledge_dir(self.kb)
        violations = lint.check_frontmatter(self.concepts)
        self.assertTrue(
            any("no closing '---'" in v for v in violations),
            f"expected 'no closing ---' violation, got: {violations}",
        )

    # T4.E.3
    def test_canary_dead_wikilink(self):
        self._good_article("foo")
        bad = self.concepts / "uses-deadlink.md"
        bad.write_text(
            "---\ntitle: bad\n---\n\nSee [[concepts/does-not-exist]] for more.\n",
            encoding="utf-8",
        )
        lint = _load_lint_kb_with_knowledge_dir(self.kb)
        violations = lint.check_dead_links(self.concepts)
        self.assertTrue(
            any("does-not-exist" in v for v in violations),
            f"expected dead-link violation for 'does-not-exist', got: {violations}",
        )

    # T4.E.4
    def test_canary_orphan(self):
        # Three articles. foo and bar reference each other; baz is orphan.
        (self.concepts / "foo.md").write_text(
            "---\ntitle: foo\n---\n\nSee [[concepts/bar]].\n", encoding="utf-8",
        )
        (self.concepts / "bar.md").write_text(
            "---\ntitle: bar\n---\n\nSee [[concepts/foo]].\n", encoding="utf-8",
        )
        (self.concepts / "baz.md").write_text(
            "---\ntitle: baz\n---\n\nStandalone, no references.\n", encoding="utf-8",
        )
        lint = _load_lint_kb_with_knowledge_dir(self.kb)
        warnings = lint.check_orphans(self.concepts)
        self.assertTrue(
            any("baz.md" in w and "orphan" in w for w in warnings),
            f"expected 'baz.md' orphan WARN, got: {warnings}",
        )
        # foo and bar should NOT be flagged
        self.assertFalse(
            any("foo.md" in w and "orphan" in w for w in warnings),
            f"foo should not be orphan, got: {warnings}",
        )

    def test_clean_kb_returns_no_violations(self):
        """All-good KB: no FATAL, no warnings (everything cross-linked)."""
        (self.concepts / "alpha.md").write_text(
            "---\ntitle: alpha\n---\n\nSee [[concepts/beta]].\n", encoding="utf-8",
        )
        (self.concepts / "beta.md").write_text(
            "---\ntitle: beta\n---\n\nSee [[concepts/alpha]].\n", encoding="utf-8",
        )
        lint = _load_lint_kb_with_knowledge_dir(self.kb)
        self.assertEqual(lint.check_frontmatter(self.concepts), [])
        self.assertEqual(lint.check_dead_links(self.concepts), [])
        self.assertEqual(lint.check_orphans(self.concepts), [])


if __name__ == "__main__":
    unittest.main()
