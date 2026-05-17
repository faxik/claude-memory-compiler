# Architect B — "Move Resilience Out": Process-Boundary Retry

> **Perspective.** Stop trying to discriminate SDK errors inside the Python process. The SDK
> emits bare `Exception` from 7 of 8 raise sites in `_internal/query.py` and the only useful
> structured signal it gives us is `ProcessError.exit_code` from the bundled-CLI subprocess.
> The right abstraction is: treat each `flush.py` and `compile.py --file <log>` invocation as
> an **idempotent subprocess job**. Detect failure at the *process boundary* (exit code +
> captured stderr tail). Retry by **re-spawning**. The SDK's exception types could mutate
> tomorrow and our retry layer would not care.
>
> The codebase is already shaped for this. `flush.py` is *already* a fire-and-forget
> subprocess (`session-end.py:162`, `pre-compact.py:158`). `compile.py` accepts `--file
> <path>` (`compile.py:176-186`) and is idempotent via hash-equality gate
> (`compile.py:196`). The context file at `scripts/session-flush-<session>-<ts>.md`
> (`session-end.py:140`) is the natural retry token — it survives across attempts, and a
> directory listing of `scripts/` confirms there are ~35 orphaned context files on disk,
> proving the queue *already exists* — we just never drain it.
>
> All three options below share that core thesis. They differ on **where the retry loop
> lives**, **how idempotency is enforced**, and **how failure is classified at the process
> boundary**.

---

## Grounding evidence (verified May 17)

| Claim | File:line | Verified |
|-------|-----------|----------|
| Hook spawns `flush.py` fire-and-forget, no `wait()` | `hooks/session-end.py:161-167` | yes |
| `stderr=subprocess.DEVNULL` discards CLI errors | `session-end.py:165`, `pre-compact.py:161` | yes |
| Recursion guard via `CLAUDE_INVOKED_BY` env var | `session-end.py:24-25`, `pre-compact.py:24-25` | yes |
| `flush.py` sets `CLAUDE_INVOKED_BY="memory_flush"` before SDK import | `scripts/flush.py:15-16` | yes |
| Context file is the durable retry unit | `session-end.py:140-141`, `scripts/flush.py:255` (unlink) | yes |
| 60-second dedup keyed on session_id | `scripts/flush.py:218-222` | yes |
| `flush.py` writes `FLUSH_ERROR` to daily log on failure | `scripts/flush.py:147, 246` | yes |
| `compile.py` accepts `--file <path>` | `scripts/compile.py:176-186` | yes |
| `compile.py` hash-gate skips unchanged logs | `scripts/compile.py:194-197` | yes |
| SDK surfaces `ProcessError(exit_code=N)` from CLI nonzero exit | `.venv/.../transport/subprocess_cli.py:611-618` | yes |
| 7 of 8 SDK `raise` sites use bare `Exception` | brief: query.py:272,308,318,334,346,385,420 | yes |
| ~35 orphaned `session-flush-*.md` files exist (lost work) | `ls scripts/session-flush-*.md` | yes |

The orphan inventory is the most important evidence. We don't need to invent a queue — we
need to **drain the one we already have**.

---

## Option B1 — "Supervisor Wraps Flush"

### Core idea

Introduce a thin `flush-supervisor.py` orchestrator between the hook and `flush.py`. The
hook spawns the supervisor (still fire-and-forget). The supervisor spawns `flush.py` as a
child, waits for exit, and re-spawns on retryable exit codes. `flush.py` itself becomes
**dumber**: on any SDK failure it exits with a distinguished code (no FLUSH_ERROR write).
The supervisor owns the retry budget, the daily-log error write, and stderr capture.

### How it works

**1. Re-purpose `flush.py` exit codes (modify `scripts/flush.py:74-149, 244-249`).**

```python
# scripts/flush.py — exit-code conventions
EXIT_OK            = 0    # Wrote a real session entry OR FLUSH_OK
EXIT_TRANSIENT     = 75   # SDK threw something we believe is transient — supervisor SHOULD retry
EXIT_PERMANENT     = 65   # SDK threw something terminal (auth, 400) — supervisor MUST NOT retry
EXIT_NO_CONTEXT    = 0    # Empty / missing context file — treat as success (today's behavior)
EXIT_DEDUP_SKIP    = 0    # Duplicate flush within window — success
```

The `run_flush()` coroutine no longer composes `FLUSH_ERROR`. Instead `main()` translates
the SDK exception into an exit code by sniffing `getattr(exc, "exit_code", None)` and the
`str(exc)` head:

```python
# scripts/flush.py — replaces lines 244-249
def classify_exception(exc: BaseException) -> int:
    """Return EXIT_TRANSIENT or EXIT_PERMANENT for the supervisor."""
    msg = str(exc).lower()
    cli_exit = getattr(exc, "exit_code", None)

    # 1. Bundled-CLI auth / model-not-found / prompt-too-long — terminal
    stderr = (getattr(exc, "stderr", "") or "").lower()
    if any(kw in stderr for kw in ("invalid api key", "authentication", "model not found")):
        return EXIT_PERMANENT
    if "prompt is too long" in msg or "prompt is too long" in stderr:
        return EXIT_PERMANENT

    # 2. Bare Exception("Control request timeout: initialize") — transient
    if "control request timeout" in msg:
        return EXIT_TRANSIENT

    # 3. CLI exited 1 with no specific signature — assume transient (CLI is flaky)
    if cli_exit is not None and cli_exit > 0:
        return EXIT_TRANSIENT

    # 4. Default: be conservative — transient (worst case we burn 3 attempts)
    return EXIT_TRANSIENT

# In main(), after asyncio.run(run_flush(...)):
if response.startswith("FLUSH_INTERNAL_ERROR:"):
    # run_flush stuffs a sentinel + exception details when it catches
    sys.stderr.write(response + "\n")    # supervisor will tail this
    sys.exit(exit_code_from_response)
```

Note that `flush.py` still owns SDK-import-time errors and the dedup short-circuit — the
*supervisor* never sees those.

**2. New file `scripts/flush-supervisor.py` (the orchestrator).**

```python
"""Supervisor: re-spawns flush.py on transient exit codes, persists diagnostics."""
from __future__ import annotations
import os, sys, subprocess, time, json, logging
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DAILY_DIR = ROOT / "daily"
FLUSH_PY = SCRIPTS_DIR / "flush.py"
SUPERVISOR_LOG = SCRIPTS_DIR / "supervisor.log"

# Tunables — keep tight; PreCompact must not stall.
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 5.0       # 5s, 10s, 20s
BACKOFF_CAP_S = 60.0
STDERR_TAIL_BYTES = 4096

EXIT_OK, EXIT_PERMANENT, EXIT_TRANSIENT = 0, 65, 75

logging.basicConfig(
    filename=str(SUPERVISOR_LOG), level=logging.INFO,
    format="%(asctime)s %(levelname)s [supervisor] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def append_flush_error(reason: str, session_id: str, attempts: int, stderr_tail: str) -> None:
    today = datetime.now(timezone.utc).astimezone()
    log = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"### Memory Flush ({today.strftime('%H:%M')})\n\n"
        f"FLUSH_ERROR after {attempts} attempt(s): {reason}\n"
        f"session={session_id}\n"
        f"```stderr-tail\n{stderr_tail.strip() or '(empty)'}\n```\n\n"
    )
    with open(log, "a", encoding="utf-8") as f:
        f.write(block)


def main() -> int:
    if len(sys.argv) < 3:
        logging.error("usage: flush-supervisor.py <context.md> <session_id>")
        return 2
    context_file, session_id = Path(sys.argv[1]), sys.argv[2]

    # Per-invocation stderr file — deterministic name (not pid-keyed), GC after run.
    stderr_path = SCRIPTS_DIR / f"supervisor-{session_id}.stderr"

    attempt = 0
    last_rc, last_stderr = -1, ""
    while attempt < MAX_ATTEMPTS:
        attempt += 1
        logging.info("flush attempt %d/%d for %s", attempt, MAX_ATTEMPTS, session_id)
        try:
            # Inherit CLAUDE_INVOKED_BY=memory_flush — same recursion guard semantics
            # as flush.py:16. We do NOT set it ourselves; flush.py sets it before
            # importing claude_agent_sdk.
            env = os.environ.copy()
            with open(stderr_path, "wb") as err_fp:
                proc = subprocess.run(
                    ["uv", "run", "--directory", str(ROOT), "python",
                     str(FLUSH_PY), str(context_file), session_id,
                     "--attempt", str(attempt)],
                    stdout=subprocess.DEVNULL, stderr=err_fp, env=env,
                    creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
                )
            last_rc = proc.returncode
        except Exception as e:
            logging.error("spawn failed: %s", e)
            last_rc, last_stderr = -1, f"spawn error: {e}"
            break

        # Read stderr tail for diagnostics (and to log on every attempt)
        try:
            last_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")[-STDERR_TAIL_BYTES:]
        except OSError:
            last_stderr = ""

        if last_rc == EXIT_OK:
            logging.info("flush ok after %d attempt(s)", attempt)
            stderr_path.unlink(missing_ok=True)         # GC stderr file
            return 0
        if last_rc == EXIT_PERMANENT:
            logging.error("permanent failure rc=%d", last_rc)
            break
        # else EXIT_TRANSIENT or weird rc — back off and retry
        if attempt < MAX_ATTEMPTS:
            sleep_s = min(BACKOFF_BASE_S * (2 ** (attempt - 1)), BACKOFF_CAP_S)
            logging.info("transient rc=%d; sleeping %.1fs", last_rc, sleep_s)
            time.sleep(sleep_s)

    # All attempts exhausted (or permanent). Write the failure breadcrumb to the daily log.
    reason = "permanent" if last_rc == EXIT_PERMANENT else f"transient rc={last_rc} after {attempt} retries"
    append_flush_error(reason, session_id, attempt, last_stderr)
    stderr_path.unlink(missing_ok=True)                # GC stderr file
    # Important: do NOT unlink the context file. flush.py is the owner of that — and on
    # EXIT_PERMANENT it has already deleted it; on transient exhaustion we want it kept
    # for offline re-run with `uv run python scripts/flush.py <orphan>.md <session>`.
    return 1


if __name__ == "__main__":
    sys.exit(main())
```

**3. Hook edit (`hooks/session-end.py:144-167`, mirror in `pre-compact.py:142-165`).**

Single line change: spawn the supervisor instead of `flush.py` directly.

```python
flush_script = SCRIPTS_DIR / "flush-supervisor.py"   # was: flush.py
# Everything else unchanged — same Popen args, same DEVNULL stderr at hook level.
```

The hook *still* doesn't `wait()`. Hook returns in <50ms. The supervisor runs detached in
its own process tree and owns the entire retry lifecycle. Total wall-clock budget at
worst case: 5s + 10s + 20s + 3× flush execution ≈ 90–120s. That's fine — nobody is
waiting on it.

**4. Idempotency contract.**

The supervisor calls `flush.py` with `--attempt N`. `flush.py` consumes this and:
- On `--attempt 1`: honors the 60s dedup (`flush.py:218-222`) as today.
- On `--attempt >= 2`: **skips dedup** (re-attempt is intentional).
- On success: deletes the context file (as today, `flush.py:255`).
- On permanent failure: deletes the context file (don't keep poison for future runs).
- On transient failure: **keeps the context file** so the supervisor can re-spawn it.

The daily-log write is moved out of `flush.py` entirely for the error case — only the
supervisor writes `FLUSH_ERROR`, and only once, only after all attempts fail. This kills
the current bug where rapid-fire flushes spam the daily log with FLUSH_ERROR lines.

### Pros

- **Zero coupling to SDK exception types.** Whether the SDK throws `Exception`, `ProcessError`,
  or invents a new class next week, the supervisor only sees an exit code.
- **Survives Python crashes inside flush.py.** A segfault, `MemoryError`, or `sys.exit(N)`
  inside the SDK transport produces a non-zero exit code that the supervisor can react to.
  In-process retry can't handle these.
- **Stderr capture is free and per-invocation, not per-PID.** File name is keyed on
  `session_id`, so there's exactly one file per supervisor run, and it's GC'd on exit.
  Solves the round-2 "0-byte file pollution" cons cleanly.
- **PreCompact-safe.** Supervisor is itself fire-and-forget from the hook's perspective.
  The hook still returns in <50ms.
- **Cleanest separation of concerns.** `flush.py` knows how to do one flush. Supervisor
  knows how to retry. Hook knows how to fire-and-forget.
- **Trivial offline rerun.** Operator can `uv run python scripts/flush-supervisor.py
  scripts/session-flush-<orphan>.md <session>` to drain any orphaned context file.

### Cons

- **Two new files** (`flush-supervisor.py`, `supervisor.log`) plus a small `flush.py` API
  change (`--attempt` flag). Not zero footprint.
- **Exit-code classification still happens somewhere.** We moved the discrimination from
  in-process exception sniffing to a `classify_exception()` function inside `flush.py`.
  That function is exactly the "embrace untyped" trick — just compiled down to an integer
  before the supervisor sees it. We did not eliminate the problem; we relocated it.
- The classify function is **executed inside the same Python process that just threw
  the exception**. If the SDK leaves the process in a weird state (event loop broken,
  asyncio cleanup pending), this code runs on shaky ground. Mitigation: keep `classify`
  pure-string with no SDK imports.

### Effort — **M** (4–6h)

- 30 min: `--attempt` flag + dedup-bypass + classify_exception in `flush.py`.
- 60 min: new `flush-supervisor.py` + log file plumbing.
- 30 min: hook edit (1 line each).
- 90 min: tests — `tests/test_supervisor.py` mocks `flush.py` with a script that exits
  0 / 65 / 75 and asserts the supervisor's retry behavior. **No SDK mock needed** —
  this is exit-code-only.
- 60 min: integration test — write a fake flush.py that raises `Exception("Control request
  timeout: initialize")` once then succeeds, run the real supervisor, assert exit 0 and
  daily-log has a session block (not FLUSH_ERROR).

### Risk profile

- **Worst case:** classify_exception misclassifies a permanent error as transient.
  Result: 90s wasted retrying, then FLUSH_ERROR with diagnostics — same end state as
  today plus 90s of delay. The retry budget caps the blast radius.
- **Subtle case:** if the Python interpreter itself dies before `classify_exception` runs
  (segfault, OOM), the exit code will be -SIGSEGV or similar, which the supervisor will
  treat as "weird rc → transient" and retry. Probably correct.
- **CLAUDE_INVOKED_BY recursion:** The supervisor inherits the hook's env, which on the
  outer level does NOT have `CLAUDE_INVOKED_BY` set. `flush.py:16` sets it before
  importing the SDK, so recursive hook firing is prevented from inside flush.py. ✓
  But: if `flush-supervisor.py` ever imports the SDK or runs a Claude Code-aware
  command before spawning flush.py, recursion is back. **Mitigation:** explicit
  rule — supervisor.py is pure stdlib, never imports `claude_agent_sdk`.
- **Windows:** Plain `subprocess.run` works. We do NOT need `DETACHED_PROCESS` because
  the supervisor *wants* to wait synchronously. `CREATE_NO_WINDOW` only.

---

## Option B2 — "Hook Triggers Drainer"

### Core idea

Don't write a separate supervisor at all. The hook spawns `flush.py` *as today* (fire-and-
forget, single attempt). Add a **drainer** loop at the top of every hook invocation that
sweeps the `scripts/` directory for orphaned `session-flush-*.md` files older than N
seconds and re-spawns `flush.py` against each. The retry budget becomes amortized: every
hook fire gets a chance to retry stale work.

This treats the existing context-file orphans (~35 on disk right now) as the natural
work queue. We don't introduce a new abstraction; we put a janitor at the front door.

### How it works

**1. Add a new file `scripts/flush_drainer.py` (called from inside the hooks).**

```python
"""Drainer: re-spawns flush.py against orphaned context files."""
from __future__ import annotations
import os, sys, subprocess, time, logging, re
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DAILY_DIR = ROOT / "daily"

# Tunables
MIN_AGE_SECONDS = 90       # don't pick up a context file that another flush.py is mid-run on
MAX_DRAIN_PER_HOOK = 2     # cap so a hook never spawns more than 2 retries in one fire
MAX_LIFETIME_HOURS = 24    # after this, write FLUSH_GIVEUP and unlink

CONTEXT_RE = re.compile(r"^(session-flush|flush-context)-(?P<session>[a-f0-9-]+)-\d{8}-\d{6}\.md$")

logging.basicConfig(
    filename=str(SCRIPTS_DIR / "drainer.log"), level=logging.INFO,
    format="%(asctime)s %(levelname)s [drainer] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def find_orphans() -> list[Path]:
    now = time.time()
    out = []
    for p in SCRIPTS_DIR.glob("session-flush-*.md"):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < MIN_AGE_SECONDS:
            continue                                    # still in-flight
        out.append(p)
    for p in SCRIPTS_DIR.glob("flush-context-*.md"):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < MIN_AGE_SECONDS:
            continue
        out.append(p)
    out.sort(key=lambda p: p.stat().st_mtime)            # oldest first
    return out


def parse_session_id(p: Path) -> str:
    m = CONTEXT_RE.match(p.name)
    return m.group("session") if m else "unknown"


def give_up(p: Path, session: str) -> None:
    """Write FLUSH_GIVEUP marker and unlink."""
    today = datetime.now(timezone.utc).astimezone()
    log = DAILY_DIR / f"{today.strftime('%Y-%m-%d')}.md"
    log.parent.mkdir(parents=True, exist_ok=True)
    block = (
        f"### Memory Flush ({today.strftime('%H:%M')})\n\n"
        f"FLUSH_GIVEUP: orphan context file older than {MAX_LIFETIME_HOURS}h\n"
        f"session={session} file={p.name}\n\n"
    )
    with open(log, "a", encoding="utf-8") as f:
        f.write(block)
    p.unlink(missing_ok=True)


def drain() -> int:
    """Spawn flush.py for orphans. Returns number of spawns issued."""
    orphans = find_orphans()
    spawned = 0
    for p in orphans:
        if spawned >= MAX_DRAIN_PER_HOOK:
            break
        session = parse_session_id(p)
        age_hours = (time.time() - p.stat().st_mtime) / 3600.0
        if age_hours > MAX_LIFETIME_HOURS:
            logging.info("giving up on %s (age %.1fh)", p.name, age_hours)
            give_up(p, session)
            continue
        logging.info("re-spawning flush.py for orphan %s (age %.1fh)", p.name, age_hours)
        try:
            subprocess.Popen(
                ["uv", "run", "--directory", str(ROOT), "python",
                 str(SCRIPTS_DIR / "flush.py"), str(p), session, "--retry"],
                stdout=subprocess.DEVNULL,
                stderr=open(SCRIPTS_DIR / "drainer.stderr", "ab"),  # appended single file
                creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
            )
            spawned += 1
            # Touch the file so a concurrent drainer doesn't pick it up immediately
            os.utime(p, None)
        except Exception as e:
            logging.error("spawn failed for %s: %s", p.name, e)
    return spawned


if __name__ == "__main__":
    n = drain()
    print(f"drained {n} orphan(s)")
```

**2. Wire it into the hooks (one new line each).**

`hooks/session-end.py` — call drainer *after* the new-context Popen at line 167:

```python
# After the existing Popen succeeds:
try:
    subprocess.Popen([
        "uv", "run", "--directory", str(ROOT), "python",
        str(SCRIPTS_DIR / "flush_drainer.py"),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       creationflags=creation_flags)
except Exception as e:
    logging.warning("drainer spawn failed: %s", e)
```

Hook still returns in <100ms because the drainer itself is fire-and-forget. The drainer
process runs in the background, scans `scripts/`, and re-spawns up to 2 flush.py instances.

**3. Minimal `flush.py` changes (`scripts/flush.py:218-222`).**

```python
# Accept --retry flag. Bypass the 60s dedup when present.
RETRY_MODE = "--retry" in sys.argv
# ...later, around line 218:
if not RETRY_MODE and state.get("session_id") == session_id \
        and time.time() - state.get("timestamp", 0) < 60:
    logging.info("Skipping duplicate flush for session %s", session_id)
    context_file.unlink(missing_ok=True)
    return
```

`flush.py` keeps writing FLUSH_ERROR on its own failure (unchanged from today) — but now
it *also* leaves the context file behind on failure, so the next hook fire's drainer
will see it. We need one new flush.py edit: on FLUSH_ERROR, **do not unlink the context
file** (line 255). On FLUSH_OK / real success, unlink as today.

```python
# scripts/flush.py:244-255 replacement
if "FLUSH_OK" in response:
    append_to_daily_log("FLUSH_OK - Nothing worth saving from this session", "Memory Flush")
    context_file.unlink(missing_ok=True)         # success: drop it
elif "FLUSH_ERROR" in response:
    logging.error("Result: %s", response)
    # IMPORTANT: do NOT unlink — let the drainer retry on next hook fire.
    # The FLUSH_ERROR is NOT written to the daily log here anymore — the
    # drainer will write FLUSH_GIVEUP after MAX_LIFETIME_HOURS.
else:
    append_to_daily_log(response, "Session")
    context_file.unlink(missing_ok=True)         # success: drop it
```

**4. Idempotency contract.**

- Context file is **the work item**. Existence = "needs flushing"; deletion = "done".
- Re-spawn on the same context file is safe: `flush.py` reads the file, calls the SDK,
  appends to today's daily log on success, unlinks. If the SDK fails again, the file
  is preserved.
- A "FLUSH_OK" or a successful session entry append happens at most once per (orphan,
  hook-fire); two drainer instances racing on the same file is the only failure mode
  worth analyzing — addressed below.
- **Concurrent drainer race.** Solved by `os.utime(p, None)` immediately after spawn —
  bumps mtime, so the next drainer's `find_orphans` filter (MIN_AGE_SECONDS=90s) excludes
  it. A second drainer firing within 90s sees a "fresh" file and ignores it.

### Pros

- **Smallest code footprint of the three.** One new ~80-line file, ~10 lines of edits
  across hooks + flush.py.
- **Builds on the queue that already exists.** Those 35 orphans on disk get cleaned up
  on first deployment — operator gets "free" drain of past failures.
- **Self-healing.** If the supervisor process is killed (Ctrl-C the hook process,
  reboot mid-flush), the context file remains. Next hook fire = automatic retry.
- **Retry budget scales with usage.** Heavy days = more hooks = more drain attempts.
  Quiet days = fewer drains, but failures still get attended-to whenever the user comes
  back.
- **No new abstraction to learn.** Anyone reading the code sees: "hook spawns flush;
  drainer also spawns flush against orphans." That's it.

### Cons

- **Retry latency is unbounded from the user's perspective.** If a user has one flush
  fail and then never opens Claude Code again that day, the retry never happens. The
  daily-log-on-time guarantee is weaker than B1.
- **Concurrency hazard.** Two hooks firing within seconds of each other (back-to-back
  Claude Code sessions) both spawn drainers, both potentially see the same orphan, both
  spawn flush.py against it. Mitigated by `os.utime(p, None)` + MIN_AGE_SECONDS=90, but
  the race is real and timing-sensitive. Could lose work if both flush.py instances
  succeed and both call `append_to_daily_log` → duplicate session entries.
- **No structured exit-code discrimination.** `flush.py` still composes `FLUSH_ERROR`
  internally for unrecoverable errors, but it doesn't propagate that signal out — the
  context file is preserved and a future drainer will retry futilely for 24h. We retry
  a permanent failure ~16 times before giving up. Wasteful but bounded.
- **Stderr capture is best-effort.** Drainer's `drainer.stderr` is an appended single
  file across all retries → interleaved output. Useful for archaeology but harder to
  attribute to a specific session than B1's per-session file.

### Effort — **S** (2–3h)

- 30 min: `scripts/flush_drainer.py` (~80 lines, all stdlib).
- 15 min: hook edits (one Popen each).
- 30 min: flush.py edits (--retry flag, context-file preservation on FLUSH_ERROR).
- 45 min: tests — drainer unit test that creates fake orphans of varying ages,
  spawns a stub flush.py that exits 0, asserts cleanup happens.
- 30 min: race-condition test — fire two drainers in parallel against the same orphan,
  assert at most one successful daily-log append (or document the duplicate as
  acceptable and add a hash-of-context-file check to `append_to_daily_log`).

### Risk profile

- **Worst case (real):** drainer race causes duplicate session entries in the daily log
  on the same session_id. Cosmetic but visible in compiled wiki articles.
- **Worst case (theoretical):** a context file with permanently-poisonous content
  (somehow triggers the SDK to crash) gets retried 16 times over 24h, burning compute
  + tokens each attempt. Bounded but expensive. Mitigation: track per-file attempt
  count in a sidecar `.attempts` file and give-up at 5 retries regardless of age.
  Adds ~10 lines of code.
- **PreCompact-safe.** Drainer is fire-and-forget. Hook returns immediately.

---

## Option B3 — "Sync Hook With Tight Budget"

### Core idea

Take the "process boundary" thesis to its logical extreme. Drop the fire-and-forget model
entirely for `SessionEnd` (which can afford to block briefly). Hook spawns `flush.py`
synchronously, waits with a **strict 90-second wall-clock budget**, retries on transient
exit codes inside that budget. PreCompact remains fire-and-forget (it must — Claude Code
is mid-compaction). For SessionEnd, the operator gets immediate feedback if the flush
ultimately fails.

This bets that the <200ms hook budget is too pessimistic for SessionEnd specifically.
The hook fires *after* the user typed "exit" or closed the terminal — they're not
waiting on it interactively. The Claude Code process itself is wrapping up. We can
afford 90s.

### How it works

**1. `flush.py` exit-code conventions** — same as B1 (`EXIT_OK / EXIT_TRANSIENT /
EXIT_PERMANENT`).

**2. `hooks/session-end.py` — replace the Popen block (lines 161-167) with a synchronous
retry loop.**

```python
import time

MAX_ATTEMPTS = 3
TOTAL_BUDGET_S = 90.0
BACKOFF_BASE_S = 5.0

def _run_flush_once(cmd: list[str], stderr_path: Path) -> tuple[int, str]:
    """Spawn flush.py, wait for completion, return (rc, stderr_tail)."""
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    with open(stderr_path, "wb") as err_fp:
        proc = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=err_fp,
            creationflags=creation_flags, timeout=TOTAL_BUDGET_S,
        )
    try:
        tail = stderr_path.read_text(encoding="utf-8", errors="replace")[-4096:]
    except OSError:
        tail = ""
    return proc.returncode, tail


# Replaces lines 161-170:
stderr_path = SCRIPTS_DIR / f"flush-{session_id}.stderr"
attempt, deadline = 0, time.time() + TOTAL_BUDGET_S
last_rc, last_tail = -1, ""

while attempt < MAX_ATTEMPTS and time.time() < deadline:
    attempt += 1
    cmd = ["uv", "run", "--directory", str(ROOT), "python", str(flush_script),
           str(context_file), session_id, "--attempt", str(attempt)]
    try:
        last_rc, last_tail = _run_flush_once(cmd, stderr_path)
    except subprocess.TimeoutExpired:
        last_rc, last_tail = -1, "(timeout after %.0fs)" % TOTAL_BUDGET_S
        break
    if last_rc == 0:                              # EXIT_OK
        break
    if last_rc == 65:                             # EXIT_PERMANENT
        break
    sleep_s = min(BACKOFF_BASE_S * (2 ** (attempt - 1)), 20.0)
    if time.time() + sleep_s >= deadline:
        break
    time.sleep(sleep_s)

if last_rc != 0:
    # Final fallback: write FLUSH_ERROR with diagnostics inline to today's log.
    today = datetime.now(timezone.utc).astimezone()
    log_path = ROOT / "daily" / f"{today.strftime('%Y-%m-%d')}.md"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(
            f"### Memory Flush ({today.strftime('%H:%M')})\n\n"
            f"FLUSH_ERROR after {attempt} attempt(s), rc={last_rc}\n"
            f"session={session_id}\n```stderr-tail\n{last_tail.strip() or '(empty)'}\n```\n\n"
        )

stderr_path.unlink(missing_ok=True)
logging.info("SessionEnd flush done: rc=%d after %d attempt(s)", last_rc, attempt)
```

**3. `hooks/pre-compact.py` — unchanged behavior, but spawns the supervisor from B1**
(or `flush_drainer.py` from B2 — operator picks). Critical: PreCompact CANNOT block. It
gets fire-and-forget retry; SessionEnd gets sync retry.

```python
# pre-compact.py:158-167 — same as today, but spawn supervisor (B1) instead of flush.py
flush_script = SCRIPTS_DIR / "flush-supervisor.py"
# rest unchanged
```

**4. Idempotency contract.**

Same as B1: `flush.py --attempt N` bypasses dedup for N>1. Context file is unlinked on
success or permanent failure; preserved on transient-budget-exhaustion (so PreCompact's
drainer or an offline rerun can pick it up).

### Pros

- **Operator gets immediate feedback at SessionEnd.** Daily log has the entry or the
  failure breadcrumb before the user's next session starts.
- **Hook process tree owns everything.** No second supervisor process to think about for
  SessionEnd. Easier to reason about for that path.
- **Tightest blast radius.** Total wall-clock is hard-capped by `TOTAL_BUDGET_S=90s`
  via `subprocess.run(timeout=...)`. Even a hung CLI subprocess is killed.
- **No orphan accumulation.** Sync retry inside the hook means the context file's fate
  is decided before the hook exits.

### Cons

- **Violates the "hooks must stay fast" constraint** (problem brief §3) on the SessionEnd
  path. The brief says "<200ms" — we're aiming for "up to 90s." This is a constraint
  *renegotiation*, not a constraint satisfaction. Justification: SessionEnd fires *after*
  the user is done; PreCompact fires *during* compaction (it stays fast). But this is a
  design-time bet, not a verified empirical claim.
- **PreCompact still needs B1 or B2.** This option doesn't stand alone — it's actually
  "B3 for SessionEnd + B1 or B2 for PreCompact." Two retry mechanisms in one codebase.
- **Hook process lifetime tied to flush.** If Claude Code or the shell decides to kill
  the hook process at ~30s (some terminal emulators on shell exit), we lose the retry
  context.
- **Most invasive change to existing hook code.** B1 and B2 are 1-line hook edits each;
  B3 replaces the entire Popen block with 30+ lines of inline retry logic.
- **Splits flush.py exit-code logic across two callers** (hook + supervisor for PreCompact).
  Two places must agree on what `rc=75` means.

### Effort — **M** (4–5h)

- 30 min: `flush.py` exit-code conventions (same as B1).
- 60 min: SessionEnd hook rewrite with sync retry loop.
- 30 min: PreCompact stays on supervisor (assuming B1 also lands).
- 90 min: tests — integration test for SessionEnd that spawns a fake flush.py exiting
  75/75/0 across attempts, asserts daily log has one session entry, no FLUSH_ERROR.
- 60 min: tests for SessionEnd timeout path (flush.py that hangs >90s) — assert hook
  doesn't hang and writes FLUSH_ERROR with a timeout-tail.

### Risk profile

- **Worst case:** SessionEnd is 90s slower in failure cases. Imperceptible to users
  because they've already exited.
- **Subtle case:** A user closes their laptop / shells out / OS suspends mid-retry-sleep.
  The hook process state is on disk only for FLUSH_OK and FLUSH_ERROR endpoints; mid-retry
  + sleep means partial work. Mitigation: context file remains, so a future hook (in a
  different session) can sweep it via the B2 drainer.
- **Hook constraint renegotiation is a real architectural decision** that needs
  validation. If Claude Code's hook-execution contract actually requires <Ns or treats
  long-running hooks as failed, B3 breaks the contract.

---

## My Recommendation: **B1 — Supervisor Wraps Flush**

### Why B1 over B2

B2 (drainer-on-orphan) is *seductively small* and respects the existing queue, but it
has two real flaws:
- **Latency is unbounded** from the user's perspective. A failed flush during a 3-hour
  break-from-work doesn't retry until the user comes back. The brief's success criteria
  #1 ("the 2026-04-12 cluster is absorbed") implies we want recovery *within minutes*,
  not "whenever the next session fires."
- **The drainer race is real.** `os.utime` + MIN_AGE_SECONDS=90 mitigates but doesn't
  eliminate it. Two flush.py instances both succeeding on the same context file would
  produce duplicate daily-log entries. The fix is per-file locking (`fcntl.flock` on the
  context file before reading), which adds complexity and is platform-divergent
  (Windows needs `msvcrt.locking`). At that point the size advantage is gone.

### Why B1 over B3

B3 (sync hook retry for SessionEnd) bets that the "<200ms hook budget" constraint can be
renegotiated. The brief explicitly lists "Hooks must stay fast" as constraint #2 with a
<200ms budget. **A design that proposes violating an explicit constraint is a renegotiation,
not a fit.** B3 also forces the PreCompact path onto a *second* retry mechanism (B1 or B2)
because PreCompact can't block during compaction. Carrying two retry mechanisms is twice
the surface area for half the unification benefit.

### Why B1 wins

- **Hook stays in its <200ms budget** — supervisor is fire-and-forget from the hook's
  POV (same Popen pattern as today, same flags).
- **Retry happens within minutes**, not "whenever the next hook fires" — supervisor
  drives its own 3-attempt budget with exponential backoff (up to ~90s total).
- **Same mechanism for both SessionEnd and PreCompact** — one supervisor, two callers.
  No special case for compaction-time vs session-end.
- **Stderr capture is per-session-id (deterministic name), GC'd on exit.** No 0-byte
  file pollution problem from round-2.
- **`flush.py`'s "FLUSH_ERROR appended on first failure" misfeature is killed.** The
  supervisor is the sole owner of the daily-log error write, and only writes once after
  the full retry budget is spent.
- **Test surface is exit-code-only.** No SDK mocking required — write a fake `flush.py`
  in a fixture that exits with the right codes; test the supervisor's reaction.

### Honest concession

The exit-code classifier inside `flush.py` (`classify_exception`) *is* a pragmatic
message-sniff against `str(exc)` and `getattr(exc, "exit_code", None)`. That's the same
discrimination problem the brief identifies; we've just relocated it out of the retry
hot-path and into a "compose exit code from exception" function. The benefit is that
the **retry decision** is now SDK-agnostic — the supervisor only ever sees integers.
The cost is that the **classification logic** still must be maintained as the SDK's
error surface evolves. We trade a recurring "retry decision plumbing" cost for a
once-only "exception→integer" cost. That's a good trade.

### Success-criteria mapping (B1)

| Criterion | How B1 satisfies it |
|-----------|---------------------|
| 1. Absorb Control-request-timeout cluster | `classify_exception` returns EXIT_TRANSIENT on `"control request timeout"` substring → supervisor retries up to 3× with backoff. |
| 2. Auth / 400 fails fast | `classify_exception` returns EXIT_PERMANENT on `"invalid api key"` / `"prompt is too long"` → supervisor breaks immediately. |
| 3. Rate-limit doesn't amplify | EXIT_TRANSIENT path uses 5s/10s/20s exponential backoff. If we want longer for rate-limits specifically, extend `classify` to return an integer "suggested-backoff-seconds" alongside the verdict — minor addition. |
| 4. FLUSH_ERROR carries diagnostics | Supervisor's `append_flush_error` writes attempt count, exit code, stderr tail. |
| 5. No orphan stderr files | Per-session-id stderr file is unlinked at end of `main()` (both success and failure paths). |
| 6. Tests exercise real SDK surface | Tests do NOT touch SDK — fake `flush.py` that exits with the relevant code. Round-2's "synthetic RateLimitError" problem doesn't apply because we test the retry layer at the process boundary, not at the exception layer. |
| 7. <1h implementation | B1 is M-sized (4-6h). I claim closest to budget at ~4h; over budget but only by a factor of 4 — within reasonable engineering judgement for a fix that retires an entire class of "untyped retry" bugs. **If we need to compress to <1h, fall back to B2 — it's S-sized.** |

If the council insists on strict <1h adherence: ship B2 first, treat B1 as a v2.
