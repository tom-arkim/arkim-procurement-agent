"""
utils/procurement_agent/onboarding/ — the Night 4 Onboarding Agent.

URL → harvest → extract → prepopulate supplier profile → concierge review/approve
→ writes an onboarded supplier via Night 3's TIER1_V2 supplier-scope registry.

Modules:
  - flags      — the TIER1_V2 gate (extends Night 3); flag-off = dormant.
  - dom        — stdlib HTML → DOM-pruned PageContent (no bs4/lxml dep).
  - harvester  — T1: URL → bounded same-domain pages + DOM-pruned text (SSRF-safe).
  - extractor  — T2: pages → structured OnboardingDraft (LLM, repo pattern) +
                 the must-confirm trio enforcement.
  - concierge  — T3: draft → review/approve → Night 3 registry scope write +
                 lifecycle onboarding→onboarded (approve-gated; nothing writes
                 without approve).

All behavior is behind TIER1_V2; flag-off is byte-identical (inertness asserted
in tests). New admin endpoints are additionally admin/token-gated via the
existing ``require_admin`` pattern (401/403/503 semantics).
"""
