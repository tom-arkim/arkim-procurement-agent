# What's Next — Arkim Procurement Agent

*Generated 2026-04-28 after Phase 3 completion.*

---

## Current state

The prototype is fully functional end-to-end:
- Photo or text input → AssetSpecs via Claude vision
- Three-tier vendor discovery (Tier 1 dynamic + Tier 2 specialist + Tier 3 RFQ)
- TCA/TLV quote ranking with urgency weighting and warranty gating
- Audit log, supplier registry, brand intelligence cache, spec enrichment cache
- Streamlit UI with analytics, sourcing, history, and admin tabs
- Full audit trail for every sourcing run

---

## Highest-value next steps (in priority order)

### 1. Real supplier onboarding flow ($$$)
**What**: Implement the partner invitation → acceptance → "Direct Buy via Arkim" pipeline.
Currently `_VERIFIED_PARTNERS` is always empty, so no vendor ever gets the Direct Buy badge.
**How**:
- Email vendors whose `onboarding_status = invited` with a unique token link
- Accept form → `update_supplier(..., status="onboarded_arkim_supplier")`
- Populate `_VERIFIED_PARTNERS` from DB on startup
**Impact**: Unlocks the core Arkim margin model. First 2–3 onboarded suppliers make the product real.

### 2. Persistent customer session + CMMS integration
**What**: `asset_id` and `diagnostic_event_id` fields exist in AssetSpecs but are never populated.
**How**:
- Accept asset ID as input alongside the photo
- Pull historical maintenance history from a CMMS API (Fiix, UpKeep, etc.)
- Pre-populate failure_mode, warranty_status from CMMS data
**Impact**: Removes manual data entry; personalizes recommendation to asset history.

### 3. Real outbound RFQ email
**What**: `EMAIL_SEND_ENABLED = False` — the email drafts are generated but never sent.
**Requires**: Legal review of templates, CAN-SPAM compliance check, SMTP/Postmark integration.
**How**: When `EMAIL_SEND_ENABLED = True`, call `send_rfq_email(vendor, draft)` after user confirms.
**Impact**: Automates the most manual step in the current workflow.

### 4. Brand intelligence warm-up on startup
**What**: Currently brand intelligence is lazily discovered per query. Cold starts hit Claude.
**How**: On app startup, call `warm_cache(_WARM_PAIRS)` in a background thread if cache is empty.
**Impact**: Eliminates 2-3 second delay on first sourcing run for common equipment types.

### 5. Price DB upgrade to Postgres
**What**: `utils/price_db.py` uses flat JSON files. The comment in `requirements.txt` notes SQLAlchemy as the target ORM.
**How**: Add `sqlalchemy>=2.0` + `psycopg2-binary`, wrap each function with a SQLAlchemy session.
**Impact**: Enables concurrent users, proper indexing, and production-grade durability.

### 6. Spec enrichment confidence penalty wiring
**What**: `enrich_equipment_specs()` sets `specs._enriched_fields` but nothing reads it yet.
**How**: In `_compute_confidence_score()` in sourcing.py, subtract 10 pts per enriched field:
```python
enriched = getattr(specs, "_enriched_fields", set())
confidence -= 10 * len(enriched)
```
**Impact**: Properly reflects epistemic uncertainty when specs are LLM-inferred, not nameplate-read.

### 7. Dynamic discovery benchmark and tuning
**What**: `scripts/test_dynamic_discovery.py` tests 20 samples but hasn't been run against live data.
**How**: Run `python scripts/test_dynamic_discovery.py` with real API keys; tune `_AUTHORITY_VIABLE_THRESHOLD`
based on results (currently 30.0).
**Impact**: Ensures Tier 1 still surfaces known vendors while allowing new ones to surface organically.

---

## Deliberately deferred (do not build)
- Streamlit replacement (FastAPI / Next.js)
- API surface / multi-tenant
- Stripe billing
- OEM direct sales integration
- Datadog / Sentry
- Anthropic SDK migration (currently uses raw HTTP)
- Comprehensive retroactive test suite
- Chat_app.py refactor / session state machine

---

## LLM cost model

### Per sourcing run (typical spare_parts query)
| Call | Model | Tokens (est.) | Cost (est.) |
|------|-------|---------------|-------------|
| Vision extraction | claude-haiku-4-5 | ~800 in + ~300 out | ~$0.001 |
| Tier 1 LLM parse (3 batches x 5 results) | claude-haiku-4-5 | ~600 in + ~200 out each | ~$0.003 |
| Tier 2 specialist extraction | claude-haiku-4-5 | ~500 in + ~150 out | ~$0.001 |
| Market confidence score | claude-haiku-4-5 | ~400 in + ~50 out | ~$0.001 |
| Brand intelligence (cache miss only) | claude-haiku-4-5 | ~300 in + ~200 out | ~$0.001 |
| Spec enrichment (cache miss only) | claude-haiku-4-5 | ~200 in + ~100 out | ~$0.001 |
| **Total per run (cold)** | | ~3,500 tokens | **~$0.008** |
| **Total per run (warm cache)** | | ~2,000 tokens | **~$0.004** |

*Haiku pricing: $0.80/M input, $4.00/M output. Estimates based on code structure; actual costs visible in `inspect_audit_log.py --verbose`.*

### Cumulative cost at scale
| Volume | Est. cost |
|--------|-----------|
| 100 runs/month (warm) | ~$0.40/month |
| 1,000 runs/month | ~$4–8/month |
| Tavily (20 searches/run) | $0.004/run at Advanced tier |

The dominant cost driver at scale is Tavily, not Claude.
