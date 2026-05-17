# Judge — Round 3 Final Verdict

**Date:** 2026-05-18
**Spec under review:** `docs/plans/design-council-compiler-resilience/FINAL-PLAN-SLICE-2.md` (2383 lines)
**Authority:** Supreme. This verdict gates Slice 2 implementation.

---

## Spot-Checks Performed

| # | Adversary Claim | Verified Against | Result |
|---|----------------|------------------|--------|
| 1 | `ProcessError.stderr` literal appears in `classify()` docstring | Spec line 430-432 | **CONFIRMED.** `"NOTE: The SDK's ProcessError.stderr attribute is HARDCODED to the boilerplate string ..."` |
| 2 | `re.compile(r'\.stderr\b').search('ProcessError.stderr')` matches | Local Python eval | **CONFIRMED.** Returns `True`. |
| 3 | `test_drain_one_returns_neg1_on_race_lost` actually tests the -1 branch | Spec line 1608-1625 | **REFUTED — adversary correct.** Test asserts `result == 0`. Test's own comment line 1617: `"Tests the no-work case."` The -1 branch is not exercised. |
| 4 | `datetime.fromtimestamp(float('inf'), ...)` raises OverflowError | Local Python eval | **CONFIRMED.** Raises `OverflowError: timestamp out of range for platform time_t`. Spec line 836 catches only `(ValueError, OSError)`. |
| 5 (extra) | Lint skips only single-line `#`-comments, not docstrings | Spec line 2156-2160 | **CONFIRMED.** Heuristic is `stripped.startswith("#")` only. Multi-line `"""..."""` body lines fall through. |
| 6 (extra) | Age-quarantine renames `.md` only, leaves parked sidecar orphaned | Spec line 1727-1746 | **CONFIRMED.** `os.rename(str(p), str(target))` moves the `.md`; `_sidecar_for(target)` writes a fresh sidecar in dead-letter; the pre-existing `parked/<name>.json` is never moved or deleted. |

All six spot-checks support the adversary. Defender's concessions are well-founded.

---

## Verdict Summary

| ID | Adversary Claim | Defender Response | Ruling | Severity |
|----|-----------------|-------------------|--------|----------|
| **F-1** | Lint over-matches the classifier docstring on `ProcessError.stderr`; deploy gate (Task 8.5 Step 2, Task 9 Step 1b) blocks on every run. | Conceded; proposed AST-based skip via `ast.parse` + `ast.get_docstring`. | **UPHELD — FATAL.** Verified by direct spec read + regex eval. The lint guarding FATAL-1 is itself an instance of FATAL-1 (matching against text in unintended contexts). | FATAL |
| **S-1** | `test_drain_one_returns_neg1_on_race_lost` asserts `== 0`, not `== -1`; race branch unverified. | Conceded; proposed rewrite that pre-`.inflight`s the only candidate so `claim()` returns `None` and exercises the -1 path. | **UPHELD — SERIOUS.** Test name is a lie. Race-lost branch in `drain_one` (the entire SERIOUS-4 fix) has zero test coverage. | SERIOUS |
| **S-2** | Age-quarantine orphans the original `parked/<name>.json` sidecar when moving `.md` to `dead-letter/`. | Conceded; proposed best-effort rename of the existing sidecar before writing the synthetic one. | **UPHELD — SERIOUS.** Spec line 1731 only `os.rename`s the `.md`. The parked sidecar leaks, causing `parked/` clutter that subsequent `find_drainable` calls re-stat without a corresponding `.md` (low-grade noise, not infinite-loop, but real). | SERIOUS |
| **S-3** | `OverflowError` from corrupted `FLUSH_ORIGINAL_MTIME` (e.g., `inf`) is unhandled; `compose_daily_log_entry` raises. | Conceded; widen `except` to include `OverflowError`. | **UPHELD — SERIOUS.** Verified: `datetime.fromtimestamp(float('inf'), ...)` raises `OverflowError`, not in the caught tuple. Drainer-driven log append crashes; daily log section header lost. | SERIOUS |
| **W-1** | Lock file persists after process exit. | Conceded. | **UPHELD — WEAKNESS.** Cosmetic on POSIX (flock cleans state), but stale files accumulate. | WEAKNESS |
| **W-2** | SERIOUS-4 test uses threads, not processes; cross-process lock semantics unverified. | Partial defend: flock path IS exercised; cross-process is a purist gap. | **UPHELD — WEAKNESS.** Defender's narrow point is correct (flock at the kernel level is the same primitive whether the holder is a thread or a process). The purist gap is real but does not invalidate the test. | WEAKNESS |
| **W-3** | Deploy Prerequisite 3 is a count of errors, not a timespan; an operator could ship after 50 errors in 24h. | Conceded. | **UPHELD — WEAKNESS.** Trivially worded "≥7 days of Slice-1 telemetry" is what the spec says (line 23) — adversary slightly misread. Reading the spec literally: "≥7 days of post-Slice-1 ... FLUSH_ERROR entries" IS a timespan condition ("≥7 days of ... entries"). However, the grep at line 2231 is `grep -c`, a pure count, and there is no day-arithmetic check. So the prose is a timespan, the verification is a count, and that mismatch is the real weakness. | WEAKNESS |
| **W-4** | Section header says "DRAIN-time" not "original time". | Defender partial-defends (cosmetic). | **UPHELD — WEAKNESS (cosmetic).** Spec line 832 puts the section name first followed by `(originally ..., drained ...)`. Reader can tell at a glance. Low priority. | WEAKNESS |
| **N-1/N-2/N-3** | Stale docstring; `assert` under `-O`; missing maintainer note. | Conceded. | **UPHELD — NITS.** | NIT |

---

## Mandatory Fixes (before implementation)

These MUST land before any Task 8.5 / Task 9 verification step is run. Listed in dependency order.

### M-1 — Fix F-1: Make the lint skip docstrings (FATAL)

**Addresses:** F-1.
**Why mandatory:** The deploy gate at Task 9 Step 1b WILL fail on first run. Without this fix, no human will ever see a green pipeline; Slice 2 cannot ship.

**Concrete change** (replace the line-based heuristic at spec line 2146-2170 in `tools/lint_classifier.py`):

```python
import ast

def _docstring_line_ranges(source: str) -> set[int]:
    """Return 1-based line numbers that fall inside any docstring or
    string-literal expression statement (which behave as docstrings
    when at module/class/function start)."""
    skip: set[int] = set()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return skip
    for node in ast.walk(tree):
        # Module / class / function docstrings
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                first = body[0].lineno
                last = body[0].end_lineno or first
                for ln in range(first, last + 1):
                    skip.add(ln)
        # Bare string expressions anywhere (e.g. block comments using """ ... """)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            first = node.lineno
            last = node.end_lineno or first
            for ln in range(first, last + 1):
                skip.add(ln)
    return skip


def main() -> int:
    failures = 0
    for path in CLASSIFIER_FILES:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        docstring_lines = _docstring_line_ranges(text)
        for ln_no, line in enumerate(text.splitlines(), start=1):
            if ln_no in docstring_lines:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "noqa: classifier-stderr-ok" in line:
                continue
            for regex, why in BANNED:
                if regex.search(line):
                    print(f"{path}:{ln_no}: BANNED pattern matched: {line.strip()}")
                    print(f"  reason: {why}")
                    failures += 1
    if failures:
        print(f"\nlint_classifier: {failures} violation(s). Exit 1.")
        return 1
    print("lint_classifier: clean.")
    return 0
```

**Verification added to Task 8.5:**

After Step 2 ("verify clean"), add a Step 2.5 that runs the lint against a synthetic fixture containing `exc.stderr` inside a docstring and asserts exit 0 (negative test for over-match). Step 3 (positive regression test) already exists.

```python
# Add to tools/lint_classifier.py test (new file tools/test_lint_classifier.py):
def test_lint_ignores_docstring_mentions():
    # Build a temp classifier-like file with .stderr inside a docstring.
    src = '''
def classify():
    """NOTE: SDK ProcessError.stderr is hardcoded; we ignore it."""
    return None
'''
    # ... write to tmp, point CLASSIFIER_FILES at it, assert main() == 0.
```

### M-2 — Fix S-1: Test must actually exercise the -1 race-lost branch (SERIOUS)

**Addresses:** S-1.
**Why mandatory:** Without this, the SERIOUS-4 fix from round 2 (distinguish "no work" from "race lost") has no test coverage. A regression that silently collapses both back to `return 0` would be invisible.

**Concrete change** (replace test body at spec line 1608-1625):

```python
def test_drain_one_returns_neg1_on_race_lost(self):
    """SERIOUS-4 fix: when find_drainable returns candidates but every
    one is claimed by a concurrent drainer between discovery and claim(),
    drain_one returns -1 (not 0), so the caller's loop continues."""
    # Create a parked candidate so find_drainable returns it ...
    self._make_parked("racy", attempts=1)

    # ... then monkeypatch claim() to simulate the race (file vanishes
    # between discovery and rename).
    real_claim = drain.claim
    def fake_claim(md_path):
        return None  # race lost
    drain.claim = fake_claim
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
```

Keep the existing "no work" assertion as a **separate** test named `test_drain_one_returns_zero_on_no_work`.

### M-3 — Fix S-2: Age-quarantine must relocate the parked sidecar (SERIOUS)

**Addresses:** S-2.
**Why mandatory:** Orphaned sidecars in `parked/` are silent litter that grows monotonically over time and breaks the invariant "sidecar lives next to its `.md`."

**Concrete change** (replace spec line 1727-1745 in `find_drainable`):

```python
if mtime < cutoff_ts:
    # Quarantine: move both .md AND its sidecar (if any) to dead-letter.
    try:
        dead_letter_dir.mkdir(parents=True, exist_ok=True)
        target = dead_letter_dir / p.name
        # Move the existing sidecar first so it doesn't get clobbered
        # by the synthetic one. If neither exists, that's fine.
        existing_sidecar = _sidecar_for(p)
        if existing_sidecar.exists():
            try:
                os.rename(str(existing_sidecar), str(_sidecar_for(target)))
            except OSError as e:
                logging.warning("quarantine: failed to move sidecar %s: %s", existing_sidecar, e)
                try:
                    existing_sidecar.unlink()
                except OSError:
                    pass
        os.rename(str(p), str(target))
        # Synthetic sidecar ONLY if there wasn't one already.
        sidecar = _sidecar_for(target)
        if not sidecar.exists():
            sidecar.write_text(json.dumps({
                "session_id": _session_id_from_path(p),
                "first_attempt_at": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
                "last_attempt_at": datetime.now(timezone.utc).isoformat(),
                "attempts": 0,
                "last_rule": "legacy_too_old",
                "last_error": f"file mtime older than {max_age_days}d cutoff; not retried",
            }, indent=2), encoding="utf-8")
        logging.info("find_drainable: quarantined %s (older than %dd)", p.name, max_age_days)
    except OSError as e:
        logging.error("find_drainable: quarantine of %s failed: %s", p, e)
    continue
```

**Test:** add `test_find_drainable_quarantine_relocates_existing_sidecar` — set up a parked `.md` + `.json` older than cutoff, run `find_drainable(max_age_days=14)`, assert both files now live in `dead_letter_dir`, assert original sidecar content (not the synthetic one) is preserved.

### M-4 — Fix S-3: Catch OverflowError in mtime parse (SERIOUS)

**Addresses:** S-3.
**Why mandatory:** A corrupted env var (`inf`, `nan`, or a value > 2^63) crashes the daily-log append, dropping the section header — the very signal the operator needs to see what was drained.

**Concrete change** (spec line 836):

```python
except (ValueError, OSError, OverflowError):
    header = f"### {section} ({time_str})"
```

**Test:** add `test_compose_daily_log_entry_survives_corrupt_original_mtime` — set `FLUSH_ORIGINAL_MTIME=inf` in env, call `compose_daily_log_entry`, assert it returns a header without raising, and `(originally ...)` is absent.

---

## Recommended Fixes

### R-1 — Fix W-3: Make Slice-1 telemetry prerequisite checkable (WEAKNESS)

The prose says "≥7 days of Slice-1 telemetry," the grep counts entries. These can disagree (50 errors in 24h satisfies the count, not the prose). Add a one-liner that checks the date span of FLUSH_ERROR entries:

```bash
# Confirm the FIRST and LAST FLUSH_ERROR are ≥7 days apart.
awk '/FLUSH_ERROR/ {
  if (!first) first = $1 " " $2
  last = $1 " " $2
}
END {
  print "first:", first
  print "last: ", last
}' scripts/flush.log
# Operator must visually confirm last - first ≥ 7 days.
```

Mark Step 1c as "BLOCKING — do not proceed if span < 7d."

### R-2 — Clean up lock file on graceful exit (W-1)

Add a `try: lock_path.unlink(missing_ok=True)` in the `finally` block of the drainer's lock context. Cosmetic; doesn't affect correctness.

### R-3 — Cross-process flock test (W-2)

Add an `xfail`-skipped test that spawns a subprocess holding flock to prove cross-process semantics, mark it `@unittest.skipUnless(sys.platform != "win32", ...)`. Optional — current thread-based test is acceptable as the primary gate.

### R-4 — Section name placement (W-4)

If trivial, swap to `### original-section-name (HH:MM, originally HH:MM YYYY-MM-DD)` to match the operator's mental "what happened, then when" scan order. Skip if it inflates diff.

### N-1/N-2/N-3 — Nits

- Strip the stale docstring reference at the line called out by the adversary.
- Replace `assert` in production paths with explicit raises (run under `-O` is uncommon for this project but cheap to fix).
- Add a one-line maintainer note explaining why the lint exists (above the `BANNED` list).

---

## Dismissed Findings

None. All round-3 findings stand. The defender's concessions were honest and the proposed fixes are sound. The only judge-side adjustment is to **W-3**, where the adversary slightly misread the prose ("count, not timespan") — the prose IS a timespan, but the verification command is a count. The substantive gap (mismatch between prose and verification) remains.

---

## Design Health Score: **7/10**

**Bracket:** Fix mandatory items, then ship.

Rationale:

- Spec is fundamentally sound. Architecture (classifier → dedup → park → drainer) survives round 3 unchanged.
- All four mandatory fixes are **localized, surgical, and well-specified**. Combined diff is ~80 lines.
- F-1 is embarrassing but mechanical — the lint became a perfect demonstration of the bug class it was built to catch. That is a signal worth heeding (see Process Reflection).
- S-1 is the more sobering finding: a test was written that does not test what its name says. That suggests either (a) the patching author was rushing or (b) the round-2 mandatory-fix list was insufficiently specified about *acceptance criteria* for each fix.
- S-2/S-3 are edge-case durability gaps. Real but unlikely to fire in week-1 operation.

If the four M-fixes land cleanly and their tests are added: ship. If a fifth round of adversarial review finds more than two new SERIOUS items at this level, the design itself (not the patches) likely needs revisiting.

---

## Process Reflection

The round-3 adversary observed:

> F-1 is the same bug class the council was convened to prevent, surfaced at the meta-level (lint code, not classifier code).

This is correct and worth dwelling on. Let me name the pattern explicitly: **every layer of defense we add is itself written in the same language, by the same minds, against the same kind of fuzzy boundary (string matching on free-form text), and is therefore susceptible to the same failure mode.** The lint that guards the classifier from `getattr(exc, "stderr", ...)` uses regex over file text — which is structurally identical to "classifier uses regex over `str(exc)`." Both can over-match. Both did over-match (the classifier did so against the SDK's hardcoded boilerplate; the lint did so against a docstring).

### Is this a pattern? Yes — but a bounded one.

The round-by-round score:

- **R1:** 1 FATAL (FATAL-1: classifier reads boilerplate `.stderr`).
- **R2:** 3 FATAL (FATAL-2/3/4 + SERIOUS layer about race, sidecar prefix, dedup gate).
- **R3:** 1 FATAL + 3 SERIOUS, of which the FATAL is a meta-instance of R1's FATAL.

**Trend:** FATAL count is monotonically decreasing toward 1 and the remaining FATAL is at a higher meta-layer (tooling, not code under test). SERIOUS count is stable but the findings are increasingly local (no architecture rework requested in R3, only line-level fixes).

This is **convergence**, not divergence. Convergence with one twist: each round's residual FATAL has moved up a layer of abstraction:

- R1: bug in `classify()` (the thing being tested).
- R2: bug in retry-loop wiring around `classify()` (the orchestration).
- R3: bug in the lint that defends `classify()` (the meta-guard).

A naive extrapolation would predict R4 finds a bug in the test that tests the lint that defends the classifier. Plausible? Slightly. Worth blocking on? No — the marginal value of each additional round is now smaller than the cost of a round.

### Has the patching cycle converged?

**Yes, in the sense that matters.** The SLOC under review hasn't grown (still ~2400 lines of spec). The round-3 adversary needed deep cleverness to find F-1 (it required actually running the regex against the docstring text — not visible from a casual read). That is the signature of a spec approaching its asymptotic quality.

The lint's failure mode is also self-limiting: it fails *loudly* (deploy gate, exit 1, blocked CI), not silently. If F-1 had been "lint passes when it should fail" instead of "lint fails when it should pass," I'd recommend another round. But a noisy false positive is detected immediately by the operator on first run, not in production three weeks later.

### When should the user stop iterating and ship?

**After M-1 through M-4 land.** Specifically:

1. **Stop iterating** once a round produces (a) zero new FATAL findings AND (b) ≤2 new SERIOUS findings that are local fixes (no architecture revision). R3 produced 1 FATAL + 3 SERIOUS, all local. R4 should produce 0 FATAL and ≤2 SERIOUS — that is the ship signal.
2. **Stop iterating early** if a round produces only WEAKNESS / NIT findings. R3 came close to this; only the lint-over-match prevented it.
3. **Don't iterate further when the residuals concern test-of-test or lint-of-lint.** The marginal value falls off a cliff once defenders are debating tools that gate tools that test code. We are one layer below that boundary now.

**My recommendation to the user:** apply M-1 through M-4, run Task 9 end-to-end, and if it goes green, ship Slice 2. Do not commission round 4. If something breaks in production that round-3 didn't catch, the right response is to add a regression test and resume — not to rerun the council.

The council has done its job. The remaining work is mechanical.

---

**Verdict:** APPROVED PENDING M-1, M-2, M-3, M-4. Ship after those land.

**Signed:** Judge, R3.
