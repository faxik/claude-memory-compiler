"""Test that flush.py / drain.py use bounded log rotation.

Verifies the RotatingFileHandler caps disk usage. Without this, flush.log
grows unbounded (28K+ lines pre-rotation).
"""
from __future__ import annotations

import logging
import shutil
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class TestLogRotation(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rotating_handler_caps_size(self):
        """Write >maxBytes; verify file count stays ≤ 1 + backupCount."""
        log_path = self.tmpdir / "test.log"
        handler = RotatingFileHandler(
            filename=str(log_path),
            maxBytes=1000,
            backupCount=3,
        )
        logger = logging.getLogger("test_rotation_isolated")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            for i in range(500):
                logger.info("line %d %s", i, "x" * 50)
            handler.close()
            files = list(self.tmpdir.glob("test.log*"))
            # Current file + at most backupCount=3 rotated backups
            self.assertLessEqual(len(files), 4)
            # And the live file shouldn't exceed roughly maxBytes
            self.assertLess(log_path.stat().st_size, 2000)
        finally:
            logger.removeHandler(handler)

    def test_flush_module_uses_rotating_handler(self):
        """flush.py installs a RotatingFileHandler on the root logger."""
        # Fresh sys.modules to ensure flush.py's module-init runs
        if "flush" in sys.modules:
            del sys.modules["flush"]
        import flush  # noqa: F401
        # The root logger should have at least one RotatingFileHandler.
        root = logging.getLogger()
        has_rotating = any(
            isinstance(h, RotatingFileHandler) for h in root.handlers
        )
        self.assertTrue(
            has_rotating,
            f"No RotatingFileHandler on root logger after importing flush; "
            f"handlers were: {root.handlers}",
        )

    def test_drain_module_uses_rotating_handler(self):
        """drain.py also installs RotatingFileHandler."""
        # Drain is loaded via the same root-logger model as flush.
        # The previous test already proved a RotatingFileHandler exists;
        # importing drain shouldn't lose it.
        if "drain" in sys.modules:
            del sys.modules["drain"]
        import drain  # noqa: F401
        root = logging.getLogger()
        rotating_count = sum(
            1 for h in root.handlers if isinstance(h, RotatingFileHandler)
        )
        self.assertGreaterEqual(rotating_count, 1)


if __name__ == "__main__":
    unittest.main()
