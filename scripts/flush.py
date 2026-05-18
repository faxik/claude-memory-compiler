"""
Memory flush agent - extracts important knowledge from conversation context.

Spawned by session-end.py or pre-compact.py as a background process. Reads
pre-extracted conversation context from a .md file, uses the Claude Agent SDK
to decide what's worth saving, and appends the result to today's daily log.

Usage:
    uv run python flush.py <context_file.md> <session_id>
"""

from __future__ import annotations

# Recursion prevention: set this BEFORE any imports that might trigger Claude
import os
os.environ["CLAUDE_INVOKED_BY"] = "memory_flush"

import asyncio
import json
import logging
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from config import DAILY_DIR, SCRIPTS_DIR, ROOT_DIR as ROOT  # noqa: E402

STATE_FILE = SCRIPTS_DIR / "last-flush.json"
LOG_FILE = SCRIPTS_DIR / "flush.log"

# Set up file-based logging so we can verify the background process ran.
# The parent process sends stdout/stderr to DEVNULL (to avoid the inherited
# file handle bug on Windows), so this is our only observability channel.
#
# RotatingFileHandler with 5MB cap × 3 backups (so flush.log + .1 + .2 + .3
# = ~20MB ceiling). At ~100B/line, that's ~50K lines/file × 4 files = 200K
# lines retained, with newest entries always in flush.log. Replaces the
# unbounded basicConfig path which had accumulated 28K+ lines pre-rotation.
from logging.handlers import RotatingFileHandler

_handler = RotatingFileHandler(
    filename=str(LOG_FILE),
    maxBytes=5_000_000,
    backupCount=3,
    encoding="utf-8",
)
_handler.setFormatter(
    logging.Formatter(
        fmt="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.basicConfig(level=logging.INFO, handlers=[_handler])


def load_flush_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_flush_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state), encoding="utf-8")


def append_to_daily_log(content: str, section: str = "Session") -> None:
    """Append content to today's daily log.

    Content-hash dedup gate: if this exact content body has been appended
    in the last 24h, skip silently and log "DUP_SKIP". Solves
    duplicate-on-retry-replay for the common "client crashed mid-write"
    case. (LLM non-determinism on regenerated content can defeat this
    gate; input-hash variant is in followups.)

    When invoked from the drainer (FLUSH_ORIGINAL_MTIME env set), the
    section header carries the original-session timestamp + the drain
    timestamp so the daily log doesn't pretend old work is fresh.
    """
    from dedup import record_append, should_append

    today = datetime.now(timezone.utc).astimezone()
    log_path = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    ledger_path = SCRIPTS_DIR / "appended_hashes.json"

    # Dedup gate. Hash the content body only; the header (with timestamps)
    # would defeat dedup since timestamps differ on retries.
    if not should_append(content, ledger_path):
        logging.info(
            "DUP_SKIP: content hash already appended in last 24h (section=%s)",
            section,
        )
        return

    if not log_path.exists():
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"# Daily Log: {today.strftime('%Y-%m-%d')}\n\n## Sessions\n\n## Memory Maintenance\n\n",
            encoding="utf-8",
        )

    time_str = today.strftime("%H:%M")

    # Chronology header: when drainer-driven, mark the original session
    # time alongside the drain time. Catches ValueError, OSError,
    # OverflowError (datetime.fromtimestamp(inf) raises OverflowError),
    # and TypeError (None propagation).
    original_mtime_env = os.environ.get("FLUSH_ORIGINAL_MTIME")
    if original_mtime_env:
        try:
            orig_dt = datetime.fromtimestamp(
                float(original_mtime_env), timezone.utc
            ).astimezone()
            header = (
                f"### {section} "
                f"(originally {orig_dt.strftime('%H:%M %Y-%m-%d')}, "
                f"drained {time_str})"
            )
        except (ValueError, OSError, OverflowError, TypeError):
            header = f"### {section} ({time_str})"
    else:
        header = f"### {section} ({time_str})"

    entry = f"{header}\n\n{content}\n\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)

    record_append(content, ledger_path)


def _build_flush_error_response(
    exc: BaseException,
    stderr_tail: "deque[str] | None" = None,
    *,
    attempts: int = 1,
) -> str:
    """Build a diagnostic-rich FLUSH_ERROR line for the daily log.

    Captured signals (all best-effort — missing fields are omitted, not
    rendered as 'None'):
      - exc class name (e.g., ProcessError, Exception)
      - first 300 chars of str(exc) ("message:")
      - exc.exit_code if a ProcessError attribute is present
      - exc.stderr if present (SDK hardcodes this to the boilerplate
        string "Check stderr output for details"; we record it only as
        a forensic crumb. Slice 2's classifier deliberately ignores
        this attribute — see scripts/classifier.py and
        tools/lint_classifier.py.)
      - stderr_tail (last 50 lines from the options.stderr callback —
        the ONLY source of real bundled-CLI stderr text)
      - attempts field (real attempt count from run_flush's retry loop)

    Format is line-oriented + greppable. The first line still starts with
    'FLUSH_ERROR:' so existing dispatch logic at the bottom of main() still
    works.
    """
    parts: list[str] = []
    parts.append(f"FLUSH_ERROR: {type(exc).__name__}")

    exit_code = getattr(exc, "exit_code", None)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    parts.append(f"attempts={attempts}")

    header = " | ".join(parts)
    body: list[str] = [header]

    msg = str(exc)[:300]
    if msg.strip():
        body.append(f"  message: {msg}")

    sdk_stderr = getattr(exc, "stderr", None)
    if sdk_stderr:
        # Known boilerplate from claude_agent_sdk subprocess_cli is
        # "Check stderr output for details" — record anyway, harmless.
        body.append(f"  sdk_stderr: {str(sdk_stderr)[:200]}")

    if stderr_tail:
        body.append("  stderr_tail:")
        for line in stderr_tail:
            body.append(f"    {line}")

    return "\n".join(body)


class ParkRequested(Exception):
    """Raised by run_flush when in-process retry exhausted on a transient
    pattern, signalling that main() should park the context file for the
    drainer rather than write FLUSH_ERROR.

    Why an exception instead of a string sentinel: an LLM could
    legitimately emit "PARK_REQUESTED" as the first token of a response
    (especially in sessions that discuss this codebase), causing a
    false-positive park + silent data loss. Exception-based signalling
    avoids the collision.
    """

    def __init__(self, flush_error_body: str, last_rule: str = "unknown"):
        super().__init__(flush_error_body)
        self.flush_error_body = flush_error_body
        self.last_rule = last_rule


def park_context_file(
    context_file: Path,
    session_id: str,
    flush_error_response: str,
    last_rule: str = "unknown",
) -> Path:
    """Move a failed context file to scripts/parked/ with a sidecar.

    The sidecar JSON tracks attempt count + first/last-attempt timestamps
    so the drainer can promote to dead-letter after N tries.

    Returns the parked path (the .md file's new location).
    """
    parked_dir = SCRIPTS_DIR / "parked"
    parked_dir.mkdir(parents=True, exist_ok=True)

    parked_md = parked_dir / f"session-flush-{session_id}.md"
    parked_sidecar = parked_dir / f"session-flush-{session_id}.json"

    # Move the context file (atomic on same-filesystem). On EXDEV
    # (cross-filesystem) or other rename failure, we return the original
    # path and the file remains at its source location — the drainer's
    # legacy-orphan glob (session-flush-*.md in scripts/) will pick it
    # up on the next SessionStart, so it's not silently lost.
    try:
        os.replace(str(context_file), str(parked_md))
    except OSError as e:
        logging.warning(
            "park_context_file: rename %s -> %s failed: %s. File remains at "
            "original location; drainer will adopt via legacy-orphan glob.",
            context_file, parked_md, e,
        )
        return context_file

    # Read existing sidecar if drainer is re-parking; otherwise create.
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()
    sidecar_data: dict = {
        "session_id": session_id,
        "first_attempt_at": now_iso,
        "last_attempt_at": now_iso,
        "attempts": 1,
        "last_rule": last_rule,
        "last_error": flush_error_response[:1000],
    }
    if parked_sidecar.exists():
        try:
            existing = json.loads(parked_sidecar.read_text(encoding="utf-8"))
            sidecar_data["first_attempt_at"] = existing.get(
                "first_attempt_at", now_iso
            )
            sidecar_data["attempts"] = existing.get("attempts", 0) + 1
        except (json.JSONDecodeError, OSError):
            pass

    parked_sidecar.write_text(
        json.dumps(sidecar_data, indent=2), encoding="utf-8"
    )
    return parked_md


async def run_flush(context: str) -> str:
    """Use Claude Agent SDK to extract knowledge from conversation context.

    Wraps the SDK query() in an in-process retry loop driven by classify().
    - On verdict="retry" (transient): sleep 2s/4s; retry up to 2 times.
    - On rate_limit_signal rule_name: sleep 60s once then retry.
    - On verdict="fail_fast" (auth/400/local-IO): skip retry, return
      FLUSH_ERROR immediately.

    Returns either the LLM extraction text, "FLUSH_OK", or a
    FLUSH_ERROR-prefixed diagnostic on fail_fast.

    Raises ParkRequested when in-process budget is exhausted on a
    transient pattern — caller (main()) parks the context file for
    the drainer to retry. Exception-based signal (not a string sentinel)
    avoids the LLM-output-collision footgun.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )
    from classifier import classify

    prompt = f"""Review the conversation context below and respond with a concise summary
of important items that should be preserved in the daily log.
Do NOT use any tools — just return plain text.

Format your response as a structured daily log entry with these sections:

**Context:** [One line about what the user was working on]

**Key Exchanges:**
- [Important Q&A or discussions]

**Decisions Made:**
- [Any decisions with rationale]

**Lessons Learned:**
- [Gotchas, patterns, or insights discovered]

**Action Items:**
- [Follow-ups or TODOs mentioned]

Skip anything that is:
- Routine tool calls or file reads
- Content that's trivial or obvious
- Trivial back-and-forth or clarification exchanges

Only include sections that have actual content. If nothing is worth saving,
respond with exactly: FLUSH_OK

## Conversation Context

{context}"""

    # Bounded stderr capture for FLUSH_ERROR diagnostics + rate-limit
    # signal detection. The bundled CLI's real stderr arrives via this
    # callback (NOT via exc.stderr — hardcoded boilerplate). Keep last
    # 50 lines; enough to catch rate-limit text without bloating logs.
    stderr_tail: deque[str] = deque(maxlen=50)

    def _log_stderr(line: str) -> None:
        stripped = line.rstrip()
        logging.error("[bundled CLI stderr] %s", stripped)
        stderr_tail.append(stripped)

    async def _single_attempt() -> str:
        collected = ""
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT),
                allowed_tools=[],
                max_turns=2,
                model="sonnet",
                fallback_model="haiku",
                stderr=_log_stderr,
                extra_args={
                    "strict-mcp-config": None,
                    "disable-slash-commands": None,
                    "setting-sources": "user",
                },
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        collected += block.text
            elif isinstance(message, ResultMessage):
                pass
        return collected

    # In-process retry budget. attempts 1..MAX_ATTEMPTS.
    MAX_ATTEMPTS = 3  # 1 initial + 2 retries
    BACKOFF_SCHEDULE_S = (2.0, 4.0)  # waits BEFORE attempts 2 and 3
    RATE_LIMIT_SLEEP_S = 60.0  # one-shot longer wait for rate_limit verdict
    last_exception: BaseException | None = None
    last_verdict_rule: str = "no_attempt"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            result = await _single_attempt()
            return result  # success
        except Exception as e:
            last_exception = e
            verdict = classify(e, stderr_tail=list(stderr_tail))
            last_verdict_rule = verdict.rule_name
            logging.warning(
                "run_flush attempt %d/%d failed: %s (verdict=%s, rule=%s)",
                attempt, MAX_ATTEMPTS, type(e).__name__,
                verdict.kind, verdict.rule_name,
            )

            if verdict.kind == "fail_fast":
                import traceback
                logging.error(
                    "Agent SDK error (fail_fast): %s\n%s",
                    e, traceback.format_exc(),
                )
                return _build_flush_error_response(e, stderr_tail, attempts=attempt)

            if attempt == MAX_ATTEMPTS:
                logging.warning(
                    "run_flush in-process budget exhausted after %d attempts "
                    "(last rule: %s); raising ParkRequested",
                    attempt, verdict.rule_name,
                )
                body = _build_flush_error_response(e, stderr_tail, attempts=attempt)
                raise ParkRequested(body, last_rule=last_verdict_rule) from e

            # Verdict is retry; we have budget left.
            if verdict.rule_name == "rate_limit_signal":
                sleep_for = RATE_LIMIT_SLEEP_S
            else:
                sleep_for = BACKOFF_SCHEDULE_S[attempt - 1]
            logging.info(
                "run_flush sleeping %.1fs before attempt %d",
                sleep_for, attempt + 1,
            )
            await asyncio.sleep(sleep_for)

    # Defensive fallback — loop should always return or raise.
    assert last_exception is not None
    return _build_flush_error_response(last_exception, stderr_tail, attempts=MAX_ATTEMPTS)


COMPILE_AFTER_HOUR = 18  # 6 PM local time


def maybe_trigger_compilation() -> None:
    """If it's past the compile hour and today's log hasn't been compiled, run compile.py."""
    import subprocess as _sp

    now = datetime.now(timezone.utc).astimezone()
    if now.hour < COMPILE_AFTER_HOUR:
        return

    # Check if today's log has already been compiled
    today_log = f"{now.strftime('%Y-%m-%d')}.md"
    compile_state_file = SCRIPTS_DIR / "state.json"
    if compile_state_file.exists():
        try:
            compile_state = json.loads(compile_state_file.read_text(encoding="utf-8"))
            ingested = compile_state.get("ingested", {})
            if today_log in ingested:
                # Already compiled today - check if the log has changed since
                from hashlib import sha256
                log_path = DAILY_DIR / today_log
                if log_path.exists():
                    current_hash = sha256(log_path.read_bytes()).hexdigest()[:16]
                    if ingested[today_log].get("hash") == current_hash:
                        return  # log unchanged since last compile
        except (json.JSONDecodeError, OSError):
            pass

    compile_script = SCRIPTS_DIR / "compile.py"
    if not compile_script.exists():
        return

    logging.info("End-of-day compilation triggered (after %d:00)", COMPILE_AFTER_HOUR)

    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(compile_script)]

    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = _sp.CREATE_NEW_PROCESS_GROUP | _sp.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True

    try:
        log_handle = open(str(SCRIPTS_DIR / "compile.log"), "a")
        _sp.Popen(cmd, stdout=log_handle, stderr=_sp.STDOUT, cwd=str(ROOT), **kwargs)
    except Exception as e:
        logging.error("Failed to spawn compile.py: %s", e)


def main():
    if len(sys.argv) < 3:
        logging.error("Usage: %s <context_file.md> <session_id>", sys.argv[0])
        sys.exit(1)

    context_file = Path(sys.argv[1])
    session_id = sys.argv[2]

    logging.info("flush.py started for session %s, context: %s", session_id, context_file)

    if not context_file.exists():
        logging.error("Context file not found: %s", context_file)
        return

    # Deduplication: skip if same session was flushed within 60 seconds.
    # BUT: drainer-spawned attempts MUST bypass this gate — otherwise the
    # drainer's respawn within 60s of a recent SessionEnd would unlink the
    # inflight context file and exit 0, making the drainer architecturally
    # broken. (Round-2 adversarial-review SERIOUS-6 → FATAL-tier fix.)
    if not os.environ.get("FLUSH_FROM_DRAIN"):
        state = load_flush_state()
        if (
            state.get("session_id") == session_id
            and time.time() - state.get("timestamp", 0) < 60
        ):
            logging.info("Skipping duplicate flush for session %s", session_id)
            context_file.unlink(missing_ok=True)
            return

    # Read pre-extracted context
    context = context_file.read_text(encoding="utf-8").strip()
    if not context:
        logging.info("Context file is empty, skipping")
        context_file.unlink(missing_ok=True)
        return

    logging.info("Flushing session %s: %d chars", session_id, len(context))

    # Run the LLM extraction (with in-process retry loop). On exhaustion,
    # run_flush raises ParkRequested — caught below.
    try:
        response = asyncio.run(run_flush(context))
    except ParkRequested as park:
        if os.environ.get("FLUSH_FROM_DRAIN"):
            # Drainer is driving this attempt; do NOT re-park. Write
            # FLUSH_ERROR to the daily log + exit 1 so drainer increments
            # the attempt counter via its failure branch.
            logging.warning(
                "FLUSH_FROM_DRAIN set; not re-parking (rule=%s). "
                "Writing FLUSH_ERROR to daily log.",
                park.last_rule,
            )
            append_to_daily_log(park.flush_error_body, "Memory Flush")
            save_flush_state({"session_id": session_id, "timestamp": time.time()})
            # Do NOT unlink the context file — drainer needs it to remain
            # to know retry is warranted via its own logic. Exit 1.
            logging.info("Flush exhausted (drain path) for session %s", session_id)
            sys.exit(1)

        # Normal path: in-process retry exhausted; park for the drainer.
        parked_path = park_context_file(
            context_file=context_file,
            session_id=session_id,
            flush_error_response=park.flush_error_body,
            last_rule=park.last_rule,
        )
        logging.warning(
            "PARKED context file -> %s (rule=%s)",
            parked_path, park.last_rule,
        )
        save_flush_state({"session_id": session_id, "timestamp": time.time()})
        # Do NOT write FLUSH_ERROR to the daily log on park — drainer's
        # eventual retry either succeeds (real content lands) or hits
        # dead-letter (operator inspects sidecar).
        logging.info(
            "Flush parked for session %s; drainer will retry",
            session_id,
        )
        return

    # Append to daily log (non-park path)
    if "FLUSH_OK" in response:
        logging.info("Result: FLUSH_OK")
        append_to_daily_log(
            "FLUSH_OK - Nothing worth saving from this session", "Memory Flush"
        )
    elif "FLUSH_ERROR" in response:
        # fail_fast verdict reached without park.
        logging.error("Result: %s", response)
        append_to_daily_log(response, "Memory Flush")
    else:
        logging.info("Result: saved to daily log (%d chars)", len(response))
        append_to_daily_log(response, "Session")

    # Update dedup state
    save_flush_state({"session_id": session_id, "timestamp": time.time()})

    # Clean up context file (success path only — parked files were moved)
    context_file.unlink(missing_ok=True)

    # End-of-day auto-compilation: if it's past the compile hour and today's
    # log hasn't been compiled yet, trigger compile.py in the background.
    maybe_trigger_compilation()

    logging.info("Flush complete for session %s", session_id)


if __name__ == "__main__":
    main()
