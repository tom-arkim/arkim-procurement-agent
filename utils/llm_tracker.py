"""
Arkim LLM Cost Tracker — thin per-run token accounting.

Pricing (Claude Sonnet 4.6, 2026-04):
  Input  tokens : $3.00 / 1M
  Output tokens : $15.00 / 1M

Usage pattern:
  1. Pipeline start : start_run(run_id)   → sets the active run
  2. Each LLM call  : record_call(in, out) → credited to active run
  3. Pipeline end   : finish_run(run_id)  → returns final RunStats, clears tracker

The "active run" is stored in a threading.local so that Streamlit's per-session
threading model isolates concurrent user sessions correctly.
"""

import threading
from dataclasses import dataclass, field
from typing import Optional

# Claude Sonnet 4.6 — USD per 1 million tokens
_INPUT_COST_PER_1M  = 3.00
_OUTPUT_COST_PER_1M = 15.00


@dataclass
class RunStats:
    run_id:        str
    calls:         int = 0
    input_tokens:  int = 0
    output_tokens: int = 0

    @property
    def estimated_cost_usd(self) -> float:
        return round(
            (self.input_tokens  / 1_000_000) * _INPUT_COST_PER_1M
            + (self.output_tokens / 1_000_000) * _OUTPUT_COST_PER_1M,
            6,
        )

    def record(self, input_toks: int, output_toks: int) -> None:
        self.calls         += 1
        self.input_tokens  += int(input_toks)
        self.output_tokens += int(output_toks)


# Keyed by run_id — survives for the duration of the pipeline run
_trackers: dict[str, RunStats] = {}
_lock = threading.Lock()

# Thread-local holds the run_id that is "active" for this thread/session
_local = threading.local()


def start_run(run_id: str) -> RunStats:
    """Create a tracker for this run and mark it active on the current thread."""
    stats = RunStats(run_id=run_id)
    with _lock:
        _trackers[run_id] = stats
    _local.run_id = run_id
    return stats


def record_call(input_toks: int, output_toks: int) -> None:
    """Record one LLM call against the currently active run (no-op if none set)."""
    run_id = getattr(_local, "run_id", None)
    if run_id:
        with _lock:
            t = _trackers.get(run_id)
        if t:
            t.record(input_toks, output_toks)


def finish_run(run_id: str) -> RunStats:
    """Retrieve final stats and remove the tracker. Returns empty stats on miss."""
    with _lock:
        stats = _trackers.pop(run_id, RunStats(run_id=run_id))
    if getattr(_local, "run_id", None) == run_id:
        _local.run_id = None
    print(
        f"[LLMTracker] run={run_id[:8]} | calls={stats.calls} | "
        f"in={stats.input_tokens} out={stats.output_tokens} | "
        f"est. ${stats.estimated_cost_usd:.4f}"
    )
    return stats


def current_stats() -> Optional[RunStats]:
    """Return stats for the active run on this thread, or None."""
    run_id = getattr(_local, "run_id", None)
    if not run_id:
        return None
    with _lock:
        return _trackers.get(run_id)
