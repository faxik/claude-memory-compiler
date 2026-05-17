# Architect C — Park-and-Resume (Queue-Based Deferred Retry)

**Perspective.** Real-time retry inside a hook is the wrong abstraction. A hook fires *during* the user's interaction with Claude Code; users do not want a `SessionEnd` hook spinning 30 seconds in a rate-limit window. Treat every failure as **"park this work for later"** — write the unfinished work to disk, let a separate **drainer** pick it up when conditions are better, accept eventual consistency.

The compiler already does this **accidentally**. Verified evidence:

- `ls scripts/session-flush-*.md | wc -l` → **32 orphaned context files**, 349 KB total, oldest dated **2026-04-14**. Every time `flush.py` crashed before line 255 (`context_file.unlink(missing_ok=True)`), the input file stayed on disk. The compiler has been *involuntarily* parking failed work for 5+ weeks. We just don't have a drainer.
- `scripts/state.json` already nests sub-keys: `ingested` (completed compiles, batch-flush.py:431-441) and `batch_flush` (batch processing state, batch-flush.py:430-456). A `pending`/`parked` sub-key would be a one-line addition to a pattern that's been load-bearing since at least 2026-05-13.
- `flush.py:152-199` (`maybe_trigger_compilation` + `COMPILE_AFTER_HOUR`) is **already a deferred-work pattern**: end-of-day compilation defers from per-session to a daily window. The architecture has precedent for "do it later when the conditions are right."
- `scripts/batch-flush.py:430-456` shows the right way to do nested state: `processed_sessions` dict keyed by session_id, with cost + timestamp metadata. We copy this shape.

The key insight: **we don't need to discriminate transient-vs-permanent at all** if everything parks. The drainer's per-attempt failure count *is* the discrimination. After N attempts a parked item gets promoted to `dead-letter/` and only an operator can revive it. The SDK's untyped error surface (bare `Exception("Control request timeout: ...")`) becomes a non-problem because we never had to classify it — we just record it, sleep, and try again.

This also lets us **finally clean up the 32 orphaned `session-flush-*.md` files**: the drainer is exactly what they've been waiting for since April 14.

Three distinct sub-approaches follow, varying on (a) where the parked store lives, (b) when the drainer fires, (c) how dead-letter promotion works.

---

## Option C1 — "Quiet Park" (filesystem queue, SessionStart drainer, fixed N-budget)

### Core Idea

Failures are written to `scripts/parked/` as a pair of files: the original context `.md` (the work) and a `.json` sidecar (the metadata: attempts, last error, last attempt time). A drainer runs **opportunistically inside the `session-start.py` hook**, processing at most 1 parked item per session start so the hook stays under the 200ms budget. After N=5 failed attempts a parked item is moved to `scripts/parked/dead-letter/` and stays there forever unless an operator intervenes.

The drainer never fires from `PreCompact` (re-entrancy danger) and never blocks `SessionEnd` (the hook is the producer; the drainer is on a different hook).

### How It Works

**Directory shape.** Two new directories, both gitignored, both under `scripts/` (matches the convention — `scripts/session-flush-*.md`, `scripts/last-flush.json`, `scripts/flush.log` all already live there):

```
scripts/
├── parked/                                                # waiting queue
│   ├── 503a8ad5-...-20260517-143022.md                   # context payload
│   ├── 503a8ad5-...-20260517-143022.json                 # sidecar metadata
│   └── dead-letter/                                      # post-budget graveyard
│       └── ...
├── session-flush-...-20260414-205806.md                  # legacy orphans (drainer will adopt these)
└── state.json
```

**Sidecar shape** (one per parked item):

```json
{
  "session_id": "503a8ad5-9797-40c5-acc5-c6d775e1fd14",
  "parked_at": "2026-05-17T14:30:22+02:00",
  "attempts": 2,
  "last_attempt_at": "2026-05-17T15:01:11+02:00",
  "last_error": {
    "type": "Exception",
    "message": "Control request timeout: initialize",
    "exit_code": null,
    "stderr_tail": "...last 2KB of stderr..."
  },
  "source": "session-end",
  "context_size_bytes": 12468
}
```

**Producer change — `flush.py`.** The `except Exception` branch at line 144-147 no longer writes `FLUSH_ERROR` to the daily log on first failure. Instead it parks. **Crucially, the context file is NOT unlinked in this path** (line 255 only runs on success):

```python
# flush.py — replaces lines 118-149
PARKED_DIR = SCRIPTS_DIR / "parked"

async def run_flush(context: str) -> str | None:
    """Returns response string on success, None if parked for retry."""
    from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions,
                                  ResultMessage, TextBlock, query)
    response = ""
    try:
        # ... existing query() loop unchanged ...
        async for message in query(prompt=prompt, options=ClaudeAgentOptions(...)):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response += block.text
    except Exception as e:
        import traceback
        logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
        return None  # signal "park me"
    return response


# flush.py main(): around line 236
response = asyncio.run(run_flush(context))
if response is None:
    # Park the work — preserve context file, write sidecar
    park_for_retry(context_file, session_id, last_exception_summary())
    logging.info("Parked session %s for retry", session_id)
    return  # NB: do NOT unlink context_file; the drainer needs it
```

The `park_for_retry` helper:

```python
def park_for_retry(context_file: Path, session_id: str, error_summary: dict) -> None:
    PARKED_DIR.mkdir(exist_ok=True)
    # Move (not copy) the context file to parked/ so two concurrent hooks
    # can't both grab it.
    parked_md = PARKED_DIR / context_file.name
    try:
        context_file.replace(parked_md)  # atomic rename on same filesystem
    except OSError:
        # Cross-device fallback (shouldn't happen — both under scripts/)
        parked_md.write_bytes(context_file.read_bytes())
        context_file.unlink(missing_ok=True)

    sidecar = parked_md.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "session_id": session_id,
        "parked_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": error_summary,
        "source": os.environ.get("FLUSH_SOURCE", "session-end"),
        "context_size_bytes": parked_md.stat().st_size,
    }, indent=2), encoding="utf-8")
```

**Drainer — new file `scripts/drain.py`.** Single function, also callable as `python scripts/drain.py --max=N`:

```python
# scripts/drain.py
"""Process parked flush items. Fired from session-start.py with --max=1."""
from __future__ import annotations
import json, logging, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PARKED_DIR = SCRIPTS_DIR / "parked"
DEAD_LETTER = PARKED_DIR / "dead-letter"
FLUSH_SCRIPT = SCRIPTS_DIR / "flush.py"

MAX_ATTEMPTS = 5            # fixed N — no exponential schedule
MIN_AGE_SECONDS = 60        # don't immediately re-attempt a just-parked item
                            # (gives a rate-limit window time to expire)


def list_parked() -> list[Path]:
    if not PARKED_DIR.exists():
        return []
    return sorted(PARKED_DIR.glob("*.md"))  # oldest first by lexicographic timestamp


def load_sidecar(parked_md: Path) -> dict:
    sidecar = parked_md.with_suffix(".json")
    if sidecar.exists():
        try:
            return json.loads(sidecar.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    # Legacy orphan adoption: session-flush-*.md from before parking existed.
    return {"session_id": "unknown", "attempts": 0, "parked_at": None,
            "last_attempt_at": None, "source": "legacy-orphan"}


def save_sidecar(parked_md: Path, sidecar: dict) -> None:
    parked_md.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8")


def promote_to_dead_letter(parked_md: Path, sidecar: dict) -> None:
    DEAD_LETTER.mkdir(exist_ok=True)
    sidecar["promoted_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    parked_md.with_suffix(".json").write_text(
        json.dumps(sidecar, indent=2), encoding="utf-8")
    parked_md.replace(DEAD_LETTER / parked_md.name)
    parked_md.with_suffix(".json").replace(
        DEAD_LETTER / (parked_md.stem + ".json"))
    logging.warning("Promoted %s to dead-letter after %d attempts",
                    parked_md.name, sidecar["attempts"])


def attempt_one(parked_md: Path) -> bool:
    """Re-run flush.py on a parked context file. Returns True on success."""
    sidecar = load_sidecar(parked_md)

    # Don't re-attempt items younger than MIN_AGE_SECONDS — gives rate-limit
    # windows a chance to drain.
    if sidecar.get("last_attempt_at"):
        last = datetime.fromisoformat(sidecar["last_attempt_at"])
        if (datetime.now(timezone.utc) - last).total_seconds() < MIN_AGE_SECONDS:
            return False

    # Synchronous re-spawn of flush.py with the parked context.
    # We can call flush.py directly (uv-managed env is the parent's already).
    session_id = sidecar.get("session_id", "drain-" + parked_md.stem[:8])
    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(FLUSH_SCRIPT),
           str(parked_md), session_id]
    env = os.environ.copy()
    env["FLUSH_FROM_DRAIN"] = "1"   # tells flush.py to not re-park on failure
                                     # (drain updates the sidecar instead)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True,
                            timeout=180)  # hard cap — drainer cannot hang

    if result.returncode == 0 and not parked_md.exists():
        # flush.py succeeded — it unlinked the context file as it always does
        # on success. Clean up the sidecar.
        parked_md.with_suffix(".json").unlink(missing_ok=True)
        logging.info("Drained parked session %s on attempt %d",
                     session_id, sidecar["attempts"] + 1)
        return True

    # Failure path — bump attempts, store new error summary
    sidecar["attempts"] = sidecar.get("attempts", 0) + 1
    sidecar["last_attempt_at"] = datetime.now(timezone.utc).astimezone().isoformat()
    sidecar["last_error"] = {
        "type": "subprocess-failure",
        "message": f"flush.py exit_code={result.returncode}",
        "exit_code": result.returncode,
        "stderr_tail": (result.stderr or "")[-2048:],
    }
    if sidecar["attempts"] >= MAX_ATTEMPTS:
        promote_to_dead_letter(parked_md, sidecar)
    else:
        save_sidecar(parked_md, sidecar)
    return False


def drain(max_items: int = 1) -> tuple[int, int]:
    """Process up to max_items. Returns (succeeded, failed)."""
    PARKED_DIR.mkdir(exist_ok=True)
    succeeded = failed = 0
    for parked_md in list_parked()[:max_items]:
        if attempt_one(parked_md):
            succeeded += 1
        else:
            failed += 1
    return succeeded, failed


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--max", type=int, default=1)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    if args.dry_run:
        for parked_md in list_parked()[:args.max]:
            sidecar = load_sidecar(parked_md)
            print(f"{parked_md.name}  attempts={sidecar.get('attempts')}  "
                  f"last_error={sidecar.get('last_error', {}).get('message')}")
        return
    s, f = drain(args.max)
    print(f"drained: {s} succeeded, {f} failed")


if __name__ == "__main__":
    main()
```

**SessionStart wiring — `hooks/session-start.py`.** Add ~6 lines that spawn the drainer in the **background**, not synchronously. The hook still returns under 200ms because we don't `wait()`:

```python
# hooks/session-start.py — add before main()'s json.dumps output
def _kick_drainer() -> None:
    """Fire-and-forget background drain of 1 parked item."""
    drain_script = ROOT / "scripts" / "drain.py"
    if not drain_script.exists():
        return
    try:
        creation_flags = (subprocess.CREATE_NO_WINDOW
                          if sys.platform == "win32" else 0)
        # Background — hook returns immediately
        subprocess.Popen(
            ["uv", "run", "--directory", str(ROOT), "python",
             str(drain_script), "--max=1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(ROOT), creationflags=creation_flags,
            start_new_session=(sys.platform != "win32"),
        )
    except Exception:
        pass  # drainer failure must not break SessionStart


def main():
    _kick_drainer()
    context = build_context()
    # ... rest unchanged
```

**Legacy orphan adoption.** First time `drain.py` runs, it sees the 32 `session-flush-*.md` files already in `scripts/`. A one-shot migration (could be inlined into drain.py's first run or `drain.py --migrate`):

```python
def migrate_legacy_orphans() -> int:
    """Move existing scripts/session-flush-*.md into parked/ as 'legacy-orphan' source."""
    PARKED_DIR.mkdir(exist_ok=True)
    moved = 0
    for orphan in SCRIPTS_DIR.glob("session-flush-*.md"):
        target = PARKED_DIR / orphan.name
        orphan.replace(target)
        # Write a sidecar marking these as legacy
        sidecar = {
            "session_id": "legacy-" + orphan.stem.split("-")[2][:8],
            "parked_at": datetime.fromtimestamp(orphan.stat().st_mtime,
                                                tz=timezone.utc).isoformat(),
            "attempts": 0, "last_attempt_at": None,
            "last_error": {"type": "unknown", "message": "pre-parking-era orphan"},
            "source": "legacy-orphan",
            "context_size_bytes": target.stat().st_size,
        }
        target.with_suffix(".json").write_text(
            json.dumps(sidecar, indent=2), encoding="utf-8")
        moved += 1
    return moved
```

**DAILY-LOG order invariant.** When a parked item drains successfully hours after the original session, its entry appears in **resolution order**, not chronological order. The section header timestamp (`### Session (HH:MM)`) is the drain time, not the original time. This is **a deliberate trade-off**: chronological-by-section-title would require splicing into the middle of the file (race-prone, line-counting-sensitive). The sidecar's `parked_at` field preserves the original time for forensics. If the user cares about chronology, the drained entry can be prefixed with `(parked at HH:MM)` in the section body.

### Pros

- **Hooks stay fast.** SessionEnd's job becomes "spawn flush.py and exit." SessionStart's drainer is `Popen` with detached-session — returns in milliseconds.
- **Zero new error-classification logic.** We don't sniff `str(exc)` for "rate limit" patterns, don't whitelist exception classes, don't import from `claude_agent_sdk._errors`. The drainer fails-and-retries opaquely.
- **The 32 orphans get drained automatically.** The first SessionStart after this ships triggers legacy adoption.
- **Survives machine sleep / suspend.** A parked item from 2 AM gets drained at 9 AM when the user opens Claude Code. This is exactly the failure mode the user described in `session-flush-362bb38c-...-20260514-064926.md` (laptop suspended mid-batch).
- **Operator visibility for free.** `ls scripts/parked/` + `cat scripts/parked/*.json` is enough to diagnose. `dead-letter/` is the audit trail.
- **Effort fits the 1-hour budget.** ~120 LOC `drain.py` + ~30 LOC edits in `flush.py` + ~10 LOC in `session-start.py`. The bulk is straightforward filesystem manipulation.
- **No re-entrancy risk in `PreCompact`.** Drainer only fires from SessionStart; PreCompact continues to be producer-only.

### Cons

- **Resolution-order daily-log entries violate chronological order.** A flush that originally fired at 14:30 may appear in the log under `### Session (09:15)` the next morning. Forensics rely on the sidecar.
- **Fixed N-attempt budget can mask a persistent problem.** If the same bug keeps killing every flush, items just pile up in `dead-letter/` without alerting. Mitigation: a `drain.py --report` flag that prints dead-letter count.
- **Synchronous subprocess inside `drain.py`.** The drainer waits up to 180s for `flush.py`. If the drainer is invoked manually from a terminal, that's fine; if it's invoked from cron, also fine. But if a user fires multiple SessionStarts in quick succession, multiple `drain.py` processes can race on the same parked file. **Mitigation: `parked_md.replace(parked_md.with_suffix(".inflight"))` at the start of `attempt_one()`, restore on failure.** Add this as a small but real piece of the design.
- **`MIN_AGE_SECONDS=60` is arbitrary.** A 429 with a 5-minute window won't drain on the first opportunistic try after 60s; it'll burn an attempt. Mitigation: bump to 300s or parse `Retry-After`-style hints out of stderr_tail.
- **`dead-letter/` grows forever.** No GC. After a year of bad days, this could be hundreds of MB. Mitigation: `drain.py --gc --older-than-days=90` as a manual housekeeping step.

### Effort Estimate

**M (medium).** ~150 LOC new + ~40 LOC edits. Most of the complexity is in the atomic-rename + inflight-lock pattern, not the logic. About 60-90 min of careful implementation; another 30 min of pytest fixtures for the drainer.

### Risk Profile

- **Worst case:** the drainer has a bug and keeps re-parking successful flushes, so an item never drains. The N-attempt budget caps the blast radius at 5 retries before dead-letter promotion stops the bleeding.
- **Subprocess hangs:** `subprocess.run(..., timeout=180)` is the safety belt. Without it a hung flush.py would hold the drainer forever; with it, the worst case is a 3-minute drainer process which is invisible to the user (background-spawned).
- **Race on parked file:** two concurrent SessionStarts could try to drain the same item. The `.inflight` rename mitigation handles this.
- **Disk fill:** 32 orphans × ~10KB ≈ 320 KB so far. Even a year of failures stays under 50 MB. Not a real risk for the foreseeable future.
- **Re-entrancy:** flush.py spawned by drain.py runs `claude_agent_sdk.query()`, which triggers Claude Code, which fires SessionStart again, which... wait, no — the `CLAUDE_INVOKED_BY` env at flush.py:16 short-circuits the recursion guard at session-start.py top. **Verified safe.**

---

## Option C2 — "State.json Queue" (in-state nested dict, opportunistic drainer on every Nth hook, exponential-backoff scheduling)

### Core Idea

The parked store lives **inside `scripts/state.json`** as a new top-level key `pending_flushes`, parallel to `ingested` (compile.py state) and `batch_flush` (batch state). No new directories, no JSON sidecars: one canonical state file holds the queue, the metadata, and the work pointers. The drainer fires **opportunistically every Nth SessionEnd hook** (the producer hook), not SessionStart — so it piggybacks on the user activity that creates work in the first place. Dead-letter promotion is by **exponential-backoff scheduled next-attempt** rather than fixed N-budget: an item that has failed K times waits `min(2^K * 60, 24*3600)` seconds before its next attempt is eligible.

This is the **state-machine-flavored** variant: the queue is data, not files; the drainer is a scheduler, not a worker; the dead-letter is "too-far-future next-attempt-at" rather than a separate directory.

### How It Works

**State shape — added to `scripts/state.json`:**

```json
{
  "ingested": { ... existing ... },
  "batch_flush": { ... existing ... },
  "pending_flushes": {
    "503a8ad5-9797-40c5-acc5-c6d775e1fd14-20260517-143022": {
      "session_id": "503a8ad5-9797-40c5-acc5-c6d775e1fd14",
      "context_file": "scripts/parked/503a8ad5-...-20260517-143022.md",
      "parked_at": "2026-05-17T14:30:22+02:00",
      "attempts": 2,
      "next_attempt_at": "2026-05-17T14:38:22+02:00",
      "last_error": {
        "type": "Exception",
        "message": "Control request timeout: initialize",
        "stderr_tail": "..."
      },
      "source": "session-end"
    }
  }
}
```

The **context file** still lives on disk (we can't stuff 15KB markdown into a JSON value cleanly), but its path is referenced by `pending_flushes[item_id].context_file`. The state.json key is the source of truth for "is this item live?"; if a context file exists with no matching state entry, it's an orphan and gets adopted on next drainer pass.

`pending_flushes` keys are sortable (timestamp-suffix) so the drainer iterates in oldest-first order.

**Producer — `flush.py` change:**

```python
# flush.py — replace except-block at line 144-147
import contextlib
import fcntl  # for state.json locking; Windows uses msvcrt.locking — handle below

STATE_JSON = SCRIPTS_DIR / "state.json"
PARKED_DIR = SCRIPTS_DIR / "parked"

@contextlib.contextmanager
def state_lock():
    """Cross-process advisory lock on state.json."""
    lock_path = STATE_JSON.with_suffix(".lock")
    lock_path.touch(exist_ok=True)
    with open(lock_path, "r+") as f:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def load_state() -> dict:
    if not STATE_JSON.exists():
        return {}
    try:
        return json.loads(STATE_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_JSON.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(STATE_JSON)


def park_to_state(context_file: Path, session_id: str, error: Exception) -> None:
    PARKED_DIR.mkdir(exist_ok=True)
    parked_md = PARKED_DIR / context_file.name
    context_file.replace(parked_md)

    item_id = parked_md.stem
    with state_lock():
        state = load_state()
        pending = state.setdefault("pending_flushes", {})
        pending[item_id] = {
            "session_id": session_id,
            "context_file": str(parked_md.relative_to(ROOT)),
            "parked_at": datetime.now(timezone.utc).astimezone().isoformat(),
            "attempts": 0,
            "next_attempt_at": _schedule_next(0),
            "last_error": _summarize_error(error),
            "source": os.environ.get("FLUSH_SOURCE", "session-end"),
        }
        save_state(state)


def _schedule_next(attempts: int) -> str:
    """Exponential backoff: 60s, 120s, 240s, ..., capped at 24h."""
    delay = min(60 * (2 ** attempts), 24 * 3600)
    next_at = datetime.now(timezone.utc).astimezone() + timedelta(seconds=delay)
    return next_at.isoformat()


def _summarize_error(e: Exception) -> dict:
    return {
        "type": type(e).__name__,
        "message": str(e)[:500],
        "exit_code": getattr(e, "exit_code", None),
        "stderr_tail": (getattr(e, "stderr", "") or "")[-2048:],
    }
```

And the existing `except Exception` block becomes:

```python
except Exception as e:
    import traceback
    logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
    if os.environ.get("FLUSH_FROM_DRAIN"):
        # Drainer-spawned attempt: bump attempt count in state, don't re-park
        bump_attempt_in_state(context_file, e)
        return  # drainer reads state to know we failed
    park_to_state(context_file, session_id, e)
    return  # do not unlink — context now lives in parked/
```

**Opportunistic drainer — fires every Nth SessionEnd.** New file `scripts/drain.py` (similar shape to C1 but reads from state.json instead of directory scan), invoked from `session-end.py` with a "1 in 5" probability:

```python
# hooks/session-end.py — add after Popen(flush.py) at line 167
# Opportunistic drainer: every 5th session, kick a background drain.
# Hash-based rather than random so tests are deterministic.
should_drain = (int(session_id.replace("-", "")[:8], 16) % 5) == 0
if should_drain:
    drain_script = SCRIPTS_DIR / "drain.py"
    try:
        subprocess.Popen(
            ["uv", "run", "--directory", str(ROOT), "python",
             str(drain_script), "--budget=2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
            start_new_session=(sys.platform != "win32"),
        )
    except Exception:
        pass
```

The `drain.py` itself:

```python
# scripts/drain.py
def drain(budget: int = 2) -> tuple[int, int]:
    """Drain up to `budget` items whose next_attempt_at has elapsed."""
    now = datetime.now(timezone.utc).astimezone()
    succeeded = failed = 0

    with state_lock():
        state = load_state()
        pending = state.get("pending_flushes", {})
        # Sort by parked_at — oldest first
        eligible = sorted(
            ((k, v) for k, v in pending.items()
             if datetime.fromisoformat(v["next_attempt_at"]) <= now),
            key=lambda kv: kv[1]["parked_at"],
        )

    for item_id, meta in eligible[:budget]:
        context_path = ROOT / meta["context_file"]
        if not context_path.exists():
            # Orphaned state entry — context file gone. Drop from state.
            with state_lock():
                state = load_state()
                state.get("pending_flushes", {}).pop(item_id, None)
                save_state(state)
            continue

        # Synchronous flush.py re-run
        env = os.environ.copy()
        env["FLUSH_FROM_DRAIN"] = "1"
        result = subprocess.run(
            ["uv", "run", "--directory", str(ROOT), "python",
             str(SCRIPTS_DIR / "flush.py"),
             str(context_path), meta["session_id"]],
            env=env, capture_output=True, text=True, timeout=180,
        )

        if result.returncode == 0 and not context_path.exists():
            # Success — flush.py unlinked the context. Drop state entry.
            with state_lock():
                state = load_state()
                state.get("pending_flushes", {}).pop(item_id, None)
                save_state(state)
            succeeded += 1
        else:
            # Failure — bump attempts, schedule next, possibly dead-letter
            with state_lock():
                state = load_state()
                pending = state.setdefault("pending_flushes", {})
                if item_id in pending:
                    pending[item_id]["attempts"] += 1
                    pending[item_id]["next_attempt_at"] = _schedule_next(
                        pending[item_id]["attempts"])
                    pending[item_id]["last_attempt_at"] = now.isoformat()
                    # "Dead-letter" = next_attempt_at >= 24h out (the cap)
                    save_state(state)
            failed += 1

    return succeeded, failed
```

**Dead-letter promotion is implicit.** An item that has failed 8+ times has `next_attempt_at = parked_at + 24h` (the cap); after that it just keeps coming up once a day. There's no separate `dead-letter/` directory. Instead, a `drain.py --status` command surfaces "stuck" items:

```python
def status() -> None:
    state = load_state()
    pending = state.get("pending_flushes", {})
    if not pending:
        print("No parked items.")
        return
    print(f"{len(pending)} parked items:")
    for item_id, meta in sorted(pending.items(), key=lambda kv: kv[1]["parked_at"]):
        stuck = "  [STUCK]" if meta["attempts"] >= 8 else ""
        print(f"  {item_id}  attempts={meta['attempts']:2}  "
              f"next={meta['next_attempt_at']}{stuck}")
        print(f"    err: {meta['last_error']['message'][:80]}")
```

The operator's mental model: "if `drain.py --status` shows `[STUCK]`, look at it; otherwise the system is self-healing."

### Pros

- **Single source of truth.** State.json is already the canonical state file. Now the queue lives there too. Backup/inspection/diff is one file.
- **Exponential backoff handles rate-limit windows naturally.** A 429 cluster at attempt 1 → wait 60s; if still failing at attempt 3 → wait 4 min; by attempt 5 → 16 min. Real 429 windows expire inside this curve.
- **No separate dead-letter directory to GC.** Stuck items stay in `pending_flushes` with capped backoff; the operator sees them via `--status`. Cleaner end-state than C1.
- **Opportunistic drainer means freshness when traffic is high.** If the user is actively using Claude Code, parked work drains within ~5 sessions. If they stop, nothing drains until they come back — but that's also when they care.
- **Drainer-from-SessionEnd composes with the existing flush.py spawn.** Both are detached Popens; the OS scheduler handles concurrency.

### Cons

- **State.json contention.** Now multiple processes (flush.py, drain.py, batch-flush.py, compile.py) all want to write the same file. The `fcntl.flock` lock is correct but adds a real failure mode: a crashed process holding the lock blocks everyone. **Mitigation: lock timeout via `fcntl.LOCK_NB` retry loop**, but this complicates the code.
- **Cross-platform lock code is ugly.** `fcntl` on POSIX, `msvcrt.locking` on Windows. Tested on neither today.
- **State.json grows unboundedly if dead-letter promotion is implicit.** 100 truly-permanent failures = 100 keys in pending_flushes forever. C1's explicit `dead-letter/` directory at least *moves* the data out of the hot path.
- **Opportunistic-every-5th means latency until first drain is undefined.** If the user has one short session and quits, parked items wait until the next session. C1's "drain on every SessionStart" is more eager.
- **Race: `flush.py` running normally and `drain.py` both write state.json.** The lock handles correctness but the failure mode of "drain.py blocked 30s on flush.py's lock" is real and ugly.
- **Exponential-backoff schedule is opinionated.** A persistently-failing item gets attempted at 60s, 2min, 4min, 8min, 16min, 32min, 1h, 2h, 4h, 8h, 16h, 24h, 24h, 24h... — the 24h cap means once stuck, the item gets *one shot per day*. If the operator clears the underlying issue at 10:00 AM, the next attempt might not happen until 23:30 the next night.

### Effort Estimate

**M-to-L.** ~200 LOC + cross-platform locking is the hard part. The state-machine logic is straightforward; the lock semantics and the test fixtures around them are not. About 90-120 min.

### Risk Profile

- **Worst case:** state.json lock corruption blocks all writes. flush.py refuses to park, normal flushes refuse to record `ingested`, compile.py refuses to record state. Effectively a full freeze of the compiler's persistence layer until the operator deletes the lock file. **Hard to recover gracefully.**
- **Stuck items invisible by default.** Without `drain.py --status` being part of the user's habit, items can accumulate silently. C1's `dead-letter/` directory at least shows up in `ls`.
- **JSON-corruption blast radius is larger.** A bad write that hoses state.json now loses the queue AND the ingested ledger AND the batch-flush state. The atomic-rename pattern (`tmp.replace(STATE_JSON)`) mitigates but doesn't eliminate.

---

## Option C3 — "Append-Only Journal" (JSONL queue, manual-only drainer, operator-promoted dead-letter)

### Core Idea

Forget the queue-as-state-machine. The parked store is a **single append-only JSONL log**: `scripts/parked.jsonl`. Every park event appends one line; every successful drain appends one "resolved" line referencing the original; every promotion to dead-letter appends one "promoted" line. The drainer **never fires from hooks**. It fires only when the operator runs `compile.py --drain` (or `drain.py` directly). Hooks become pure producers; the operator becomes the consumer.

This is the **most conservative variant.** Hooks gain a single line of new code (call `park_to_journal`). The compiler becomes more honest: failed work goes to a visible, append-only log, and the operator decides what to do with it. No background scheduling, no opportunistic firing, no race conditions.

### How It Works

**Journal shape — `scripts/parked.jsonl`** (one JSON object per line):

```jsonl
{"event":"parked","ts":"2026-05-17T14:30:22+02:00","item_id":"503a8ad5-...-20260517-143022","session_id":"503a8ad5-...","context_file":"scripts/parked/503a8ad5-...-20260517-143022.md","error":{"type":"Exception","message":"Control request timeout: initialize","stderr_tail":"..."},"source":"session-end"}
{"event":"attempted","ts":"2026-05-17T18:01:11+02:00","item_id":"503a8ad5-...-20260517-143022","outcome":"failed","error":{"type":"Exception","message":"Control request timeout: initialize"}}
{"event":"resolved","ts":"2026-05-17T19:33:42+02:00","item_id":"503a8ad5-...-20260517-143022","outcome":"success","cost_usd":0.0023}
{"event":"parked","ts":"2026-05-17T20:15:00+02:00","item_id":"abc12345-...-20260517-201500","session_id":"abc12345-...","context_file":"...","error":{...},"source":"pre-compact"}
{"event":"promoted","ts":"2026-05-18T09:00:00+02:00","item_id":"abc12345-...-20260517-201500","reason":"operator-promotion-from-status-cmd"}
```

The current "live" parked set is derived by replaying the log: for each `item_id`, the latest event wins (`parked` → live, `resolved` → done, `promoted` → dead-letter).

**Producer — `flush.py`:**

```python
# flush.py
PARKED_DIR = SCRIPTS_DIR / "parked"
JOURNAL = SCRIPTS_DIR / "parked.jsonl"

def append_journal(record: dict) -> None:
    """Atomic single-line append to journal. POSIX O_APPEND is atomic for < PIPE_BUF (4KB)."""
    record_line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    if len(record_line.encode("utf-8")) > 3500:
        # Truncate stderr_tail if needed to stay under PIPE_BUF atomicity bound
        if "error" in record and "stderr_tail" in record["error"]:
            record["error"]["stderr_tail"] = record["error"]["stderr_tail"][-1500:]
            record_line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(record_line)


def park_to_journal(context_file: Path, session_id: str, error: Exception) -> None:
    PARKED_DIR.mkdir(exist_ok=True)
    parked_md = PARKED_DIR / context_file.name
    context_file.replace(parked_md)

    append_journal({
        "event": "parked",
        "ts": datetime.now(timezone.utc).astimezone().isoformat(),
        "item_id": parked_md.stem,
        "session_id": session_id,
        "context_file": str(parked_md.relative_to(ROOT)),
        "error": {
            "type": type(error).__name__,
            "message": str(error)[:300],
            "exit_code": getattr(error, "exit_code", None),
            "stderr_tail": (getattr(error, "stderr", "") or "")[-1500:],
        },
        "source": os.environ.get("FLUSH_SOURCE", "session-end"),
    })


# Replace the except-block in run_flush:
except Exception as e:
    import traceback
    logging.error("Agent SDK error: %s\n%s", e, traceback.format_exc())
    park_to_journal(context_file, session_id, e)
    return  # no daily-log FLUSH_ERROR write — the journal IS the log
```

**Drainer — `scripts/drain.py`:**

```python
# scripts/drain.py
"""Manual drainer. Never fires from hooks. Operator runs:
    uv run python scripts/drain.py --status         # see what's parked
    uv run python scripts/drain.py --drain          # attempt all live items
    uv run python scripts/drain.py --promote ITEM   # send to dead-letter
"""
def replay_journal() -> dict[str, dict]:
    """Return {item_id: latest_state_for_that_item}."""
    live: dict[str, dict] = {}
    if not JOURNAL.exists():
        return live
    with open(JOURNAL, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = rec.get("item_id")
            if not item_id:
                continue
            if rec["event"] == "parked":
                live[item_id] = rec
            elif rec["event"] == "attempted":
                if item_id in live:
                    live[item_id]["attempts"] = live[item_id].get("attempts", 0) + 1
                    live[item_id]["last_attempt"] = rec
            elif rec["event"] in ("resolved", "promoted"):
                live.pop(item_id, None)
    return live


def status() -> None:
    live = replay_journal()
    if not live:
        print("No parked items.")
        return
    print(f"{len(live)} parked items:")
    for item_id, rec in sorted(live.items(), key=lambda kv: kv[1]["ts"]):
        attempts = rec.get("attempts", 0)
        ts = rec["ts"]
        err = rec["error"]["message"][:80]
        print(f"  {item_id}")
        print(f"    parked_at={ts}  attempts={attempts}")
        print(f"    error: {err}")
        print(f"    source: {rec['source']}")


def drain_all(dry_run: bool = False) -> tuple[int, int]:
    live = replay_journal()
    succeeded = failed = 0
    for item_id, rec in sorted(live.items(), key=lambda kv: kv[1]["ts"]):
        context_path = ROOT / rec["context_file"]
        if not context_path.exists():
            # Stale journal entry, context lost. Mark resolved-by-disappearance.
            append_journal({"event": "resolved", "ts": _now(),
                            "item_id": item_id, "outcome": "context-vanished"})
            continue
        if dry_run:
            print(f"[dry-run] would attempt {item_id}")
            continue
        print(f"Attempting {item_id} ({rec['session_id']})...")
        result = subprocess.run(
            ["uv", "run", "--directory", str(ROOT), "python",
             str(SCRIPTS_DIR / "flush.py"),
             str(context_path), rec["session_id"]],
            env={**os.environ, "FLUSH_FROM_DRAIN": "1"},
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode == 0 and not context_path.exists():
            append_journal({"event": "resolved", "ts": _now(),
                            "item_id": item_id, "outcome": "success"})
            print(f"  ✓ drained")
            succeeded += 1
        else:
            append_journal({"event": "attempted", "ts": _now(),
                            "item_id": item_id, "outcome": "failed",
                            "error": {"exit_code": result.returncode,
                                      "stderr_tail": (result.stderr or "")[-1500:]}})
            print(f"  ✗ failed (exit {result.returncode})")
            failed += 1
    return succeeded, failed


def promote(item_id: str, reason: str = "operator-decision") -> None:
    live = replay_journal()
    if item_id not in live:
        print(f"Not parked: {item_id}")
        return
    DEAD_LETTER = PARKED_DIR / "dead-letter"
    DEAD_LETTER.mkdir(exist_ok=True)
    src = ROOT / live[item_id]["context_file"]
    if src.exists():
        src.replace(DEAD_LETTER / src.name)
    append_journal({"event": "promoted", "ts": _now(),
                    "item_id": item_id, "reason": reason})
    print(f"Promoted {item_id} to dead-letter.")


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    p.add_argument("--drain", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--promote", metavar="ITEM_ID")
    args = p.parse_args()
    if args.status:
        status()
    elif args.drain:
        s, f = drain_all(dry_run=args.dry_run)
        print(f"\nResult: {s} succeeded, {f} failed.")
    elif args.promote:
        promote(args.promote)
    else:
        p.print_help()
```

**Hook changes are nearly zero.** SessionEnd/PreCompact don't change at all (they still spawn `flush.py`; flush.py is the one that journals). SessionStart optionally adds a one-line **passive warning** if parked items exceed a threshold — but does NOT invoke the drainer:

```python
# hooks/session-start.py — addition to build_context()
def parked_warning() -> str:
    """Surface a passive warning if many items are parked."""
    journal = ROOT / "scripts" / "parked.jsonl"
    if not journal.exists():
        return ""
    # Quick replay — don't import drain.py to keep hook cold-start fast
    live = 0
    try:
        with open(journal, encoding="utf-8") as f:
            seen: dict[str, str] = {}
            for line in f:
                try:
                    rec = json.loads(line)
                    iid = rec.get("item_id")
                    if not iid: continue
                    seen[iid] = rec["event"]
                except Exception:
                    continue
            live = sum(1 for v in seen.values() if v == "parked")
    except OSError:
        return ""
    if live >= 5:
        return (f"\n## Parked Flushes\n\n"
                f"{live} flushes are parked awaiting retry. "
                f"Run `uv run python scripts/drain.py --status` to inspect, "
                f"`--drain` to retry.")
    return ""
```

This surfaces the queue **in the SessionStart context injection** — the operator sees it inline in Claude Code's session preamble, in their own conversation.

**Compile.py wiring (optional).** `compile.py --drain` is added as a convenience alias that invokes `drain.py --drain` first, then runs the normal compile. This is the "I'm starting a fresh compile, drain stale work first" workflow.

**DAILY-LOG order.** Same as C1: parked work that drains later appears in the daily log under the drain time, not the original time. The journal preserves chronology for forensics.

### Pros

- **Hooks gain ONE function call.** Producer side is the smallest possible change. Everything else is opt-in for the operator.
- **Append-only journal is auditable and crash-safe.** A truncated final line is detectable (no closing `}`) and skippable. Replay is idempotent.
- **No locking.** POSIX `O_APPEND` with single-line writes under PIPE_BUF (4KB) is atomic. We enforce <3500 bytes per record to stay safely under the limit.
- **No re-entrancy risk anywhere.** Drainer is invoked by a human (or by a wrapper they wrote — cron, launchd, whatever), never by a hook. PreCompact can fire freely.
- **State.json is untouched.** No new contention on a file that compile.py + batch-flush.py already share.
- **The 32 orphans get adopted on first drain run** — journal-replay sees them as `context_file` paths with no matching `parked` event, and a small migration helper writes a synthetic `parked` event for each.
- **Operator agency.** The operator decides when to spend money attempting retries. After a known bad day (e.g., Anthropic outage), they can `--promote` everything to dead-letter without burning $$$ on doomed retries.
- **Effort is minimal.** ~140 LOC drain.py + ~25 LOC in flush.py + 0 LOC in hooks (or +15 LOC for the passive warning). About 45-60 min.

### Cons

- **Manual-only means the bystander operator never drains.** If the user forgets to look at `parked.jsonl`, items rot forever. The SessionStart passive warning is the only nudge; if they ignore it, nothing happens.
- **No automatic retry of transient errors.** A `Control request timeout: initialize` happens once → the work parks → the operator runs `--drain` 2 hours later → it succeeds. **The work was always going to succeed on retry, but it required human intervention to retry.** This is a deliberate trade-off but it's a real cost.
- **PIPE_BUF atomicity is platform-dependent.** Linux guarantees 4KB; macOS guarantees 512 bytes (POSIX minimum). A 3500-byte record on macOS may interleave with a concurrent append. **Mitigation: a `fcntl.flock` around the append on the write path** — but now we've reintroduced the lock we were avoiding. Honest trade-off: the lock is only on the append path (microseconds), not on the read path (which is replay-from-zero anyway).
- **Replay cost grows over time.** If parked.jsonl reaches 100K lines, every `--status` re-parses the whole file. Mitigation: a `--compact` command that rewrites the journal keeping only `parked` (live), `promoted`, and the last 1000 lines of history.
- **No backoff guidance.** Operator running `--drain` 30 seconds after parking will burn an attempt against a still-active rate-limit window. Mitigation: `--drain --min-age=300` flag that skips items younger than N seconds.
- **Daily-log entries still appear in resolution order.** Same trade-off as C1.

### Effort Estimate

**S-to-M (small-to-medium).** ~165 LOC. The smallest of the three. About 45-60 min of careful implementation; another 20-30 min of pytest fixtures.

### Risk Profile

- **Worst case:** the operator stops looking at the parked queue. After 6 months, `parked.jsonl` has 200 stale records and 200 stale `.md` files in `parked/`. No data is lost — but no data is delivered either. The passive SessionStart warning is the only defense.
- **macOS atomicity:** if PIPE_BUF is the POSIX minimum (512 bytes), a concurrent SessionEnd + PreCompact both writing to the journal could interleave records. The 3500-byte truncation isn't enough on macOS. Real risk if the user runs on a Mac.
- **Journal-replay drift:** if the journal has unexpected event types (e.g., a future version writes a new event we don't understand), the replay logic might misclassify state. Mitigation: ignore unknown event types in replay.
- **Cold start latency:** SessionStart's passive warning re-reads the journal every time. For a 10K-line journal at 200 bytes/line ≈ 2 MB, that's a ~15ms read, fine for the 200ms budget. For a 100K-line journal: ~150ms, marginal. The `--compact` command is the safety belt.

---

## My Recommendation: **Option C1 — "Quiet Park."**

### Why

C1 hits the sweet spot of the three on the dimensions that matter for this codebase:

1. **It already matches the codebase's idiomatic patterns** — files-on-disk under `scripts/`, JSON sidecars, no shared mutable state, atomic renames, detached background subprocesses. The 32 orphans in `scripts/` are literally proto-parked items; C1 just gives them a proper home and a worker. C2 introduces JSON-state locking that has no precedent in the codebase. C3 introduces a journal-replay pattern that also has no precedent.

2. **It automates the recovery loop without requiring operator habits.** C3 is the simplest but assumes the operator will routinely run `--drain`. The user is the kind of person who has 32 orphaned files dating back to **April 14** sitting in `scripts/` because nothing ever swept them up. Manual-only will produce the same outcome: parked work that never drains. C1's "drain 1 on every SessionStart" guarantees forward progress without operator action.

3. **It's the most defensive against worst-case data loss.** C1's dead-letter directory is **explicit, visible in `ls`, and survives state.json corruption.** C2 puts everything in state.json, where a single bad write can lose the queue AND the ingested ledger. C3 is journal-only but if the user never drains, work effectively rots.

4. **The N-attempt budget (fixed 5) is operationally legible.** "After 5 tries we give up" is a sentence the user can hold in their head. C2's exponential schedule is correct but harder to reason about ("when will this next be attempted? let me do the math..."). C3 defers the decision entirely to the operator, who in practice won't make it.

5. **Effort fits the brief's 1-hour budget.** C1 is genuinely about an hour. C2 is over budget once you account for cross-platform locking. C3 is under budget but trades correctness for the operator-discipline assumption.

### What I'd Tweak Before Implementing

- **Bump `MIN_AGE_SECONDS` from 60 to 300.** 60 seconds isn't enough margin for an Anthropic rate-limit window. 5 minutes is the real Retry-After hint frequency.
- **Add `.inflight` lock-rename in `attempt_one()`** before the subprocess spawn, and restore it on subprocess timeout/crash. This handles the rare-but-real concurrent-SessionStart race.
- **Add legacy orphan adoption to `drain.py`'s first-run path** so the 32 existing `session-flush-*.md` files get sidecars and join the queue. Make it idempotent — running migration twice is a no-op.
- **Add a `drain.py --status` and `drain.py --gc --older-than-days=90` from C2/C3.** Even with automation, the operator wants visibility and a way to clean up stale dead-letter items.
- **Set `FLUSH_SOURCE` env in both hooks** before `Popen(flush.py, ...)` so the sidecar's `source` field reflects whether the original failure was from `session-end` or `pre-compact`. Tiny code change, big forensic value.
- **Wire `compile.py --drain` as a convenience alias** (also from C3) so the user can include "drain queue then compile" in their batch workflow.

These tweaks cost another ~30 LOC and ~15 min, well within budget.

### What This Design Does NOT Solve

I'm being honest about this:

1. **It does not solve the in-day daily-log chronology problem.** Drained entries land at drain-time, not flush-time. This is fixable later with a more careful "insert at chronological position" writer; punt for v1.
2. **It does not solve the SDK's untyped-error problem.** It sidesteps it. We don't need to classify errors because we retry everything. A truly-permanent error (e.g., model-name typo in flush.py) will burn 5 attempts before dead-lettering. That's $0 in API cost (the SDK rejects locally) but it's logged 5x.
3. **It does not solve the rate-limit-amplification concern from the problem brief.** A 429 burst that fails 5 attempts at 60s intervals = 5 attempts inside a 5-minute window. Bumping `MIN_AGE_SECONDS` to 300 helps but isn't a guaranteed fix. A sibling architect's text-pattern detection of "rate limit" in `stderr_tail` could compose into this design — if the drainer's `last_error.stderr_tail` contains rate-limit signal, multiply the next-attempt delay. That's a clean orthogonal extension.

### What This Design Does Solve, Concretely

- **2026-04-12 "Control request timeout" cluster:** every failed flush parks; SessionStart drainer retries on next session start; absorbed.
- **2026-05-13 rapid-fire failure burst:** 13 failures park; drainer processes one per SessionStart; spread across the next ~15 sessions; no amplification because attempts are minutes apart by definition.
- **Silent-stderr observability gap:** every park sidecar captures `last_error.stderr_tail`. `ls scripts/parked/*.json` + jq is the new operator workflow. No `/dev/null` discards.
- **32 orphaned `session-flush-*.md` files:** adopted on first drain run, sidecars synthesized, retried automatically, dead-lettered if permanently broken.
- **The `partial_costs` unbounded-growth problem from the patched plan:** still a separate fix (it's a compile.py concern, not a flush concern), but parked compiles get the same treatment — `compile.py --drain` is the operator workflow.

The user said yes to "park-and-resume" with eyes open. This design delivers it with the smallest, most idiomatic possible footprint.
