"""Acceptance tests for the compile.py gate + crash-honesty fixes.

Covers the two bugs documented in BUG-compile-retry-and-cost-leak.md:

- Bug A (gate): a file already ingested with a matching hash must not be
  re-listed for compilation, and a file that has crashed
  MAX_COMPILE_ATTEMPTS times in a row must be quarantined.
- Bug B (cost honesty): when the LLM stream crashes AFTER billing, the
  actual debited cost must be recorded into state["wasted_cost"], the
  per-file retry counter must increment, and state["ingested"] must NOT
  gain an entry for the failed file.

Run from repo root:
    uv run python scripts/test_compile_gate.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# Ensure scripts/ is on sys.path so 'import config' / 'import utils' /
# 'import compile' resolve when invoked via pytest or the __main__ block.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


class CompileGateTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reroute STATE_FILE to a tmp location so save_state() in the
        # crash recorder writes there instead of clobbering real state.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.state_file = self.tmp / "state.json"

        import config  # type: ignore[import-not-found]
        import utils  # type: ignore[import-not-found]
        import compile as compile_mod  # type: ignore[import-not-found]

        self._orig_config_state = config.STATE_FILE
        self._orig_utils_state = utils.STATE_FILE
        config.STATE_FILE = self.state_file
        utils.STATE_FILE = self.state_file
        self.config = config
        self.utils = utils
        self.compile_mod = compile_mod

    def tearDown(self) -> None:
        self.config.STATE_FILE = self._orig_config_state
        self.utils.STATE_FILE = self._orig_utils_state
        self._tmpdir.cleanup()

    # ── Bug A: gate skips already-ingested file with matching hash ──

    def test_gate_skips_file_with_matching_hash(self) -> None:
        daily = self.tmp / "2026-05-21.md"
        daily.write_text("hello world\n", encoding="utf-8")
        h = self.utils.file_hash(daily)

        state = {
            "ingested": {
                "2026-05-21.md": {
                    "hash": h,
                    "compiled_at": "2026-05-21T23:46:44+02:00",
                    "cost_usd": 4.18,
                }
            },
            "compile_attempts": {},
        }

        to_compile = self.compile_mod.select_files_to_compile(state, [daily])
        self.assertEqual(to_compile, [],
                         "ingested file with matching hash must not be re-listed")

    def test_gate_picks_file_when_hash_differs(self) -> None:
        daily = self.tmp / "2026-05-21.md"
        daily.write_text("v2 content\n", encoding="utf-8")

        state = {
            "ingested": {
                "2026-05-21.md": {
                    "hash": "stale_hash_here",
                    "compiled_at": "2026-05-20T10:00:00+02:00",
                    "cost_usd": 1.0,
                }
            },
            "compile_attempts": {},
        }

        to_compile = self.compile_mod.select_files_to_compile(state, [daily])
        self.assertEqual(to_compile, [daily],
                         "file with mismatched hash must be picked for recompile")

    # ── Quarantine: skips a file that has failed MAX_COMPILE_ATTEMPTS times ──

    def test_quarantine_blocks_after_max_attempts(self) -> None:
        daily = self.tmp / "2026-05-21.md"
        daily.write_text("v2 content\n", encoding="utf-8")

        state = {
            "ingested": {
                "2026-05-21.md": {
                    "hash": "stale_hash_here",
                    "compiled_at": "2026-05-20T10:00:00+02:00",
                    "cost_usd": 1.0,
                }
            },
            "compile_attempts": {
                "2026-05-21.md": self.compile_mod.MAX_COMPILE_ATTEMPTS,
            },
        }

        to_compile = self.compile_mod.select_files_to_compile(state, [daily])
        self.assertEqual(to_compile, [],
                         "file at the retry budget must be quarantined")

    def test_quarantine_releases_below_max(self) -> None:
        daily = self.tmp / "2026-05-21.md"
        daily.write_text("v2 content\n", encoding="utf-8")

        state = {
            "ingested": {
                "2026-05-21.md": {"hash": "stale", "cost_usd": 1.0},
            },
            "compile_attempts": {
                "2026-05-21.md": self.compile_mod.MAX_COMPILE_ATTEMPTS - 1,
            },
        }

        to_compile = self.compile_mod.select_files_to_compile(state, [daily])
        self.assertEqual(to_compile, [daily],
                         "file below retry budget must still be compiled")

    # ── Bug B: crash recorder captures debited cost honestly ──

    def test_record_crash_persists_cost_and_attempt(self) -> None:
        state = {
            "ingested": {},
            "compile_attempts": {},
            "wasted_cost": 0.0,
            "total_cost": 100.0,
        }

        self.compile_mod._record_crash(
            state, "2026-05-21.md", 3.41, RuntimeError("simulated SDK crash"),
        )

        self.assertEqual(state["wasted_cost"], 3.41,
                         "crashed cost must be added to wasted_cost")
        self.assertEqual(state["compile_attempts"]["2026-05-21.md"], 1,
                         "crash must increment per-file retry counter")
        self.assertNotIn("2026-05-21.md", state["ingested"],
                         "crashed file must NOT be marked ingested")
        self.assertEqual(state["total_cost"], 100.0,
                         "total_cost (successful spend) must be untouched on crash")

        # Verify the save hit disk so a subsequent process sees the bump.
        import json
        on_disk = json.loads(self.state_file.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["wasted_cost"], 3.41)
        self.assertEqual(on_disk["compile_attempts"]["2026-05-21.md"], 1)

    def test_record_crash_accumulates_attempts(self) -> None:
        state = {
            "ingested": {},
            "compile_attempts": {"2026-05-21.md": 1},
            "wasted_cost": 3.41,
        }

        self.compile_mod._record_crash(
            state, "2026-05-21.md", 3.91, RuntimeError("another crash"),
        )

        self.assertEqual(state["compile_attempts"]["2026-05-21.md"], 2)
        self.assertAlmostEqual(state["wasted_cost"], 7.32, places=4)

    def test_record_crash_zero_cost_still_bumps_attempt(self) -> None:
        # Crash before the LLM billed anything (network failure pre-response).
        state = {"ingested": {}, "compile_attempts": {}, "wasted_cost": 0.0}

        self.compile_mod._record_crash(
            state, "2026-05-21.md", 0.0, RuntimeError("connection refused"),
        )

        self.assertEqual(state["wasted_cost"], 0.0)
        self.assertEqual(state["compile_attempts"]["2026-05-21.md"], 1)

    # ── Success path: clears the retry counter ──

    def test_record_success_resets_attempt_counter(self) -> None:
        daily = self.tmp / "2026-05-21.md"
        daily.write_text("compiled content\n", encoding="utf-8")

        state = {
            "ingested": {},
            "compile_attempts": {"2026-05-21.md": 1},
            "total_cost": 10.0,
        }

        self.compile_mod._record_success(state, daily, 2.50)

        self.assertEqual(state["compile_attempts"]["2026-05-21.md"], 0,
                         "successful compile must reset retry counter")
        self.assertIn("2026-05-21.md", state["ingested"])
        self.assertEqual(state["ingested"]["2026-05-21.md"]["cost_usd"], 2.50)
        self.assertEqual(state["ingested"]["2026-05-21.md"]["hash"],
                         self.utils.file_hash(daily))
        self.assertEqual(state["total_cost"], 12.50)


if __name__ == "__main__":
    unittest.main()
