#!/usr/bin/env bash
# Deploy gate: verify ≥N days of Slice-1 enriched FLUSH_ERROR telemetry
# have accumulated in scripts/flush.log before promoting speculative
# classifier rules out of UNVERIFIED status.
#
# Usage:
#   tools/check_telemetry_span.sh           # default 7d
#   tools/check_telemetry_span.sh 14        # require 14d
#
# Exit codes:
#   0  — span meets or exceeds the threshold
#   1  — span below threshold (or zero entries)
#   2  — log file missing
set -euo pipefail

THRESHOLD_DAYS="${1:-7}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/scripts/flush.log"

if [[ ! -f "$LOG" ]]; then
    echo "ERROR: $LOG not found" >&2
    exit 2
fi

# Slice 1 emits multi-line structured FLUSH_ERROR:
#   2026-05-18 09:42 ERROR FLUSH_ERROR: ProcessError | exit_code=1 | attempts=1
#     message: ...
#     stderr_tail:
#       ...
# We match the header line (which starts with a timestamp + LEVEL + "FLUSH_ERROR:").
FIRST=$(awk '/FLUSH_ERROR: .* exit_code=/ {print $1 " " $2; exit}' "$LOG")
LAST=$(awk '/FLUSH_ERROR: .* exit_code=/ {f=$1 " " $2} END {print f}' "$LOG")

if [[ -z "$FIRST" || -z "$LAST" ]]; then
    echo "Slice-1 enriched FLUSH_ERROR entries: 0" >&2
    echo "(Slice 1 may not have shipped yet, or no failures have occurred since)" >&2
    exit 1
fi

# Compute span in days. Python is the simplest cross-platform path here.
SPAN_DAYS=$(python3 -c "
from datetime import datetime
try:
    f = datetime.fromisoformat('$FIRST'.replace(' ', 'T'))
    l = datetime.fromisoformat('$LAST'.replace(' ', 'T'))
    print((l - f).days)
except Exception as e:
    print(0)
")

echo "first: $FIRST"
echo "last:  $LAST"
echo "span:  ${SPAN_DAYS} days (threshold: ${THRESHOLD_DAYS})"

if (( SPAN_DAYS >= THRESHOLD_DAYS )); then
    exit 0
else
    exit 1
fi
