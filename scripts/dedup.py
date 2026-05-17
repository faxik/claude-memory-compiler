"""Content-hash dedup ledger for the daily-log append site.

Solves three failure modes in one place:
  1. In-process retry replays the same content after a server-side
     success / client-side failure → would duplicate the daily-log entry.
  2. Drainer replays a parked context file after a transient failure
     that already wrote to the daily log → same duplicate.
  3. Legacy orphan adoption (the 32 session-flush-*.md files dating
     back to 2026-04-14) → re-processing them risks re-appending content
     the daily log already has.

Ledger lives at scripts/appended_hashes.json — a separate file from
state.json deliberately, to avoid the round-1 design-council finding
that state.json has non-atomic multi-writer hazards. This file is
single-purpose and atomically replaced via os.replace, with an
exclusive lock around the read-modify-write cycle to prevent
concurrent writers from losing each other's entries.

Hash is SHA256-16 (first 16 hex chars of SHA256). Collision space ~2^64,
expected daily session volume < 1000, collision odds ~10^-13.

Entries TTL after 24 hours and are pruned on every record_append.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

_TTL = timedelta(hours=24)


@contextmanager
def _ledger_lock(ledger_path: Path) -> Iterator[None]:
    """Cross-platform exclusive lock around the ledger file.

    POSIX: fcntl.flock on a sibling .lock file. The .lock file is
    intentionally persistent (single file, normal flock pattern; not
    unlinked to avoid races between unlink and next open).

    Non-POSIX: degrades to no-lock with no warning. The codebase targets
    Linux+macOS; cross-process race is theoretical on single-user Windows.
    """
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = ledger_path.with_suffix(ledger_path.suffix + ".lock")
    if sys.platform == "win32":
        yield
        return
    try:
        import fcntl  # POSIX only
    except ImportError:
        yield
        return
    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _load(ledger_path: Path) -> dict[str, str]:
    """Read ledger; tolerate missing or corrupt files (treat as empty)."""
    if not ledger_path.exists():
        return {}
    try:
        data = json.loads(ledger_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _prune(data: dict[str, str], *, now: datetime | None = None) -> dict[str, str]:
    """Drop entries older than TTL. Returns a NEW dict; doesn't mutate input."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - _TTL
    kept: dict[str, str] = {}
    for key, iso in data.items():
        try:
            ts = datetime.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept[key] = iso
    return kept


def _atomic_write(ledger_path: Path, data: dict[str, str]) -> None:
    """Tmp-then-os.replace. Crash-safe."""
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = ledger_path.with_suffix(ledger_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, ledger_path)


def should_append(content: str, ledger_path: Path) -> bool:
    """Return True iff this content has not been appended in the last 24h.

    Read-only — does NOT modify the ledger. Caller must follow with
    record_append() when the actual append succeeds.
    """
    data = _load(ledger_path)
    return _hash(content) not in data


def record_append(content: str, ledger_path: Path) -> None:
    """Record that content was just appended; prune stale entries.

    Atomically replaces the ledger file (tmp + os.replace), with an
    exclusive lock around the read-modify-write cycle to prevent
    concurrent flush.py instances from losing each other's entries.
    """
    with _ledger_lock(ledger_path):
        data = _load(ledger_path)
        data = _prune(data)
        data[_hash(content)] = datetime.now(timezone.utc).isoformat()
        _atomic_write(ledger_path, data)
