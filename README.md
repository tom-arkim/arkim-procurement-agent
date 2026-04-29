# Arkim Procurement Agent — Prototype

AI-powered industrial procurement assistant. Ingests a photo or text description
of a failed asset, runs multi-tier vendor discovery, and produces a ranked
Arkim-branded quote with TCA/TLV scoring.

---

## Architecture

```
chat_app.py (Streamlit UI)
    │
    ├── utils/vision.py          Phase 0 — photo → AssetSpecs (Claude vision)
    ├── utils/sourcing.py        Tier 1/2/3 vendor discovery
    │       ├── Tier 1/1.5      Dynamic Tavily search + authority scoring
    │       │                   Fallback: hardcoded _VENDOR_DOMAINS if <3 viable
    │       ├── Tier 2          National specialist discovery (open Tavily + Claude)
    │       └── Tier 3          Managed RFQ outreach — email drafts per vendor
    │
    ├── utils/brand_intelligence.py  Phase 3.1 — LLM manufacturer relationship graph
    ├── utils/spec_lookup.py         Phase 3.4 — LLM equipment spec enrichment
    ├── utils/quoting.py             TCA/TLV scoring + Arkim fee calculation
    ├── utils/audit_log.py           SQLite audit trail
    ├── utils/supplier_registry.py   Supplier onboarding registry
    ├── utils/llm_tracker.py         LLM call cost tracking
    └── utils/price_db.py            Price cache (JSON, upgradeable to Postgres)
```

### Workflow modes
| Mode | Trigger | Tier 1/2? | Ranking |
|------|---------|-----------|---------|
| spare_parts | Default | Yes | TCA (speed 35%, reliability 25%, friction 20%, cost 20%) |
| replacement | Equipment swap | Yes | TLV (purchase + downtime risk + shipping + tax) |
| capex | New purchase | No (Tier 3 only) | TLV |

### Urgency factor
`0.0` = Stocking (ignore downtime cost), `0.3` = Predictive (default), `1.0` = Emergency.
At ≥ 0.8, TCA re-weights: Speed 50%, Cost 5% — fastest vendor wins even at a premium.

---

## Data Model

### SQLite databases (in `data/`)

**`audit_log.sqlite`** — one row per sourcing run
| Column | Type | Notes |
|--------|------|-------|
| sourcing_run_id | TEXT | UUID, matches ArkimQuote |
| input_summary | TEXT | e.g. "Bearing for Bellatrx conveyor" |
| vendors_surfaced | JSON | all vendors shown in UI |
| final_recommendation | TEXT | top-ranked vendor name |
| user_selection | TEXT | set on Accept Offer |
| urgency_factor_used | REAL | 0.0–1.0 |
| llm_calls_made | INTEGER | total API calls |
| estimated_llm_cost_usd | REAL | estimated cost |
| duration_ms | INTEGER | end-to-end ms |

**`supplier_registry.sqlite`** — one row per known supplier
| Column | Type | Notes |
|--------|------|-------|
| name | TEXT | canonical vendor name |
| domain | TEXT | e.g. "grainger.com" |
| onboarding_status | TEXT | discovery_only / invited / onboarded_arkim_supplier |
| vendor_authorization_status | TEXT | Authorized / Unauthorized / Unknown |
| contact_email | TEXT | populated on outreach |

**`brand_intelligence.sqlite`** — Phase 3.1 manufacturer relationship graph
| Column | Type | Notes |
|--------|------|-------|
| manufacturer | TEXT | lowercase, cache key |
| equipment_type | TEXT | lowercase, cache key |
| parent_company | TEXT | e.g. "ruthman" for Gusher/Nagle |
| common_competitors | JSON | competing brands for query injection |
| subcategory_niche_terms | JSON | Tier 2 specialist search terms |
| wrong_category_terms | JSON | niche exclusion terms for suitability guardrail |
| ttl_days | INTEGER | default 90 — re-discovered after expiry |

**`spec_cache.sqlite`** — Phase 3.4 equipment spec enrichment cache
| Column | Type | Notes |
|--------|------|-------|
| manufacturer | TEXT | cache key |
| model | TEXT | cache key |
| enriched_json | JSON | inferred voltage, rpm, frame, phase, hp, etc. |

### Key constants (quoting.py)
| Constant | Value | Meaning |
|----------|-------|---------|
| ARKIM_PROCESSING_FEE_RATE | 3.5% | flat fee on (vendor base + shipping) |
| BROADER_OUTREACH_MIN_MATCH_SCORE | 25.0 | minimum suitability to show in Tier 3 |
| MCS_LABOR_THRESHOLD | 5.0 | below this MCS triggers labor surcharge |
| LABOR_RATE_PER_HOUR | $200 | for labor impact calculation |

---

## CLI Tools

All scripts run from the repo root: `python scripts/<script>.py`

| Script | Purpose |
|--------|---------|
| `scripts/inspect_audit_log.py` | Browse recent sourcing runs, filter by run ID |
| `scripts/manage_suppliers.py` | List, show, update, seed supplier registry |
| `scripts/refresh_brand_intel.py` | Warm/invalidate brand intelligence cache |
| `scripts/test_dynamic_discovery.py` | Phase 3.3 Tavily authority scoring benchmark |

### inspect_audit_log
```
python scripts/inspect_audit_log.py [--limit N] [--run-id UUID] [--verbose] [--json]
```

### manage_suppliers
```
python scripts/manage_suppliers.py list
python scripts/manage_suppliers.py show "Gusher Pumps"
python scripts/manage_suppliers.py update "Gusher Pumps" --status onboarded_arkim_supplier
python scripts/manage_suppliers.py seed
```

### refresh_brand_intel
```
python scripts/refresh_brand_intel.py list
python scripts/refresh_brand_intel.py warm                          # pre-warm 10 common pairs
python scripts/refresh_brand_intel.py refresh Gusher pump
python scripts/refresh_brand_intel.py invalidate Baldor motor
```

---

## Setup

```bash
pip install -r requirements.txt
export TAVILY_API_KEY=tvly-...
export ANTHROPIC_API_KEY=sk-ant-...
streamlit run chat_app.py
```

### Environment variables
| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| TAVILY_API_KEY | Yes | — | Tavily web search |
| ANTHROPIC_API_KEY | Yes | — | Claude LLM calls |
| OS_EXTRACTION_MODEL | No | claude-haiku-4-5-20251001 | Tier 1/2 extraction model |
| BRAND_INTEL_MODEL | No | claude-haiku-4-5-20251001 | Brand intelligence discovery |
| SPEC_ENRICH_MODEL | No | claude-haiku-4-5-20251001 | Spec enrichment model |
| SHOW_ADMIN_VIEW | No | True (in app) | Controls admin tab visibility |

---

## Phase History

| Phase | Features |
|-------|----------|
| 0 | Vision extraction (photo → AssetSpecs), multi-tier search, TCA quote |
| 1.5 | OEM Direct badge, unified contact resolution, Tier 3 RFQ, supplier registry |
| 2 | Urgency factor, warranty gate, audit log, LLM cost tracking |
| 3.1 | Brand intelligence module (LLM-discovered manufacturer graph, SQLite cache) |
| 3.2 | Replaced 4 hardcoded dicts in sourcing.py with brand intelligence calls |
| 3.3 | Dynamic Tier 1 discovery: unrestricted Tavily + vendor authority scoring |
| 3.4 | Spec enrichment: LLM infers voltage/rpm/frame/phase from manufacturer+model |
