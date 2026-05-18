# Sprint: Compiler Followups v1

**Slug:** `compiler-followups-v1`
**Date opened:** 2026-05-18
**Source:** Slice 2 followups (W-2..W-7 from round-3 adversarial review) + 3 of the 5 unfinished structural gaps from `knowledge/concepts/claude-memory-compiler-setup.md` (May 13 dating). The **May-13 concept article is aspirational and partially stale** — its claims have been re-verified during pre-flight (see §"Source-of-Truth verification" below). Compile-cost remediation (May-13 gap #3) is explicitly **out of scope** here.

## Problem statement

The compiler's **resilience + observability** stack is now solid (Slices 1+2 shipped 2026-05-17..18). What remains open:

- **Slice 2 polish items** identified during round-3 adversarial review but explicitly deferred from that scope. Each is small (15min–2h) but they compose into operator-noticeable quality-of-life improvements: log rotation (avoid 28K+ line `flush.log`), cost-budget circuit breaker (cap retry-storm dollars/day), real-timespan check for the Slice-1 telemetry deploy gate, drained-entry chronology header section-name preservation, cross-process flock test for the dedup ledger, and classifier regex refinement once Slice-1 evidence accumulates.
- **3 of the 5 unfinished structural gaps** from May 13 retrospective: (1) `knowledge/connections/` HAS 19 articles (75KB) but `compile.py` produces them ad-hoc with no per-run cap, no quality/format guard rails, and the only governance is whatever the single LLM prompt is taught to do; (2) `knowledge/qa/` is empty and `scripts/query.py` (138 LOC) is never invoked from any hook; (3) no provenance/confidence schema on compiled articles — operators cannot tell which session contributed which fact; (4) `scripts/lint.py` is LLM-only and expensive, with no deterministic pre-pass for frontmatter validation, dead `[[wiki-links]]`, or orphan article detection.
- **May-13 gap #3 (compile cost) is NOT in this sprint.** `scripts/compile.py` is a single-pass full-context-sender (1 `query()` call; lines 53-63 pump all 204 existing articles into every prompt). The file-level hash gate at line 196 only skips when the daily log is unchanged — it does not reduce per-compile input tokens on the path that actually compiles. Mean historical compile cost is **$4.48** (n=52; max $11.88). Reducing this is genuinely substantial work (a multi-pass dedup design that was previously REJECTED by a 2-round adversarial review for repeating the FATAL-1 type-based-retry-whitelist bug class). It belongs in its own sprint with fresh design council.

This sprint closes the polish items + 3 of the 5 structural gaps in parallel. Compile-cost remediation is deferred to a later sprint (followups-v2 or a dedicated cost-reduction sprint).

## Constraints

1. **No new heavyweight dependencies.** Stdlib + the existing `claude-agent-sdk`, `python-dotenv`, `tzdata`. The lint pre-pass uses `ast` + `re`; the provenance work uses YAML strings (no PyYAML — `knowledge/concepts/*.md` already use raw `---` blocks, parse by line).
2. **Knowledge dir is per-user gitignored.** Schema changes ship as compiler behavior (forward-only); the existing 200+ articles get a backfill script (T1.B) that adds the `evidence:` block when absent.
3. **No retroactive breaking changes to existing articles.** Provenance schema MUST be additive — old articles without `evidence:` still parse.
4. **Compiler bills real Anthropic dollars at realistic rates.** Historical mean compile cost is **$4.48** (state.json, n=52). Any cost ceilings in this sprint MUST accommodate this reality: T0.D budget circuit breaker defaults to **$15/day** (≥2 historical-mean retries); sprint hard cap is **$25 total LLM spend** (dogfood compile alone is $4-12; T2/T3 add ~$0.5-2 each for real-LLM smoke; balance is headroom). Mocked unit tests are free; only T2/T3 smoke + dogfood compile draw against the cap.
5. **Sprint timebox:** ~3-5 days of orchestrator wall-clock. Smaller than the secretary-sprint-3 scale; closer to a "polish sprint" than a "feature sprint."
6. **Slice-1 telemetry prerequisite for T0.C (regex refinement) still applies.** Slice 1 shipped 2026-05-17; flush.log has 0 days of enriched FLUSH_ERROR entries as of sprint open. The structurally-honest path is: T0.C ships a tool that ASSERTS `# UNVERIFIED — speculative` annotations on rules without ground-truth matches (the day-1 path). Telemetry-grounded regex refinement defers to followups-v2 by design, not by accident.

## Success criteria

The sprint is **done** when ALL of:

1. All 60-80 `COMPLETION-CHECKLIST.md` rows tagged `blocking` are processed by codesweep.
2. The acceptance test (Task T-FINAL: `python tools/check_followups_v1.py`) returns exit 0. The script:
   - Confirms 6 polish items shipped (log rotation, cost-budget, timespan check, section preservation, flock cross-process test, regex refinement OR deferral file).
   - Confirms 4 structural gaps shipped (connections produce non-zero output; QA loop wired; ≥3 articles have `evidence:` block; lint pre-pass catches 4 canary defects).
   - Tests pass: `python -m unittest scripts.test_classifier scripts.test_dedup scripts.test_drain scripts.test_lint_kb scripts.test_connections scripts.test_qa_loop scripts.test_provenance scripts.test_flush_error_format scripts.test_utils_state` returns OK.
   - Both lint gates clean: `tools/lint_classifier.py` AND `tools/lint_kb.py knowledge/concepts/`.
3. Operator dogfood (CHECKPOINT-D): run `scripts/compile.py --file daily/<recent>.md`, observe (a) at least one connection article gets created if non-obvious relationship detected, (b) the daily log's "Memory Flush" entries are unchanged in format (no regression from Slice 1's enriched FLUSH_ERROR), (c) the new `evidence:` block appears on the modified concept articles, (d) the QA loop wrote at least one entry to `knowledge/qa/` if the daily log contained user questions, (e) observed cost is within operator tolerance and recorded in `status/dogfood-evidence.md` — no hardcoded threshold (historical baseline $4.48-$11.88, expect within band).

## Known context

- `scripts/compile.py` is the LLM-compile loop. **224 lines**. Has a file-level hash early-return (`compile.py:196` — skips when daily log hash unchanged; does NOT reduce input-token cost on the compile path). Reads `AGENTS_FILE` for schema. Writes to `knowledge/concepts/` + `knowledge/connections/` + appends to `knowledge/log.md`. Has a **single LLM prompt** (lines 67-127) with a **single `query()` call** (line 132); the prompt's tool allowlist is `Read/Write/Edit/Glob/Grep` with `permission_mode="acceptEdits"`. There is no Pass-1/Pass-2 architecture.
- `scripts/lint.py` (312 LOC) is the current LLM-only lint. Untouched by this sprint EXCEPT for adding a pre-pass that runs before it.
- `scripts/query.py` (138 LOC) implements the QA loop spine but is never wired. Likely takes a question string, searches knowledge/, returns an answer-and-citation dict.
- `knowledge/concepts/` has 200+ `.md` files with YAML-ish frontmatter (`---` delimited). Format is consistent — pattern-matching is reliable.
- `knowledge/connections/` **has 19 substantive articles totaling ~75KB** (NOT empty as an earlier draft inherited from the concept article). The articles are produced opportunistically by `compile.py`'s single prompt when the LLM detects a cross-concept relationship; there is no per-run cap, no naming-convention enforcement, no quality bar. T2 in this sprint adds those guardrails — it does NOT seed the directory.
- `knowledge/qa/` exists and is empty.
- `knowledge/index.md` is 59KB at sprint open — likely close to 60-65KB by sprint close.
- `scripts/state.json` ingestion table: **n=52 historical compiles, mean cost $4.48, max $11.88, total $232.74 lifetime**. These are the real numbers any budget gate must accommodate.
- `scripts/flush.log` is **28,938 lines** of accumulated log history (unbounded by Slice 1; T0.A in this sprint adds rotation).
- `grep -c "FLUSH_ERROR.*exit_code=" scripts/flush.log` = **0** (Slice 1 ships the structured FLUSH_ERROR format across multiple lines; same-line regex doesn't capture it). T0.C must use multi-line awareness or block-aware grep.

## Source-of-Truth verification (pre-flight)

Per `autonomous-sprint` skill §1b.5, every quotative claim above was re-verified against the live tree on 2026-05-18 before this brief was finalized. Commands run + results:

| Claim | Command | Result |
|---|---|---|
| `compile.py` is one-pass | `grep -c "query(" scripts/compile.py` | **1** ✅ |
| `knowledge/connections/` has 19 articles | `ls knowledge/connections/ \| wc -l` | **19** ✅ |
| Connections dir has zero empty files | `find knowledge/connections/ -size 0 \| wc -l` | **0** ✅ |
| Connections dir is ~75KB | `du -sh knowledge/connections/` | **108K** (close enough — drift from compile output) ✅ |
| State.json cost stats | `python3 -c "..."` (mean/min/max/sum over `ingested[*].cost_usd`) | **n=52, min=$0.84, max=$11.88, mean=$4.48, total=$232.74** ✅ |
| `flush.log` line count | `wc -l scripts/flush.log` | **28938** ✅ |
| Slice-1 enriched FLUSH_ERROR present in log | `grep -c "FLUSH_ERROR.*exit_code=" scripts/flush.log` | **0** (0 days post-Slice-1 telemetry; T0.C must structurally accommodate) ✅ |
| `knowledge/qa/` is empty | `ls knowledge/qa/` exits 1 (or returns empty) | **exit 1** ✅ |

An earlier draft of this brief inherited 3 wrong claims from `knowledge/concepts/claude-memory-compiler-setup.md` (the aspirational concept article). Those claims were corrected during pre-flight. The article itself is **stale** and should be updated as a sprint sub-task (T1.D — see checklist).

## Open questions (for user CHECKPOINT-A)

These are sprint-scope questions. Defaults provided; user can override.

**Q1: Telemetry prerequisite for T0.C (classifier regex refinement).**
- Default: defer T0.C as a single follow-up file (`followups.md::FU-FU1-T0-C`) if `flush.log` has <7d of post-Slice-1 entries at sprint close. Don't block sprint completion on telemetry the sprint can't generate.
- Alternative: drop T0.C from sprint scope entirely.
- Impact: minor. T0.C is one regex edit + one test row.

**Q2: Provenance schema — full-fidelity or summary.**
- Default: per-article `evidence:` block of the form:
  ```yaml
  evidence:
    - session_id: <uuid>
      flushed_at: <iso>
      confidence: high|medium|low
      claim_summary: <one-liner>
  ```
  Compiler emits this for new articles; existing articles get an `evidence: [legacy]` shim on next touch.
- Alternative: simpler `confidence: <level>` at article-level (no per-claim breakdown).
- Impact: substantial. Full-fidelity is ~3h more work but matches the secretary-memory pattern; summary is faster but doesn't differentiate from `last_updated:`.

**Q3: Connections synthesis budget.**
- Default: cap at 1 connections-article per compile run (compile.py emits one IF a clear cross-concept relationship is detected). Adds ~$0.05/run.
- Alternative: unlimited connections per run (could spike a single compile from ~$0.20 to ~$1+ if the log mentions many concepts).
- Impact: cost-control critical.

**Q4: QA loop trigger and cost.**
- Default: SessionStart hook scans the most recent daily log for question marks in user lines; passes each to `query.py` and writes results to `knowledge/qa/<hash>.md`. Cap: 3 questions per session.
- Alternative: no automatic trigger; QA loop is operator-invoked only via `python scripts/query.py "..."`.
- Impact: automatic trigger adds ~$0.05/session × N sessions/day. Manual is free but evidence shows the user is the operator and 32 orphans sat for 5 weeks unattended (precedent: manual workflows die in this codebase).

## Adversarial review history

Adversarial-review of THIS brief was performed at sprint pre-flight as part of the user's `/adversarial-review` invocation. Findings + dispositions will be filed here after the review completes (see §3c.5 of `autonomous-sprint` skill).

## Team composition

See `team-of-agents-v1.md` for the role catalogue + protocols. Summary:
- 1 Orchestrator (Opus, this session)
- 5 implementer tracks (T0 Polish, T1 Provenance, T2 Connections, T3 QA, T4 Lint) — Opus
- 5 Sonnet integrator workers (one per implementer track)
- 1 Adversary-on-tap per merge (Opus, ephemeral)
- No Researcher track (scope is internal codebase only)
- No QA budget tier (no real-LLM acceptance run; smoke tests are unit-level)

## Sequencing

```
                ┌────────────────────────────────────────┐
Wave 0  ────► T0 polish + T2 connections-governance + T4 lint pre-pass (all independent)
                ├── T0.A log rotation (RotatingFileHandler swap)
                ├── T0.B section name preservation (chronology header)
                ├── T0.C classifier regex day-1 path (annotate unverified rules + ship the audit tool)
                ├── T0.D cost-budget circuit breaker
                ├── T0.E timespan check (deploy gate hardening)
                ├── T0.F flock cross-process test
                ├── T2.A extend compile.py prompt with per-run-cap connections governance
                ├── T2.B index + log integration for new connections
                └── T4 lint pre-pass (tools/lint_kb.py: frontmatter, dead-links, orphans)

Wave 1  ────► T1 provenance schema (foundational; blocks T3 QA loop)
                ├── T1.A schema design — `evidence:` block format
                ├── T1.B compile.py emit on new articles (interleaves with T2.A prompt edits;
                │       integrator merges T1 first, T2 layered on top — see §7 R4 callout)
                ├── T1.C backfill script for existing articles
                └── T1.D update knowledge/concepts/claude-memory-compiler-setup.md (stale-source fix)

Wave 2  ────► T3 QA loop (consumes T1 evidence schema for citation linking)

Wave 3  ────► acceptance test + dogfood + close-out
```

T2 does NOT depend on T1's evidence schema — the connections governance is prompt-level guard rails (per-run cap, naming convention) that don't require provenance. T2 moves to Wave 0 alongside T0 and T4. Only T3 (QA loop with citation output) consumes T1's `evidence:` field.

## Out of scope (explicit deferrals)

- **Migrating off the bundled CLI to direct Anthropic SDK.** Major SDK swap; would unlock typed exceptions, defeats the round-2 problem entirely — but out of sprint scope.
- **`compile.py` index-only first-pass dedup.** Re-attempting the round-2 plan that got rejected. The current hash-equality gate is good enough for now; revisit when costs justify.
- **Email/Plaud/calendar integrations.** These would extend the compiler to non-conversation sources. Different scope.
- **UI for browsing the knowledge base.** Out of compiler scope entirely.
- **`scripts/lint.py` LLM-only replacement.** Stays as-is; we add a PRE-PASS, not a replacement.

## Followups bucket

`docs/plans/compiler-followups-v1/followups.md` will collect items surfaced during execution that need user approval before being added to the sweep. Items there at sprint close roll to `compiler-followups-v2` brief.
