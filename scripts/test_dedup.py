"""Tests for the content-hash dedup ledger."""
from __future__ import annotations

import json
import shutil
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
        self.assertEqual(len(data), 1)
        hash_key = list(data.keys())[0]
        self.assertEqual(len(hash_key), 16)
        datetime.fromisoformat(data[hash_key])

    def test_entries_older_than_24h_are_pruned_on_record(self):
        old_iso = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.ledger.write_text(
            json.dumps({"aaaaaaaaaaaaaaaa": old_iso}),
            encoding="utf-8",
        )
        record_append("fresh content", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertNotIn("aaaaaaaaaaaaaaaa", data)
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
        self.ledger.write_text("{not valid json", encoding="utf-8")
        self.assertTrue(should_append("anything", self.ledger))
        record_append("anything", self.ledger)
        data = json.loads(self.ledger.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)

    def test_atomic_write_via_os_replace(self):
        record_append("content", self.ledger)
        tmp = self.ledger.with_suffix(self.ledger.suffix + ".tmp")
        self.assertFalse(tmp.exists())

    def test_should_append_does_not_create_ledger_file(self):
        result = should_append("content", self.ledger)
        self.assertTrue(result)
        self.assertFalse(self.ledger.exists())

    def test_append_to_daily_log_dedup_gate_blocks_duplicate(self):
        """End-to-end: flush.append_to_daily_log skips on duplicate content."""
        import os
        import flush  # patch its module-level paths to point at tmpdir
        original_daily = flush.DAILY_DIR
        original_scripts = flush.SCRIPTS_DIR
        try:
            flush.DAILY_DIR = Path(self.tmpdir) / "daily"
            flush.SCRIPTS_DIR = Path(self.tmpdir) / "scripts"
            flush.DAILY_DIR.mkdir(parents=True)
            flush.SCRIPTS_DIR.mkdir(parents=True)

            flush.append_to_daily_log("test content", "Session")
            flush.append_to_daily_log("test content", "Session")  # dup
            today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            log = flush.DAILY_DIR / f"{today}.md"
            text = log.read_text(encoding="utf-8")
            self.assertEqual(text.count("test content"), 1)
        finally:
            flush.DAILY_DIR = original_daily
            flush.SCRIPTS_DIR = original_scripts

    def test_append_to_daily_log_survives_corrupt_original_mtime(self):
        """M-4 fix: FLUSH_ORIGINAL_MTIME=inf must NOT crash the append.
        datetime.fromtimestamp(inf) raises OverflowError; our except
        catches it and falls back to the plain header."""
        import os
        import flush
        original_daily = flush.DAILY_DIR
        original_scripts = flush.SCRIPTS_DIR
        original_env = os.environ.get("FLUSH_ORIGINAL_MTIME")
        try:
            flush.DAILY_DIR = Path(self.tmpdir) / "daily"
            flush.SCRIPTS_DIR = Path(self.tmpdir) / "scripts"
            flush.DAILY_DIR.mkdir(parents=True)
            flush.SCRIPTS_DIR.mkdir(parents=True)
            os.environ["FLUSH_ORIGINAL_MTIME"] = "inf"

            # Must NOT raise.
            flush.append_to_daily_log("corrupt-mtime probe", "Memory Flush")
            today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            log = flush.DAILY_DIR / f"{today}.md"
            text = log.read_text(encoding="utf-8")
            self.assertIn("corrupt-mtime probe", text)
            # Plain header (no "originally" annotation).
            self.assertNotIn("originally", text)
        finally:
            flush.DAILY_DIR = original_daily
            flush.SCRIPTS_DIR = original_scripts
            if original_env is None:
                os.environ.pop("FLUSH_ORIGINAL_MTIME", None)
            else:
                os.environ["FLUSH_ORIGINAL_MTIME"] = original_env

    def test_append_to_daily_log_renders_chronology_header_when_mtime_set(self):
        """When FLUSH_ORIGINAL_MTIME is a valid timestamp, the section
        header carries the original time alongside the drain time."""
        import os, time
        import flush
        original_daily = flush.DAILY_DIR
        original_scripts = flush.SCRIPTS_DIR
        original_env = os.environ.get("FLUSH_ORIGINAL_MTIME")
        try:
            flush.DAILY_DIR = Path(self.tmpdir) / "daily"
            flush.SCRIPTS_DIR = Path(self.tmpdir) / "scripts"
            flush.DAILY_DIR.mkdir(parents=True)
            flush.SCRIPTS_DIR.mkdir(parents=True)
            # 2 hours ago
            os.environ["FLUSH_ORIGINAL_MTIME"] = str(time.time() - 7200)

            flush.append_to_daily_log("drained content probe", "Memory Flush")
            today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
            log = flush.DAILY_DIR / f"{today}.md"
            text = log.read_text(encoding="utf-8")
            self.assertIn("originally", text)
            self.assertIn("drained", text)
        finally:
            flush.DAILY_DIR = original_daily
            flush.SCRIPTS_DIR = original_scripts
            if original_env is None:
                os.environ.pop("FLUSH_ORIGINAL_MTIME", None)
            else:
                os.environ["FLUSH_ORIGINAL_MTIME"] = original_env

    def test_concurrent_record_append_preserves_all_entries(self):
        """SERIOUS-4 fix: fcntl.flock around read-modify-write must
        prevent lost entries when multiple writers race."""
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
        self.assertEqual(len(data), 20)


if __name__ == "__main__":
    unittest.main()
