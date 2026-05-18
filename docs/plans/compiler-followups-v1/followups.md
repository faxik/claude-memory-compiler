# Compiler-followups-v1 — Followups

Two distinct routing buckets per `autonomous-sprint` skill (W4 fix from R1 adversarial review):

## Adversarial review dispositions (this sprint's pre-flight)

Brief-level fixes from the R1 adversarial review on 2026-05-18. Each finding gets a disposition: FIXED, DEFERRED, WONTFIX. See `review/r1-judge.md` for full verdicts.

| Finding | Severity | Disposition | Evidence |
|---|---|---|---|
| F1 (compile-cost retired claim) | FATAL | FIXED in brief Problem-statement rewrite | `00-problem-brief.md:9-13`; pre-flight SoT verification table |
| F2 (connections dir empty claim) | FATAL | FIXED — re-derived from `ls knowledge/connections/` | `00-problem-brief.md` §"Known context" + SoT table |
| F3 (pass-2 prompt claim) | FATAL | FIXED — Known-context says "single LLM prompt, single query() call" | `00-problem-brief.md` §"Known context" |
| F4 (T0.C undeliverable) | SERIOUS | FIXED via T0.C.0a (UNVERIFIED annotation day-1 path) | `COMPLETION-CHECKLIST.md` T0.C section rewrite |
| F5 (broken Verify greps) | SERIOUS | FIXED in T0.A.3, T1.A.2, T3.B.2 + checklist preamble note | `COMPLETION-CHECKLIST.md` preamble + 3 row fixes |
| F6 (budget math) | FATAL | FIXED — T0.D.3 cap $15, T-FINAL.6 user-tolerance, §6 cap $25 | `00-problem-brief.md` Constraints #4; `team-of-agents-v1.md` §6 |
| S1 (waived checkpoint artifact) | SERIOUS | WONTFIX — bounded tooling deferral, compliance path preserved | defender DEFEND verdict |
| S2 (T2→T1 dep fictional) | SERIOUS | FIXED — T2 moved to Wave 0; handshake protocol updated | brief Sequencing + team-of-agents §4 |
| S3 (T-FINAL owned by R0) | SERIOUS | FIXED — added R-FINAL Sonnet role | `team-of-agents-v1.md` §1 |
| S4 (T-FINAL aggregated exit) | SERIOUS | FIXED — per-check subcommands `--check polish/structural/tests/lints` | `COMPLETION-CHECKLIST.md` T-FINAL rewrite |
| S5 ("modified this sprint" undefined) | SERIOUS | FIXED — drop the qualifier; T1.B.2 asserts ALL articles after T1.C backfill | `COMPLETION-CHECKLIST.md` T1.B.2 |
| S6 (folded into F6) | — | FIXED with F6 | — |
| W1 (gap #3 erased) | RECOMMENDED | FIXED — brief notes 5 May-13 gaps, gap #3 explicitly out-of-scope | `00-problem-brief.md` Problem-statement + Out-of-scope |
| W2 (no real-LLM acceptance) | RECOMMENDED | FIXED via R1 R-FINAL CHECKPOINT-D dogfood-evidence requirement | `team-of-agents-v1.md` §5 + CHECKPOINT-D row |
| W3 (T3.A.3 100ms ceiling) | RECOMMENDED | FIXED — relaxed to <1s wall | `COMPLETION-CHECKLIST.md` T3.A.3 |
| W4 (followups bucket ambiguity) | RECOMMENDED | FIXED — this file has 2 H2 sections | this file |
| W5 (lint canaries undefined) | RECOMMENDED | FIXED — T-FINAL.3 enumerates 4 canaries | `COMPLETION-CHECKLIST.md` T-FINAL.3 + new T4.E rows |
| W6 (claim_summary mismatch) | DISMISSED | per defender | — |
| W7 (concurrency math) | RECOMMENDED | FIXED — §7 peak-concurrency math added | `team-of-agents-v1.md` §7 |
| N1 (row count math) | DISMISSED | — | — |
| N2 (forward-ref) | NITPICK | FIXED — sequencing note added | brief Sequencing |
| N3 (term unspecified) | DISMISSED | — | — |
| N4 (wave sequencing) | DISMISSED | — | — |
| N5 (budget-meter bootstrap) | NITPICK | FIXED — §7 Bootstrap step added | `team-of-agents-v1.md` §7 |

---

## Execution-time discoveries (filled during sprint)

Items surfaced during Wave 0..3 execution that need user approval before being added to the codesweep or rolled to followups-v2.

### Pre-populated for sprint open

- **FU-FU1-T0-C** — After ≥7d Slice-1 telemetry accumulates in `flush.log`, re-run `tools/check_classifier_groundedness.py --promote` and tighten `auth_invalid` / `prompt_too_long` regexes based on real-world stderr signatures observed. Target: followups-v2 brief.
- **FU-FU1-COMPILE-COST** — May-13 retro gap #3 (per-article compile cost; current mean $4.48/run). The previously-attempted multi-pass dedup design was REJECTED by a 2-round adversarial review for repeating the FATAL-1 type-based-retry-whitelist bug class. Requires fresh design council. Target: dedicated cost-reduction sprint, NOT followups-v2 unless explicitly user-prioritized.
- **FU-FU1-CONCEPT-DOC-STALE** — `knowledge/concepts/claude-memory-compiler-setup.md` is the source-of-truth article that gave rise to 3 FATAL claims in this sprint's pre-flight. T1.D in this sprint updates it; but a recurring "concept docs are aspirational, not state-of-record" pattern remains. Consider adding a per-article `last_verified_at` field as a future provenance schema extension.
