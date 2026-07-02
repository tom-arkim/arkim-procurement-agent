"""
LangSmith tracing client — the procurement slice of the observability layer (T7).

Implements messaging's INVESTIGATED pattern (no re-derivation):
  - NO LangGraph auto-tracing; MANUAL `ls.trace()` context managers everywhere.
  - The only env var is LANGSMITH_API_KEY (endpoint hardcoded
    https://api.smith.langchain.com — never read from env).
  - Enablement is PROGRAMMATIC via `tracing_context(enabled=True, project_name=…,
    tags=…, metadata=…)`.
  - Project name: f"Arkim Procurement ({environment})" — a SIBLING project to
    messaging's "Arkim Assistant ({environment})" (decided).
  - Root spans run_type="chain"; nested LLM spans run_type="llm" with
    ls_model_name + usage metadata.
  - Children attach to the root via contextvar propagation (ls.trace handles
    this when used inside a tracing_context / parent trace).

ENV-GATED, FAIL-SOFT, OFFLINE-FIRST:
  - With LANGSMITH_API_KEY unset, EVERYTHING here is inert and offline: no
    network, no exception, no-op context managers. Proven by
    test_langsmith_client.py::test_offline_inert.
  - A missing `langsmith` import (dep not installed) also degrades to inert
    no-ops — so the intake pipeline never crashes if the dep is absent.

This module is the ONLY place procurement code touches LangSmith. The intake
agent routes its requests.post calls + classify_part_type through `traced_llm`
(T7 wiring), and opens a root `trace()` around intake run() carrying run_id in
metadata (the cross-HTTP-boundary filter convention).

The sourcing pipeline is NOT instrumented tonight (named, supervised follow-up —
recorded in the morning report).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

# ---------------------------------------------------------------------------
# Lazy, optional langsmith import — the whole module degrades to inert no-ops
# if langsmith isn't installed or the key is absent.
# ---------------------------------------------------------------------------

_LANGSMITH_ENDPOINT = "https://api.smith.langchain.com"  # hardcoded — never from env

_ls: Any = None
_ls_import_error: Optional[BaseException] = None

try:  # pragma: no cover - exercised via offline-inert test (key unset path)
    import langsmith as _langsmith_mod  # noqa: F401
    _ls = _langsmith_mod
except Exception as exc:  # pragma: no cover
    _ls_import_error = exc


def _environment() -> str:
    """Read ENVIRONMENT (default 'dev'). Used for project name + tags."""
    return (os.environ.get("ENVIRONMENT") or "dev").strip() or "dev"


def _api_key() -> str:
    return (os.environ.get("LANGSMITH_API_KEY") or "").strip()


def tracing_enabled() -> bool:
    """True iff langsmith imported AND LANGSMITH_API_KEY is set. Read at call time."""
    return _ls is not None and bool(_api_key())


# ---------------------------------------------------------------------------
# Project / tags / metadata conventions
# ---------------------------------------------------------------------------

_VERSION = "intake-redesign-overnight"


def project_name() -> str:
    """The LangSmith project name — a sibling to messaging's 'Arkim Assistant'."""
    return f"Arkim Procurement ({_environment()})"


def _default_tags() -> list:
    return [_environment(), f"v{_VERSION}"]


# ---------------------------------------------------------------------------
# Root trace — a manual ls.trace() context manager wrapping intake run().
# Offline-inert: when disabled, yields None and does nothing.
# ---------------------------------------------------------------------------

@contextmanager
def trace(name: str, *, run_id: Optional[str] = None,
          inputs: Optional[Dict[str, Any]] = None,
          metadata: Optional[Dict[str, Any]] = None,
          tags: Optional[list] = None) -> Iterator[Any]:
    """Open a root LangSmith trace around a unit of work.

    Carries `run_id` in metadata (the cross-HTTP-boundary filter convention —
    every intake trace is filterable by run_id). When tracing is disabled, this
    is a no-op context manager (yields None, no network, no exception).
    """
    if not tracing_enabled():
        yield None
        return
    meta: Dict[str, Any] = dict(metadata or {})
    if run_id:
        meta["run_id"] = run_id
    try:
        with _ls.trace(  # type: ignore[union-attr]
            name,
            run_type="chain",
            project_name=project_name(),
            inputs=inputs,
            metadata=meta,
            tags=tags or _default_tags(),
        ) as rt:
            yield rt
    except Exception as exc:  # pragma: no cover - fail-soft, never raise into intake
        print(f"[langsmith_client] root trace failed (degraded to inert): {exc}")
        yield None


# ---------------------------------------------------------------------------
# traced_llm — wrap a single LLM call as a nested 'llm' span with model +
# usage metadata. Mirrors messaging's traced_invoke.
#
# Usage:
#   with trace("intake run", run_id=run_id) as root:
#       text = traced_llm("extraction", model="claude-haiku-4-5-20251001",
#                         parent=root, inputs={...}, output=raw_text)
# ---------------------------------------------------------------------------

@contextmanager
def traced_llm(name: str, *, model: str,
               parent: Any = None,
               inputs: Optional[Dict[str, Any]] = None,
               metadata: Optional[Dict[str, Any]] = None,
               tags: Optional[list] = None) -> Iterator[Any]:
    """Open a nested 'llm' span. The caller performs the actual HTTP call inside
    the `with` block and can attach usage metadata to the yielded run tree via
    `add_metadata`/`add_usage` if available. Offline-inert when disabled."""
    if not tracing_enabled():
        yield None
        return
    meta: Dict[str, Any] = dict(metadata or {})
    meta["ls_model_name"] = model
    try:
        kwargs: Dict[str, Any] = dict(
            run_type="llm",
            project_name=project_name(),
            inputs=inputs,
            metadata=meta,
            tags=tags or _default_tags(),
        )
        if parent is not None:
            kwargs["parent"] = parent
        with _ls.trace(name, **kwargs) as llm_run:  # type: ignore[union-attr]
            yield llm_run
    except Exception as exc:  # pragma: no cover - fail-soft
        print(f"[langsmith_client] traced_llm failed (degraded to inert): {exc}")
        yield None


def record_llm_output(run: Any, output: Any, *, usage: Optional[Dict[str, Any]] = None) -> None:
    """Attach the LLM call's output text + usage metadata to its span, if the
    span is real. No-op when run is None (offline/inert). Fail-soft."""
    if run is None:
        return
    try:
        if hasattr(run, "add_outputs"):
            run.add_outputs({"output": output})
        if usage and hasattr(run, "add_metadata"):
            run.add_metadata({"usage": usage})
    except Exception as exc:  # pragma: no cover
        print(f"[langsmith_client] record_llm_output failed: {exc}")
