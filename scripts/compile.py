"""
Compile daily conversation logs into structured knowledge articles.

This is the "LLM compiler" - it reads daily logs (source code) and produces
organized knowledge articles (the executable).

Usage:
    uv run python compile.py                    # compile new/changed logs only
    uv run python compile.py --all              # force recompile everything
    uv run python compile.py --file daily/2026-04-01.md  # compile a specific log
    uv run python compile.py --dry-run          # show what would be compiled
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import sys
from pathlib import Path

from config import AGENTS_FILE, CONCEPTS_DIR, CONNECTIONS_DIR, DAILY_DIR, KNOWLEDGE_DIR, SCRIPTS_DIR, now_iso
from utils import (
    file_hash,
    list_raw_files,
    list_wiki_articles,
    load_state,
    read_wiki_index,
    save_state,
)

# ── Paths for the LLM to use ──────────────────────────────────────────
ROOT_DIR = Path(__file__).resolve().parent.parent

# Quarantine a daily log after this many consecutive failed compile attempts.
# Manual `--file` invocations bypass the quarantine check.
MAX_COMPILE_ATTEMPTS = 2
LOCK_FILE = SCRIPTS_DIR / "compile.lock"


def _record_crash(state: dict, rel_path: str, cost: float, error: BaseException) -> None:
    """Persist a crashed compile attempt.

    Even when the LLM stream crashes after billing, the Anthropic charge is
    already debited. Capture the actual cost into `state["wasted_cost"]` and
    bump the per-file retry counter so the quarantine guard can kick in.
    """
    print(f"  Error: {error}")
    if cost > 0:
        print(
            f"  WARNING: '{rel_path}' compile crashed AFTER incurring "
            f"${cost:.4f} in API charges — file remains uncompiled but "
            "you have been billed."
        )
    state["wasted_cost"] = state.get("wasted_cost", 0.0) + cost
    attempts = state.setdefault("compile_attempts", {})
    attempts[rel_path] = attempts.get(rel_path, 0) + 1
    save_state(state)


def _record_success(state: dict, log_path: Path, cost: float) -> None:
    """Persist a successful compile and reset the retry counter."""
    rel_path = log_path.name
    state.setdefault("ingested", {})[rel_path] = {
        "hash": file_hash(log_path),
        "compiled_at": now_iso(),
        "cost_usd": cost,
    }
    state["total_cost"] = state.get("total_cost", 0.0) + cost
    attempts = state.setdefault("compile_attempts", {})
    if rel_path in attempts:
        attempts[rel_path] = 0
    save_state(state)


def select_files_to_compile(state: dict, all_logs: list[Path]) -> list[Path]:
    """Decide which daily logs to compile.

    Skips files whose stored hash matches the on-disk hash. Quarantines
    files that have failed MAX_COMPILE_ATTEMPTS times in a row (loud
    warning, no API call). Prints a one-line diagnostic whenever a
    previously-ingested file is re-picked, so future loops can be
    diagnosed from a single log scan.
    """
    to_compile: list[Path] = []
    ingested = state.get("ingested", {})
    attempts_map = state.get("compile_attempts", {})

    for log_path in all_logs:
        rel = log_path.name
        prev = ingested.get(rel, {})
        current_hash = file_hash(log_path)
        if prev and prev.get("hash") == current_hash:
            continue
        attempts = attempts_map.get(rel, 0)
        if attempts >= MAX_COMPILE_ATTEMPTS:
            print(f"  SKIP (quarantined after {attempts} failed attempts): {rel}")
            print(
                f"        Run `uv run python compile.py --file {rel}` "
                "to retry manually."
            )
            continue
        if prev:
            print(
                f"  RECOMPILE {rel}: hash {prev.get('hash')!r} -> "
                f"{current_hash!r} (last compiled {prev.get('compiled_at')})"
            )
        to_compile.append(log_path)

    return to_compile


async def compile_daily_log(log_path: Path, state: dict) -> float:
    """Compile a single daily log into knowledge articles.

    Returns the API cost of the compilation.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ResultMessage,
        TextBlock,
        query,
    )

    log_content = log_path.read_text(encoding="utf-8")
    schema = AGENTS_FILE.read_text(encoding="utf-8")
    wiki_index = read_wiki_index()

    # Read existing articles for context
    existing_articles_context = ""
    existing = {}
    for article_path in list_wiki_articles():
        rel = article_path.relative_to(KNOWLEDGE_DIR)
        existing[str(rel)] = article_path.read_text(encoding="utf-8")

    if existing:
        parts = []
        for rel_path, content in existing.items():
            parts.append(f"### {rel_path}\n```markdown\n{content}\n```")
        existing_articles_context = "\n\n".join(parts)

    timestamp = now_iso()

    prompt = f"""You are a knowledge compiler. Your job is to read a daily conversation log
and extract knowledge into structured wiki articles.

## Schema (AGENTS.md)

{schema}

## Current Wiki Index

{wiki_index}

## Existing Wiki Articles

{existing_articles_context if existing_articles_context else "(No existing articles yet)"}

## Daily Log to Compile

**File:** {log_path.name}

{log_content}

## Your Task

Read the daily log above and compile it into wiki articles following the schema exactly.

### Rules:

1. **Extract key concepts** - Identify 3-7 distinct concepts worth their own article
2. **Create concept articles** in `knowledge/concepts/` - One .md file per concept
   - Use the exact article format from AGENTS.md (YAML frontmatter + sections)
   - Include `sources:` in frontmatter pointing to the daily log file
   - Use `[[concepts/slug]]` wikilinks to link to related concepts
   - Write in encyclopedia style - neutral, comprehensive
3. **Create connection articles** in `knowledge/connections/` if this log reveals non-obvious
   relationships between 2+ existing concepts
4. **Update existing articles** if this log adds new information to concepts already in the wiki
   - Read the existing article, add the new information, add the source to frontmatter
5. **Update knowledge/index.md** - Add new entries to the table
   - Each entry: `| [[path/slug]] | One-line summary | source-file | {timestamp[:10]} |`
6. **Append to knowledge/log.md** - Add a timestamped entry:
   ```
   ## [{timestamp}] compile | {log_path.name}
   - Source: daily/{log_path.name}
   - Articles created: [[concepts/x]], [[concepts/y]]
   - Articles updated: [[concepts/z]] (if any)
   ```

### File paths:
- Write concept articles to: {CONCEPTS_DIR}
- Write connection articles to: {CONNECTIONS_DIR}
- Update index at: {KNOWLEDGE_DIR / 'index.md'}
- Append log at: {KNOWLEDGE_DIR / 'log.md'}

### Quality standards:
- Every article must have complete YAML frontmatter
- Every article must link to at least 2 other articles via [[wikilinks]]
- Key Points section should have 3-5 bullet points
- Details section should have 2+ paragraphs
- Related Concepts section should have 2+ entries
- Sources section should cite the daily log with specific claims extracted
"""

    cost = 0.0

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                cwd=str(ROOT_DIR),
                system_prompt={"type": "preset", "preset": "claude_code"},
                allowed_tools=["Read", "Write", "Edit", "Glob", "Grep"],
                permission_mode="acceptEdits",
                max_turns=30,
            ),
        ):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        pass  # compilation output - LLM writes files directly
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd or 0.0
                print(f"  Cost: ${cost:.4f}")
    except Exception as e:
        _record_crash(state, log_path.name, cost, e)
        return cost

    _record_success(state, log_path, cost)
    return cost


def main():
    parser = argparse.ArgumentParser(description="Compile daily logs into knowledge articles")
    parser.add_argument("--all", action="store_true", help="Force recompile all logs")
    parser.add_argument("--file", type=str, help="Compile a specific daily log file")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be compiled")
    args = parser.parse_args()

    # Single-instance lock: prevents parallel compile.py runs from racing
    # on state.json (the SessionEnd hook can spawn multiple in parallel).
    # The kernel releases the lock when the process exits, so no unlock needed.
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another compile.py is already running; exiting.")
        sys.exit(0)

    state = load_state()
    initial_wasted = state.get("wasted_cost", 0.0)

    # Determine which files to compile
    if args.file:
        target = Path(args.file)
        if not target.is_absolute():
            target = DAILY_DIR / target.name
        if not target.exists():
            # Try resolving relative to project root
            target = ROOT_DIR / args.file
        if not target.exists():
            print(f"Error: {args.file} not found")
            sys.exit(1)
        to_compile = [target]
    elif args.all:
        to_compile = list_raw_files()
    else:
        to_compile = select_files_to_compile(state, list_raw_files())

    if not to_compile:
        print("Nothing to compile - all daily logs are up to date.")
        return

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Files to compile ({len(to_compile)}):")
    for f in to_compile:
        print(f"  - {f.name}")

    if args.dry_run:
        return

    # Compile each file sequentially
    total_cost = 0.0
    for i, log_path in enumerate(to_compile, 1):
        print(f"\n[{i}/{len(to_compile)}] Compiling {log_path.name}...")
        cost = asyncio.run(compile_daily_log(log_path, state))
        total_cost += cost
        print("  Done.")

    articles = list_wiki_articles()
    print(f"\nCompilation complete. Total cost: ${total_cost:.2f}")
    run_wasted = state.get("wasted_cost", 0.0) - initial_wasted
    if run_wasted > 0:
        print(f"  ↳ ${run_wasted:.2f} of that was burned on crashed retries.")
    print(f"Knowledge base: {len(articles)} articles")


if __name__ == "__main__":
    main()
