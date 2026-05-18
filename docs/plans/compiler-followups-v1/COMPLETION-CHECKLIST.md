# Completion Checklist — compiler-followups-v1

Each row is a one-line mechanically-verifiable assertion. Codesweep tracks state (processed=true means the row is verified green). Adversary-on-tap re-verifies after each integration commit.

Notation:
- **Verify** column: a shell command or grep that, when satisfied, justifies marking the row processed. "Y/N" in Blocking column: Y = must be green for sprint to ship; N = nice-to-have, defer-able to followups-v2.

**Verify commands are bash-as-pasted, NOT markdown-table-escaped.** Workers running the verify column must un-escape any `\|` that arrived from MD table rendering. Pipes are literal `|`; alternation in `grep -E` is `|` (not `\|`); alternation in BRE `grep` is `\|`. Numeric comparisons use `-ge`/`-gt` (not `\>=`).

## T0 — Slice 2 polish

### T0.A — Log rotation (flush.log + drain.log)

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.A.1 | `scripts/flush.py` uses `RotatingFileHandler(maxBytes=5MB, backupCount=3)` for `flush.log` | `grep -n RotatingFileHandler scripts/flush.py` | R1 | Y |
| T0.A.2 | `scripts/drain.py` uses `RotatingFileHandler(maxBytes=5MB, backupCount=3)` for `drain.log` | `grep -n RotatingFileHandler scripts/drain.py` | R1 | Y |
| T0.A.3 | Existing 28K+ line `flush.log` is renamed to `flush.log.legacy-pre-rotation` on first invocation OR a one-shot trim runs at module load | `[[ $(ls scripts/flush.log* 2>/dev/null \| wc -l) -ge 1 ]]` (file still exists, just bounded) | R1 | N |
| T0.A.4 | New test `test_log_rotation_caps_size` writes >5MB to the handler; asserts file count ≤ 4 (current + 3 backups) | `python -m unittest scripts.test_log_rotation` | R1 | Y |

### T0.B — Section name preservation in chronology header

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.B.1 | Drainer passes `FLUSH_ORIGINAL_SECTION` env (in addition to `FLUSH_ORIGINAL_MTIME`) when spawning flush.py | `grep -n FLUSH_ORIGINAL_SECTION scripts/drain.py` | R1 | Y |
| T0.B.2 | `append_to_daily_log` reads `FLUSH_ORIGINAL_SECTION` env; uses it as the section name in the chronology header if set | `grep -n FLUSH_ORIGINAL_SECTION scripts/flush.py` | R1 | Y |
| T0.B.3 | Test `test_chronology_header_uses_original_section` asserts `FLUSH_ORIGINAL_SECTION=Session` produces `### Session (originally ...)` not `### Memory Flush (originally ...)` | `python -m unittest scripts.test_dedup.TestDedup.test_chronology_header_uses_original_section` | R1 | Y |

### T0.C — Classifier regex audit (telemetry-aware; ships day-1)

The day-1 path: annotate speculative rules with `# UNVERIFIED — speculative` so the audit script can pass even with 0 days of accumulated Slice-1 telemetry. Telemetry-grounded regex refinement is filed as FU-FU1-T0-C for followups-v2.

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.C.0a | `auth_invalid` and `prompt_too_long` rules in `scripts/classifier.py` carry `# UNVERIFIED — speculative` annotations (because no real-world matches exist in flush.log yet) | `grep -B1 "auth_invalid\|prompt_too_long" scripts/classifier.py \| grep -c "UNVERIFIED"` returns ≥2 | R1 | Y |
| T0.C.0 | `tools/check_classifier_groundedness.py` reads `flush.log` (block-aware: groups `FLUSH_ERROR` headers with their `stderr_tail:` indented block), extracts distinct stderr-signal patterns, asserts each `CLASSIFICATIONS` rule either (a) has ≥1 real-world match OR (b) is annotated `# UNVERIFIED — speculative` | `python tools/check_classifier_groundedness.py` exit 0 | R1 | Y |
| T0.C.1 | If `tools/check_classifier_groundedness.py` finds ≥100 enriched FLUSH_ERROR entries spanning ≥7 days at some FUTURE run: emit advisory log line listing patterns that could promote out of `UNVERIFIED` status. **NOT BLOCKING in this sprint.** | `python tools/check_classifier_groundedness.py --advisory` returns non-empty if conditions met; advisory-only | R1 | N |
| T0.C.2 | `followups.md` has a `FU-FU1-T0-C` entry: "After ≥7d Slice-1 telemetry, re-run `check_classifier_groundedness.py --promote` and tighten `auth_invalid`/`prompt_too_long` regexes based on real stderr signatures" | `grep -n "FU-FU1-T0-C" docs/plans/compiler-followups-v1/followups.md` | R1 | Y |

### T0.D — Cost-budget circuit breaker

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.D.1 | `state.json` gets a new `daily_retry_cost` dict keyed by ISO date | `python -c "import json; d=json.load(open('scripts/state.json')); print('daily_retry_cost' in d)"` returns `True` after first run | R1 | Y |
| T0.D.2 | After each LLM call from `flush.py` retry loop, `state['daily_retry_cost'][today_iso]` increments by the actual cost | `grep -n "daily_retry_cost" scripts/flush.py` shows write site | R1 | Y |
| T0.D.3 | If `daily_retry_cost[today] > daily_retry_cost_cap` (default **$15.0**, ≥2 historical-mean retries), drainer SKIPS dispatching flush.py and logs `BUDGET_EXCEEDED` to drain.log | `grep -n "BUDGET_EXCEEDED" scripts/drain.py` | R1 | Y |
| T0.D.4 | Cap is overridable via `state["daily_retry_cost_cap"]` (default 15.0, justified by historical mean $4.48 from state.json) | `grep -n "daily_retry_cost_cap" scripts/drain.py` | R1 | N |
| T0.D.5 | Test `test_drain_skips_when_budget_exceeded` synthesizes `daily_retry_cost[today]=20.0` (above default), asserts drain_one returns 0 + logs BUDGET_EXCEEDED | `python -m unittest scripts.test_drain.test_drain_skips_when_budget_exceeded` | R1 | Y |

### T0.E — Real-timespan check (deploy gate hardening)

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.E.1 | `tools/check_telemetry_span.sh` exists; computes first/last FLUSH_ERROR ISO timestamps; exits 0 iff span ≥ 7d | `tools/check_telemetry_span.sh` exit code reflects actual span | R1 | Y |
| T0.E.2 | `FINAL-PLAN-SLICE-2.md` Task 9 Step 1c updated to reference `tools/check_telemetry_span.sh` instead of the count-only grep | `grep -n check_telemetry_span.sh docs/plans/design-council-compiler-resilience/FINAL-PLAN-SLICE-2.md` | R1 | N |

### T0.F — Cross-process flock test for dedup ledger

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T0.F.1 | New test `test_concurrent_record_append_across_processes` spawns 5 subprocesses each calling `record_append` 4 times; asserts ledger has 20 unique entries | `python -m unittest scripts.test_dedup.test_concurrent_record_append_across_processes` | R1 | Y |
| T0.F.2 | Test runs in <10s wall-clock (subprocess spawn overhead bounded) | timing captured in test output | R1 | N |

---

## T1 — Provenance schema

### T1.A — Schema design

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T1.A.1 | `docs/plans/compiler-followups-v1/evidence-schema.md` exists; documents the `evidence:` YAML block format | `test -f docs/plans/compiler-followups-v1/evidence-schema.md` | R2 | Y |
| T1.A.2 | Schema defines required fields: `session_id`, `flushed_at`, `confidence` (high/medium/low). Optional: `claim_summary` | `grep -E "(session_id\|flushed_at\|confidence)" docs/plans/compiler-followups-v1/evidence-schema.md` returns 3 matches | R2 | Y |
| T1.A.3 | `handshake/t1-evidence-schema.md` published with `status: ready` | `grep "status: ready" docs/plans/compiler-followups-v1/handshake/t1-evidence-schema.md` | R2 | Y |

### T1.B — compile.py emits evidence on new articles

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T1.B.1 | `scripts/compile.py` prompt instructs the LLM to include an `evidence:` block in every NEW concept article | `grep -n "evidence:" scripts/compile.py` (in prompt template) | R2 | Y |
| T1.B.2 | After T1.C backfill completes, **every** article in `knowledge/concepts/` has an `evidence:` block | `python tools/check_evidence_blocks.py knowledge/concepts/` exit 0 (script in T1.B.4) | R2 | Y |
| T1.B.3 | Updates to existing articles: prompt instructs LLM to APPEND to the `evidence:` list, not replace | `grep -A2 "append" scripts/compile.py` (in prompt template) | R2 | Y |
| T1.B.4 | `tools/check_evidence_blocks.py` validates the `evidence:` block format in a target directory | `python tools/check_evidence_blocks.py knowledge/concepts/claude-memory-compiler-setup.md` exit 0 | R2 | Y |

### T1.C — Backfill script for existing articles

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T1.C.1 | `tools/backfill_evidence.py` walks `knowledge/concepts/` and adds `evidence: [{session_id: "legacy", flushed_at: "<file_mtime>", confidence: "low"}]` to articles missing the block | `python tools/backfill_evidence.py --dry-run knowledge/concepts/` lists candidate files | R2 | Y |
| T1.C.2 | `tools/backfill_evidence.py` is idempotent: running it twice produces no diff on the second run | `python tools/backfill_evidence.py knowledge/concepts/ && python tools/backfill_evidence.py knowledge/concepts/ && git -C ~/tools/claude-memory-compiler/knowledge diff --exit-code` | R2 | N |
| T1.C.3 | Test `test_backfill_idempotency` covers the above | `python -m unittest scripts.test_provenance.test_backfill_idempotency` | R2 | Y |
| T1.C.4 | Test `test_backfill_preserves_existing_evidence` ensures articles that already have an `evidence:` block (from T1.B newly-compiled articles) are not modified by the backfill | `python -m unittest scripts.test_provenance.test_backfill_preserves_existing_evidence` | R2 | Y |

### T1.D — Update stale concept article (R1 adversarial-review finding)

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T1.D.1 | `knowledge/concepts/claude-memory-compiler-setup.md` §"Five structural gaps identified" section is updated to reflect current reality: gap #1 (connections) is partially closed (19 articles exist; T2 in this sprint adds governance); gap #2 (QA loop) is closed by T3 in this sprint; gap #3 (compile cost) is still open and deferred to a future sprint; gap #4 (provenance) is closed by T1; gap #5 (LLM-only lint) is closed by T4 | `grep -A20 "Five structural gaps" knowledge/concepts/claude-memory-compiler-setup.md \| grep -E "(closed in followups-v1\|deferred\|gap #)"` returns ≥5 matches | R2 | Y |

---

## T2 — Connections governance (NOT seeding — dir already has 19 articles)

The connections directory is NOT empty — it has 19 substantive articles produced opportunistically by `compile.py`'s single prompt. T2 adds **governance** (per-run cap, naming convention, audit) to prevent unbounded growth + ensures format consistency. It does NOT introduce a "pass-2" architecture (there is no pass-2 — only one prompt, one query call).

### T2.A — Compile-prompt governance for connections

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T2.A.1 | If `compile.py` contains an `empty-dir-guard` early-return for connections (defensive against legacy code paths), remove it. If no such guard exists, document the absence with a comment `# connections dir governance: no empty-dir short-circuit` | `grep -n "connections.*0\|empty\|placeholder" scripts/compile.py` returns no surviving early-return; comment present | R3 | Y |
| T2.A.2 | `compile.py`'s single prompt is extended with a `## Cross-concept connections` rules block: when 2+ existing concepts are referenced AND the relationship is non-obvious, emit ONE new file in `knowledge/connections/` with filename `<concept-a-slug>-<concept-b-slug>.md` (alphabetical order) and ≥3-paragraph rationale | `grep -A5 "Cross-concept connections" scripts/compile.py` shows the new rules block | R3 | Y |
| T2.A.3 | At most ONE connection-article per compile run (cost cap; per CHECKPOINT-A Q3 default) | `grep -nE "at most ONE\|max[_-]?[1one]\|single connection\|one connection" scripts/compile.py` finds at least one match | R3 | Y |
| T2.A.4 | Test `test_connections_synthesis_respects_per_run_cap` mocks the SDK `query()` to return a fake prompt-response with 3 connection-article filenames; asserts only the first is written | `python -m unittest scripts.test_connections.test_connections_synthesis_respects_per_run_cap` | R3 | Y |
| T2.A.5 | Test `test_connections_naming_convention` mocks SDK output with non-alphabetical filename; asserts compile.py either renames OR rejects with a warning | `python -m unittest scripts.test_connections.test_connections_naming_convention` | R3 | N |

### T2.B — Index + log integration

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T2.B.1 | When a new connection article is created, `knowledge/index.md` gets a `[[connections/...]]` row | `grep "connections/" knowledge/index.md \| wc -l` returns ≥1 after smoke run | R3 | Y |
| T2.B.2 | When a new connection article is created, `knowledge/log.md` gets a timestamped entry mentioning the connected concept slugs | `grep -A2 "connection" knowledge/log.md` shows a recent entry with two `[[concepts/...]]` references | R3 | N |

---

## T3 — QA loop

### T3.A — SessionStart trigger

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T3.A.1 | `hooks/session-start.py` reads the most recent daily log; extracts user-line questions (regex: line starts with `**User:**` and contains `?`) | `grep -n "User.*\\?\|extract_questions" hooks/session-start.py` | R4 | Y |
| T3.A.2 | For each extracted question (cap 3 per session per CHECKPOINT-A Q4 default), spawn `scripts/query.py "<question>" --output knowledge/qa/<hash>.md` as fire-and-forget Popen | `grep -n "query.py" hooks/session-start.py` | R4 | Y |
| T3.A.3 | SessionStart hook latency stays under 1s wall-clock with 3 fire-and-forget query.py Popens (Python startup + 3 spawns) | `time (echo '{}' \| uv run python hooks/session-start.py > /dev/null) 2>&1 \| awk '/real/ {print; if ($2+0 > 1) exit 1}'` exit 0 | R4 | Y |

### T3.B — query.py output format

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T3.B.1 | `scripts/query.py` accepts `--output <path>` flag; writes a markdown file with question, answer, citations | `python scripts/query.py "test question" --output /tmp/test-qa.md && test -s /tmp/test-qa.md` | R4 | Y |
| T3.B.2 | Output file has YAML frontmatter with `question_hash`, `asked_at`, `confidence` | `head -10 /tmp/test-qa.md \| grep -E "(question_hash\|asked_at\|confidence)"` returns 3 matches | R4 | Y |
| T3.B.3 | Question hash is SHA256-16 of the question string; QA loop is idempotent (same question = same filename, skipped if exists) | `python scripts/query.py "test question" --output /tmp/test-qa.md` second run shows "already answered, skipping" | R4 | Y |
| T3.B.4 | Test `test_query_output_format` covers the schema | `python -m unittest scripts.test_qa_loop.test_query_output_format` | R4 | Y |
| T3.B.5 | Test `test_qa_loop_idempotency` covers re-running on same question | `python -m unittest scripts.test_qa_loop.test_qa_loop_idempotency` | R4 | Y |

---

## T4 — Structural lint pre-pass

### T4.A — Frontmatter validation

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T4.A.1 | `tools/lint_kb.py` is a new lint script (~150-200 LOC) that walks `knowledge/concepts/` AND `knowledge/connections/` | `test -f tools/lint_kb.py` | R5 | Y |
| T4.A.2 | Lint reports articles WITHOUT a `---` YAML frontmatter block at top of file | grep test: `tools/lint_kb.py --check frontmatter knowledge/concepts/` exit 0 OR lists violators | R5 | Y |
| T4.A.3 | Lint reports articles with malformed frontmatter (missing closing `---`, bad indent) | covered by T4.A.2 + canary test | R5 | Y |

### T4.B — Dead-link detection

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T4.B.1 | Lint extracts all `[[wikilinks]]` from every article; asserts each link target exists on disk under `knowledge/` | `tools/lint_kb.py --check dead-links knowledge/concepts/` reports any `[[concepts/foo]]` whose `foo.md` doesn't exist | R5 | Y |
| T4.B.2 | Canary test: temporarily add `[[concepts/does-not-exist]]` to a test article; lint exits 1; revert | `python -m unittest scripts.test_lint_kb.test_dead_link_detection` | R5 | Y |

### T4.C — Orphan article detection

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T4.C.1 | Lint detects articles with ZERO inbound `[[wikilinks]]` from any other article AND not mentioned in `knowledge/index.md` | `tools/lint_kb.py --check orphans knowledge/concepts/` | R5 | Y |
| T4.C.2 | Orphans are reported as WARN (not FATAL) — they may be intentionally standalone | exit code 0 with warnings; FATAL only for frontmatter + dead-links | R5 | Y |
| T4.C.3 | Test `test_orphan_detection` covers a synthetic 3-article KB with one orphan | `python -m unittest scripts.test_lint_kb.test_orphan_detection` | R5 | Y |

### T4.D — Integration with existing scripts/lint.py

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T4.D.1 | `scripts/lint.py` is modified to invoke `tools/lint_kb.py` as a PRE-PASS before its existing LLM checks; aborts with FATAL exit if `lint_kb.py` finds frontmatter/dead-link errors | `grep -n "lint_kb" scripts/lint.py` | R5 | Y |
| T4.D.2 | Canary cost check: `scripts/lint.py` invocation with no errors costs ~0 (pre-pass is deterministic) | observed by user; no automated check | R5 | N |

### T4.E — Canary test fixture (4 enumerated canaries for T-FINAL.3)

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T4.E.1 | Canary 1: missing-frontmatter article → lint_kb.py exits non-zero with "missing frontmatter" message | `python -m unittest scripts.test_lint_kb.test_canary_missing_frontmatter` | R5 | Y |
| T4.E.2 | Canary 2: malformed frontmatter (unclosed `---`) → lint_kb.py exits non-zero with "malformed frontmatter" message | `python -m unittest scripts.test_lint_kb.test_canary_malformed_frontmatter` | R5 | Y |
| T4.E.3 | Canary 3: dead `[[concepts/does-not-exist]]` wikilink → lint_kb.py exits non-zero | `python -m unittest scripts.test_lint_kb.test_canary_dead_wikilink` | R5 | Y |
| T4.E.4 | Canary 4: orphan article (zero inbound links, not in index) → lint_kb.py prints WARN (exit 0 still) | `python -m unittest scripts.test_lint_kb.test_canary_orphan` | R5 | Y |

---

## T-FINAL — Acceptance test

Each T-FINAL sub-row has its own discrete `--check <name>` subcommand so a failure on one dimension does not hide the others. Per-check exit codes preserve diagnostic value.

| ID | Assertion | Verify | Owner | Blocking |
|---|---|---|---|---|
| T-FINAL.1 | `tools/check_followups_v1.py` exists; supports `--check polish`, `--check structural`, `--check tests`, `--check lints`, `--check all` (default) | `test -f tools/check_followups_v1.py && python tools/check_followups_v1.py --help \| grep -E "(polish\|structural\|tests\|lints\|all)"` | R-FINAL | Y |
| T-FINAL.2 | `--check polish` confirms all 6 T0 items shipped (RotatingFileHandler in flush+drain, FLUSH_ORIGINAL_SECTION env, cost-budget gate in drain.py, timespan script exists, flock cross-process test passes) | `python tools/check_followups_v1.py --check polish` exit 0 | R-FINAL | Y |
| T-FINAL.3 | `--check structural` confirms 3 structural-gap items shipped (T2 connections governance present in compile.py prompt, T3 QA loop wired in SessionStart, ≥3 articles with `evidence:` block) AND lint_kb canaries (4) fire as expected | `python tools/check_followups_v1.py --check structural` exit 0 | R-FINAL | Y |
| T-FINAL.4 | `--check tests` runs `python -m unittest scripts.test_classifier scripts.test_dedup scripts.test_drain scripts.test_lint_kb scripts.test_connections scripts.test_qa_loop scripts.test_provenance scripts.test_flush_error_format scripts.test_utils_state scripts.test_log_rotation` | `python tools/check_followups_v1.py --check tests` exit 0 | R-FINAL | Y |
| T-FINAL.5 | `--check lints` runs `tools/lint_classifier.py` AND `tools/lint_kb.py knowledge/concepts/` AND verifies both clean | `python tools/check_followups_v1.py --check lints` exit 0 | R-FINAL | Y |
| T-FINAL.6 | Dogfood: `scripts/compile.py --file daily/<recent>.md` runs successfully; user signs off at CHECKPOINT-D that observed cost is within their tolerance (historical baseline $4.48-$11.88; record actual cost in `status/dogfood-evidence.md` per R1 recommended fix) | manual CHECKPOINT-D signoff; cost recorded in `status/dogfood-evidence.md` | R0 | Y |

---

## Row count (post-R1-adversarial-review)

- T0 polish: 4 + 3 + 4 + 5 + 2 + 2 = 20 rows (added T0.C.0a)
- T1 provenance: 3 + 4 + 4 + 1 = 12 rows (added T1.D.1 stale-doc fix)
- T2 connections: 5 + 2 = 7 rows (added T2.A.5 naming-convention test)
- T3 QA loop: 3 + 5 = 8 rows
- T4 lint pre-pass: 3 + 2 + 3 + 2 + 4 = 14 rows (added T4.E.1-4 canary tests)
- T-FINAL: 6 rows
- **Total: 67 rows** (in the 60-150 target band).

Blocking rows: 57. Non-blocking (nice-to-have): 10.

---

## Acceptance command

```bash
cd /home/faxik/tools/claude-memory-compiler && python tools/check_followups_v1.py
```

Exit 0 + CHECKPOINT-D user signoff = sprint shipped.
