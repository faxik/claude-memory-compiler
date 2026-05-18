"""Tests for the drain script.

Tests exercise the drainer's file-discovery, idempotency, and
dead-letter promotion logic without invoking a real flush.py. We
patch subprocess.run to simulate success/failure outcomes.
"""
from __future__ import annotations

import json
import os
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
        self.scripts = self.tmpdir
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
        """flush-context-*.md is the PreCompact orphan prefix."""
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
        """SERIOUS-5 fix: orphans older than max_age_days quarantine
        directly to dead-letter rather than burning LLM cost on stale
        content."""
        import time
        old = self.scripts / "session-flush-legacy-old.md"
        old.write_text("ctx")
        old_ts = time.time() - 30 * 86400
        os.utime(str(old), (old_ts, old_ts))

        candidates = drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
            max_age_days=14,
        )
        self.assertEqual(candidates, [])
        self.assertTrue((self.dead_letter / "session-flush-legacy-old.md").exists())
        sidecar = self.dead_letter / "session-flush-legacy-old.json"
        self.assertTrue(sidecar.exists())
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["last_rule"], "legacy_too_old")

    def test_age_quarantine_relocates_existing_sidecar(self):
        """M-3 fix: when quarantining an old parked file, the ORIGINAL
        sidecar (with real attempt history) must move to dead-letter
        alongside the .md, NOT be orphaned in parked/."""
        import time
        self.parked.mkdir()
        old_md = self.parked / "session-flush-old.md"
        old_md.write_text("ctx")
        # Pre-existing sidecar with real history
        old_sidecar = self.parked / "session-flush-old.json"
        old_sidecar.write_text(json.dumps({
            "session_id": "old",
            "attempts": 3,
            "first_attempt_at": "2026-04-01T12:00:00+00:00",
            "last_rule": "process_exit_1",
            "last_error": "real error history",
        }))
        old_ts = time.time() - 30 * 86400
        os.utime(str(old_md), (old_ts, old_ts))

        drain.find_drainable(
            parked_dir=self.parked,
            scripts_dir=self.scripts,
            dead_letter_dir=self.dead_letter,
            max_age_days=14,
        )

        # Both files must be in dead-letter; original sidecar must NOT
        # be orphaned in parked/.
        self.assertTrue((self.dead_letter / "session-flush-old.md").exists())
        self.assertTrue((self.dead_letter / "session-flush-old.json").exists())
        self.assertFalse((self.parked / "session-flush-old.json").exists())
        # Verify the real history is preserved (NOT clobbered by synthetic).
        data = json.loads(
            (self.dead_letter / "session-flush-old.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["attempts"], 3)
        self.assertEqual(data["last_error"], "real error history")


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
        f = self.parked / "session-flush-x.md"
        f.write_text("ctx")
        first = drain.claim(f)
        self.assertIsNotNone(first)
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
                args=[], returncode=0, stdout=b"", stderr=b"",
            )
            n = drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        self.assertEqual(n, 1)
        self.assertFalse(any(self.parked.glob("*.inflight")))
        self.assertFalse((self.parked / "session-flush-ok.json").exists())

    def test_drain_one_failure_increments_attempts_and_releases_inflight(self):
        self._make_parked("fail", attempts=1)
        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"",
            )
            drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        md = self.parked / "session-flush-fail.md"
        self.assertTrue(md.exists())
        sidecar = self.parked / "session-flush-fail.json"
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["attempts"], 2)

    def test_drain_one_promotes_after_max_attempts(self):
        self._make_parked("dl", attempts=5)
        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"",
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
        (which doesn't exist) → attempts never increment → infinite retry."""
        # Synthesize a PreCompact orphan directly in scripts (no sidecar).
        md = self.tmpdir / "flush-context-pc1.md"
        md.write_text("pc context")

        with mock.patch("drain.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout=b"", stderr=b"",
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
        # Attempts incremented to 1 (not stuck at 0).
        data = json.loads(
            (self.tmpdir / "flush-context-pc1.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["attempts"], 1)

    def test_drain_one_returns_neg1_on_race_lost(self):
        """M-2 fix: when find_drainable returns candidates but every one
        is claimed by a concurrent drainer between discovery and claim(),
        drain_one returns -1 (not 0), so the caller's loop continues.

        We make claim() return None via monkey-patching to simulate the
        race deterministically."""
        self._make_parked("racy", attempts=1)

        real_claim = drain.claim
        def losing_claim(_p):
            return None  # race lost
        drain.claim = losing_claim
        try:
            result = drain.drain_one(
                parked_dir=self.parked,
                scripts_dir=self.tmpdir,
                dead_letter_dir=self.dead_letter,
                flush_script=Path("/fake/flush.py"),
                project_root=self.tmpdir,
            )
        finally:
            drain.claim = real_claim
        self.assertEqual(result, -1)

    def test_drain_one_returns_0_on_no_candidates(self):
        """When no candidates exist, drain_one returns 0 (not -1)."""
        result = drain.drain_one(
            parked_dir=self.parked,
            scripts_dir=self.tmpdir,
            dead_letter_dir=self.dead_letter,
            flush_script=Path("/fake/flush.py"),
            project_root=self.tmpdir,
        )
        self.assertEqual(result, 0)

    def test_drain_skips_when_budget_exceeded(self):
        """T0.D: when state.json's daily_retry_cost[today] exceeds cap,
        drain_one SKIPS the dispatch (does not call subprocess.run),
        releases the inflight, returns 0, and logs BUDGET_EXCEEDED."""
        # Set up a parked candidate.
        self._make_parked("budgetbusted", attempts=1)
        # Synthesize a state.json with daily_retry_cost above the cap.
        today = drain._today_iso()
        state_file = self.tmpdir / "state.json"
        state_file.write_text(json.dumps({
            "daily_retry_cost": {today: 20.0},  # above default cap $15
            "ingested": {},
        }))
        # Patch drain.STATE_FILE to point at the tmp state.json.
        original_state_file = drain.STATE_FILE
        drain.STATE_FILE = state_file
        try:
            with mock.patch("drain.subprocess.run") as run:
                result = drain.drain_one(
                    parked_dir=self.parked,
                    scripts_dir=self.tmpdir,
                    dead_letter_dir=self.dead_letter,
                    flush_script=Path("/fake/flush.py"),
                    project_root=self.tmpdir,
                )
                # subprocess.run MUST NOT have been called — budget gate fired
                run.assert_not_called()
            self.assertEqual(result, 0)
            # Inflight file released back to .md
            self.assertTrue((self.parked / "session-flush-budgetbusted.md").exists())
        finally:
            drain.STATE_FILE = original_state_file


if __name__ == "__main__":
    unittest.main()
