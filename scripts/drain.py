"""Drainer for parked + orphaned flush context files.

Invoked from hooks/session-start.py as fire-and-forget Popen.

Discovers:
  - scripts/parked/session-flush-*.md   (in-process retry exhaustions)
  - scripts/session-flush-*.md          (legacy SessionEnd orphans)
  - scripts/flush-context-*.md          (PreCompact orphans)

For each:
  1. .inflight rename (atomic claim; concurrent-drainer safe)
  2. Spawn flush.py with FLUSH_FROM_DRAIN=1 set
  3. On exit_code==0: unlink inflight + sidecar (flush.py succeeded)
  4. On exit_code!=0: rename inflight back to .md, bump sidecar.attempts
  5. If attempts >= MAX_ATTEMPTS (5): promote to scripts/dead-letter/

Files older than MAX_AGE_DAYS skip retry entirely and quarantine
directly to dead-letter with a synthetic 'legacy_too_old' sidecar.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
PARKED_DIR = SCRIPTS_DIR / "parked"
DEAD_LETTER_DIR = SCRIPTS_DIR / "dead-letter"
FLUSH_SCRIPT = SCRIPTS_DIR / "flush.py"
LOG_FILE = SCRIPTS_DIR / "drain.log"

# RotatingFileHandler with 5MB cap × 3 backups (see flush.py for rationale).
from logging.handlers import RotatingFileHandler

_handler = RotatingFileHandler(
    filename=str(LOG_FILE),
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s %(levelname)s [drain] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])

MAX_ATTEMPTS = 5
DRAIN_DEFAULT_LIMIT = 1
MAX_AGE_DAYS = 14
MAX_RACE_LOSSES = 3
# Default daily retry-cost cap in USD. ≥2 historical-mean retries
# (mean compile $4.48 per scripts/state.json). Overridable per-day
# via state.json key `daily_retry_cost_cap`.
DEFAULT_DAILY_COST_CAP_USD = 15.0
STATE_FILE = SCRIPTS_DIR / "state.json"


def _today_iso() -> str:
    """Local-tz date in ISO YYYY-MM-DD form (matches existing state.json layout)."""
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


def _load_daily_retry_cost(state_file: Path | None = None) -> tuple[float, float]:
    """Return (spent_today_usd, cap_usd) from state.json.

    cap_usd reads `daily_retry_cost_cap` if present, else
    DEFAULT_DAILY_COST_CAP_USD. spent_today_usd reads
    `daily_retry_cost[<today>]` (defaults to 0.0). Missing state file
    or malformed JSON treated as $0 spent + default cap.

    state_file: if None, looks up module-level STATE_FILE at call time.
    This indirection lets tests patch `drain.STATE_FILE` and have the
    change picked up — Python's default-argument-at-def-time semantics
    would otherwise pin the original module-level path forever.
    """
    if state_file is None:
        state_file = STATE_FILE
    if not state_file.exists():
        return 0.0, DEFAULT_DAILY_COST_CAP_USD
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return 0.0, DEFAULT_DAILY_COST_CAP_USD
    cap = float(data.get("daily_retry_cost_cap", DEFAULT_DAILY_COST_CAP_USD))
    today = _today_iso()
    daily = data.get("daily_retry_cost", {})
    spent = float(daily.get(today, 0.0))
    return spent, cap


def _sidecar_for(md_or_inflight: Path) -> Path:
    """Derive sidecar path from an .md or .md.inflight path, preserving prefix.

    CRITICAL: this is the ONLY place sidecar paths are constructed.
    Hardcoding `session-flush-<id>.json` elsewhere creates a prefix-mismatch
    bug for PreCompact orphans (`flush-context-<id>.md`) — the failure
    branch writes a sidecar at session-flush-<id>.json next to a
    flush-context-<id>.md, and the next read can't find it → attempts
    never increment → infinite retry loop. (Round-2 FATAL-3.)
    """
    name = md_or_inflight.name
    if name.endswith(".md.inflight"):
        name = name[: -len(".md.inflight")]
    elif name.endswith(".md"):
        name = name[: -len(".md")]
    return md_or_inflight.parent / f"{name}.json"


def _read_sidecar(md_path: Path) -> dict:
    """Read the sidecar JSON for md_path. Returns {} if absent/corrupt."""
    sidecar = _sidecar_for(md_path)
    if not sidecar.exists():
        return {}
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_sidecar(md_path: Path, data: dict) -> None:
    """Write sidecar JSON for md_path. CALLER PASSES THE .md OR .inflight
    PATH — NOT the sidecar path itself."""
    sidecar = _sidecar_for(md_path)
    sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _session_id_from_path(md_path: Path) -> str:
    """Extract session ID from `session-flush-<id>.md` or `flush-context-<id>.md`."""
    name = md_path.name
    for prefix in ("session-flush-", "flush-context-"):
        if name.startswith(prefix):
            stem = name[len(prefix):]
            for suffix in (".md.inflight", ".md"):
                if stem.endswith(suffix):
                    return stem[: -len(suffix)]
            return stem
    return md_path.stem


def find_drainable(
    parked_dir: Path = PARKED_DIR,
    scripts_dir: Path = SCRIPTS_DIR,
    dead_letter_dir: Path = DEAD_LETTER_DIR,
    max_age_days: int = MAX_AGE_DAYS,
) -> list[Path]:
    """Discover .md files eligible for draining.

    Returns paths sorted oldest-first (mtime ascending) so we work the
    backlog FIFO. Skips .inflight files (already claimed).

    Side effect: files older than max_age_days are moved DIRECTLY to
    dead-letter without retry. Re-flushing 5-week-old context creates
    stale time-travel daily-log entries; the operator can manually
    triage dead-letter/.
    """
    raw: list[Path] = []
    if parked_dir.exists():
        for p in parked_dir.glob("session-flush-*.md"):
            if not p.name.endswith(".inflight"):
                raw.append(p)
    for p in scripts_dir.glob("session-flush-*.md"):
        if not p.name.endswith(".inflight"):
            raw.append(p)
    for p in scripts_dir.glob("flush-context-*.md"):
        if not p.name.endswith(".inflight"):
            raw.append(p)

    cutoff_ts = time.time() - max_age_days * 86400
    fresh: list[Path] = []
    for p in raw:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff_ts:
            _quarantine_stale(p, mtime, dead_letter_dir, max_age_days)
            continue
        fresh.append(p)

    fresh.sort(key=lambda p: p.stat().st_mtime)
    return fresh


def _quarantine_stale(
    p: Path,
    mtime: float,
    dead_letter_dir: Path,
    max_age_days: int,
) -> None:
    """Move a too-old .md (and its sidecar, if any) to dead-letter.

    Preserves real attempt history from the original sidecar if it
    exists; otherwise writes a synthetic 'legacy_too_old' sidecar.
    (Round-3 M-3 fix: don't orphan the original sidecar in parked/.)
    """
    try:
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        target = dead_letter_dir / p.name

        # Salvage the original sidecar if it exists.
        original_sidecar = _sidecar_for(p)
        had_original = original_sidecar.exists()
        if had_original:
            try:
                os.rename(str(original_sidecar), str(_sidecar_for(target)))
            except OSError as e:
                logging.warning(
                    "quarantine: failed to move sidecar %s: %s",
                    original_sidecar, e,
                )
                try:
                    original_sidecar.unlink()
                except OSError:
                    pass
                had_original = False

        os.rename(str(p), str(target))

        # Synthetic sidecar ONLY if the original wasn't moved.
        if not had_original:
            sidecar = _sidecar_for(target)
            sidecar.write_text(json.dumps({
                "session_id": _session_id_from_path(p),
                "first_attempt_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "attempts": 0,
                "last_rule": "legacy_too_old",
                "last_error": f"file mtime older than {max_age_days}d cutoff; not retried",
            }, indent=2), encoding="utf-8")
        else:
            # Original sidecar moved; annotate WITHOUT clobbering real
            # attempt history. `last_rule` flips to "legacy_too_old" so
            # the operator can filter; the actual prior `last_error` is
            # preserved verbatim. New `quarantine_reason` field carries
            # the why-this-is-in-dead-letter explanation.
            sidecar = _sidecar_for(target)
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            data["prior_last_rule"] = data.get("last_rule")
            data["last_rule"] = "legacy_too_old"
            data["quarantine_reason"] = (
                f"file mtime older than {max_age_days}d cutoff; not retried "
                f"(original attempt history preserved)"
            )
            sidecar.write_text(json.dumps(data, indent=2), encoding="utf-8")

        logging.info(
            "find_drainable: quarantined %s (older than %dd)",
            p.name, max_age_days,
        )
    except OSError as e:
        logging.error("find_drainable: quarantine of %s failed: %s", p, e)


def claim(md_path: Path) -> Path | None:
    """Atomic claim via .inflight rename. Returns the new path, or None
    if another drainer beat us to it."""
    inflight = md_path.with_suffix(md_path.suffix + ".inflight")
    try:
        os.rename(str(md_path), str(inflight))
    except FileNotFoundError:
        return None
    except OSError as e:
        logging.error("claim: %s -> %s failed: %s", md_path, inflight, e)
        return None
    return inflight


def release(inflight: Path) -> Path:
    """Rename .inflight back to .md after a failed attempt."""
    # For `something.md.inflight`, with_suffix("") drops the last
    # `.inflight` suffix, leaving `something.md`. Assert the invariant
    # rather than leave a dead-code branch.
    # NOTE: assert is stripped under `python -O`; uv run uses default
    # optimization, so this is sufficient for an internal invariant.
    md = inflight.with_suffix("")
    assert md.suffix == ".md", f"release: unexpected shape: {inflight}"
    try:
        os.rename(str(inflight), str(md))
    except OSError as e:
        logging.error("release: %s -> %s failed: %s", inflight, md, e)
    return md


def promote_to_dead_letter(
    inflight: Path,
    sidecar: Path,
    dead_letter_dir: Path,
) -> None:
    """Move .inflight (or .md) + sidecar into dead-letter/."""
    dead_letter_dir.mkdir(parents=True, exist_ok=True)
    target_md = dead_letter_dir / inflight.with_suffix("").name
    if not target_md.name.endswith(".md"):
        target_md = dead_letter_dir / (target_md.name + ".md")
    try:
        os.rename(str(inflight), str(target_md))
    except OSError as e:
        logging.error("promote: %s -> %s failed: %s", inflight, target_md, e)
    if sidecar.exists():
        target_sidecar = dead_letter_dir / sidecar.name
        try:
            os.rename(str(sidecar), str(target_sidecar))
        except OSError as e:
            logging.error(
                "promote sidecar: %s -> %s failed: %s",
                sidecar, target_sidecar, e,
            )


def drain_one(
    parked_dir: Path = PARKED_DIR,
    scripts_dir: Path = SCRIPTS_DIR,
    dead_letter_dir: Path = DEAD_LETTER_DIR,
    flush_script: Path = FLUSH_SCRIPT,
    project_root: Path = ROOT,
) -> int:
    """Drain at most one file.

    Returns:
        1  — drained one file (success or failure-with-retry-budget-left)
        0  — no candidates exist
       -1  — candidate(s) existed but claim() lost the race; caller can
             continue to try the next candidate
    """
    candidates = find_drainable(
        parked_dir=parked_dir,
        scripts_dir=scripts_dir,
        dead_letter_dir=dead_letter_dir,
    )
    if not candidates:
        return 0

    md = candidates[0]
    inflight = claim(md)
    if inflight is None:
        # Race lost — caller's loop can try the next candidate.
        return -1

    session_id = _session_id_from_path(md)
    sidecar_data = _read_sidecar(inflight)
    attempts_so_far = int(sidecar_data.get("attempts", 0))

    # Promote BEFORE spawning if we've already exhausted attempts.
    if attempts_so_far >= MAX_ATTEMPTS:
        logging.warning(
            "drain_one: session %s exhausted MAX_ATTEMPTS=%d; promoting",
            session_id, MAX_ATTEMPTS,
        )
        promote_to_dead_letter(
            inflight=inflight,
            sidecar=_sidecar_for(inflight),
            dead_letter_dir=dead_letter_dir,
        )
        return 1

    # Cost-budget circuit breaker (T0.D).
    # If today's accumulated retry cost from state.json's daily_retry_cost
    # dict exceeds the cap (default $15, overridable via daily_retry_cost_cap),
    # SKIP the dispatch — log BUDGET_EXCEEDED, release the inflight back to
    # .md so a future drainer day picks it up, return 0.
    spent_today, cap = _load_daily_retry_cost()
    if spent_today >= cap:
        logging.warning(
            "BUDGET_EXCEEDED: daily_retry_cost[%s] = $%.4f >= cap $%.4f; "
            "skipping drain of session %s, releasing inflight",
            _today_iso(), spent_today, cap, session_id,
        )
        release(inflight)
        return 0

    # Spawn flush.py against the inflight file.
    env = dict(os.environ)
    env["FLUSH_FROM_DRAIN"] = "1"
    # Pass the ORIGINAL mtime so flush.py's daily-log section header can
    # render "(originally HH:MM, drained HH:MM)" — avoids time-travel
    # confusion where today's log claims old work.
    try:
        original_mtime = inflight.stat().st_mtime
        env["FLUSH_ORIGINAL_MTIME"] = str(original_mtime)
    except OSError:
        pass
    # Pass the SECTION name flush.py used originally (if we have it from
    # the sidecar), so the chronology header preserves it. Defaults to
    # "Memory Flush" if absent — the drainer's typical case is replaying
    # a failure, so the original section IS Memory Flush.
    env["FLUSH_ORIGINAL_SECTION"] = sidecar_data.get(
        "original_section", "Memory Flush"
    )
    cmd = [
        "uv", "run", "--directory", str(project_root),
        "python", str(flush_script),
        str(inflight), session_id,
    ]
    logging.info(
        "drain_one: spawning flush.py for session %s (attempt %d/%d)",
        session_id, attempts_so_far + 1, MAX_ATTEMPTS,
    )
    try:
        result = subprocess.run(
            cmd,
            env=env,
            timeout=300,
            check=False,
            capture_output=True,
        )
    except subprocess.TimeoutExpired:
        logging.error("drain_one: session %s timed out at 5min", session_id)
        sidecar_data["attempts"] = attempts_so_far + 1
        sidecar_data["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
        sidecar_data["last_error"] = "drain timeout 300s"
        _write_sidecar(inflight, sidecar_data)
        release(inflight)
        return 1

    if result.returncode == 0:
        # Success — flush.py already unlinked the inflight (via
        # context_file.unlink() at end of main()). Remove sidecar.
        sidecar_file = _sidecar_for(inflight)
        if sidecar_file.exists():
            try:
                sidecar_file.unlink()
            except OSError:
                pass
        if inflight.exists():
            try:
                inflight.unlink()
            except OSError:
                pass
        logging.info("drain_one: session %s succeeded", session_id)
        return 1

    # Failure — bump attempts, release inflight, possibly promote.
    sidecar_data["attempts"] = attempts_so_far + 1
    sidecar_data["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
    sidecar_data["last_error"] = (
        result.stderr.decode("utf-8", errors="replace")[:500]
        if result.stderr else f"exit_code={result.returncode}"
    )
    _write_sidecar(inflight, sidecar_data)

    if sidecar_data["attempts"] >= MAX_ATTEMPTS:
        logging.warning(
            "drain_one: session %s reached MAX_ATTEMPTS=%d; promoting",
            session_id, MAX_ATTEMPTS,
        )
        promote_to_dead_letter(
            inflight=inflight,
            sidecar=_sidecar_for(inflight),
            dead_letter_dir=dead_letter_dir,
        )
    else:
        release(inflight)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=DRAIN_DEFAULT_LIMIT,
                        help="Maximum files to drain in this invocation")
    args = parser.parse_args()

    drained = 0
    race_losses = 0
    iterations_left = args.max
    while iterations_left > 0:
        n = drain_one()
        if n == 1:
            drained += 1
            iterations_left -= 1
        elif n == -1:
            race_losses += 1
            if race_losses >= MAX_RACE_LOSSES:
                logging.info(
                    "drain.py: hit MAX_RACE_LOSSES=%d, exiting",
                    MAX_RACE_LOSSES,
                )
                break
            continue
        else:  # n == 0: no work
            break
    logging.info(
        "drain.py: drained %d file(s) this run (race_losses=%d)",
        drained, race_losses,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
