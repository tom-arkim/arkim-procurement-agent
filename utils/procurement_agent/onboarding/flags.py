"""
utils/procurement_agent/onboarding/flags.py

Feature-flag gate for the Night 4 Onboarding Agent.

The onboarding agent (URL → harvest → extract → prepopulate → concierge approve
→ onboarded supplier) is the supplier-intake companion to Night 3's TIER1_V2
supplier-scope registry. It **extends** Night 3: it only makes sense when the
TIER1_V2 supplier-scope surface is live (it writes scope via
``set_supplier_classes`` / ``set_supplier_brands`` / ``set_supplier_territory``
/ ``set_supplier_verticals`` and drives the ``tier1_transition`` state machine,
all of which no-op when TIER1_V2 is off).

So the onboarding gate is **TIER1_V2 itself** — re-exported here as
``ONBOARDING_ENABLED`` so call sites read intent, not mechanism. When TIER1_V2
is off, every onboarding entry point is dormant:

  - the harvester is not invoked from the API (endpoints 503, dormant — never
    404-ish pretend-not-here),
  - no draft is created,
  - no registry scope write or lifecycle transition fires.

This keeps the flag-off API and registry byte-identical to pre-Night-4 (the
inertness wall, asserted in tests).

Strict truthy parse mirrors the house convention (``_env_truthy`` in
api_server.py / supplier_registry.py / scoring.py / run_labels.py): only
"1/true/yes/on" enables; everything else -> OFF, fails safe/closed.
"""
from __future__ import annotations

import os
from typing import Optional


def _env_truthy(value: Optional[str]) -> bool:
    """Strict opt-in: only an explicit truthy token is True. Fails safe/closed."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


# The onboarding agent extends Night 3's TIER1_V2 supplier-scope redesign — it
# cannot function without that surface, so it shares the flag. Re-read at import
# (tests monkeypatch this module's ONBOARDING_ENABLED, mirroring how Night 3
# tests monkeypatch supplier_registry.TIER1_V2).
def _resolve_enabled() -> bool:
    """Resolve the onboarding gate from the TIER1_V2 env flag.

    Read live from os.environ so a test that sets TIER1_V2 via monkeypatch.setenv
    is honored (mirrors run_labels.RUN_CAPTURE / supplier_registry.TIER1_V2
    import-time capture + the test pattern of monkeypatching the module attr).
    """
    return _env_truthy(os.environ.get("TIER1_V2"))


ONBOARDING_ENABLED: bool = _resolve_enabled()


def is_enabled() -> bool:
    """Live check (honors monkeypatched os.environ). Use at API entry points."""
    return _env_truthy(os.environ.get("TIER1_V2"))
