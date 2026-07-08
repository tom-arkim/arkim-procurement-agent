# Night 5 — Tier 1 Runtime Investigation (I1–I4)

Read-only investigation, reported before any code changes per guardrail 8.
HEAD: `4cc06be` on `test/flag-on-integration`; branch `feature/tier1-runtime-overnight`.
Working tree: clean of deleted/modified tracked files (only untracked stale briefs/audit docs).

Baseline suite: **1740 passed / 73 skipped** (full `uv run pytest -q`).
§7.7 flaky-under-load pair (`test_orchestrator.py` + `test_persistence.py`): green this run
(29/29 when run explicitly). Not touched this night.

---

## I1 — Tier 1 path today + frontend candidate contract

**Post-purge `_run_tier1` behavior (CONFIRMED EXPECTED — honest-empty):**
`utils/procurement_agent/agents/sourcing_agent.py:509`. It reads
`data/mock_tier1_suppliers.json` (now `{"suppliers": []}` — fabricated vendors purged at
the source), loops suppliers×inventory matching on normalized PN or manufacturer substring,
and on zero hits falls through to `_seeded_tier1_candidates` which **returns `[]` always**
(`:1214` — permanently disabled). So Tier 1 is `[]` for any specs in all modes. DEMO_MODE
short-circuits the empty read (belt-and-suspenders, no behavior change). The result is
ranked via `_rank` (TCA score under urgency weights) and returned. `_seeded_tier3_candidates`
(untouched) still surfaces genuine brand-intel anchors in Tier 3.

**Downstream consumers of `tier_1`:**
1. **api_server `_transform_sourcing_results` (`:970`)** → `_tier("tier_1", 1)` →
   `_transform_option(o, 1, i, quote=...)` per candidate (skips `rejection_reason` rows).
2. **api_server `_run_sourcing_background` (`:1092`)**: cache-first read from `known_parts`;
   on miss runs SourcingAgent, then comparison, then **write-back to `known_parts`** for
   all tiers incl. tier_1 (`:1231-1238`). Tier 1 candidates with a `source_url`/name would
   get cached. This is the I4 poison surface — see I4.
3. **`request-confirmation` + `_mock_confirmation_response` (`:2097`)**: flips
   `confirmation_needed` on tier_1 candidates by display-id `{vendor}-t1-{idx}`.
4. **Frontend**: `frontend/src/types/index.ts` `Candidate` + `vendor-card.tsx` render.

**EXACT candidate contract the frontend needs (from `_transform_option` `:800` + types):**
A Tier 1 candidate dict (snake_case, the SourcingOption shape `_run_tier1` emits) must
carry — the transform maps these to the camelCase `Candidate`:
- `vendor_name` → `vendorName` (str, required; "Unknown" fallback)
- `merchant_type` → `vendorType` via `_vendor_type(...)` (str)
- `base_price` + `price_tbd` (+ `requires_rfq`) → `price` (None when price_tbd/requires_rfq
  → frontend renders "Quote Required", `evidenceState="uncontacted"`)
- `lead_time_days` + `lead_time_source` → `leadTime` (None when absent/placeholder) +
  `leadTimeSource` ("extracted"|"defaulted"|"placeholder"|"quoted")
- `source_url` → `url`
- `found_part_number` → `foundPartNumber`
- `suitability_score` → `suitability` (float)
- `confidence_score` → `confidence` (float)
- `match_type` → `isExactMatch` ("Exact OEM"), `isAftermarket` ("Aftermarket Compatible"),
  `pnMatchLevel` via `_pn_match_level`
- `vendor_authorization_status` == "Authorized" → `isAuthorizedDistributor`
- `is_oem_direct` → `isOemDirect`
- `in_stock` → `stock` ("In stock" only when True)
- `ship_from_country` → `shipFrom` / `loc`
- `contact_email` → `contact`
- `suitability_tier` → `relationship` (Pill)
- `comparison_artifact` → `comparisonArtifact`
- `confirmation_needed` (default True for tier 1) → `confirmationPending`
- `limited_price_data` → `priceVerified`

**Price-honesty handling:** `price_tbd=True` OR `requires_rfq=True` → `price=None`,
`evidenceState="uncontacted"`. A registry-backed candidate with NO confirmed quote MUST set
`price_tbd=True` (and `base_price` absent/0) so the card renders "Quote Required" — never a
fabricated figure. This is the gate-2 trust guarantee.

## I2 — Request-side match inputs at Tier 1 time

`SourcingAgent.run` builds `AssetSpecs` via `_dict_to_specs` from `run.asset_specs_json`
(`:1268`). At `_run_tier1` call (`:433`), `specs` carries:
- **`detected_type`** (Optional[str]) — e.g. "mechanical seal". Classified to a noun-class
  canonical via `part_type_classes.classify_noun_class(detected_type)` → e.g. "SEAL".
  This is the **class hard-gate** input (request canonical vs supplier `class_id`).
- **`manufacturer`** (str, default "Unknown") — the **brand amplifier** input (vs supplier
  `brand_id` rows, tri-state relationship).
- **`category`** ("Part"|"Equipment") — secondary class signal.
- **`component_of`** (Optional) — parent machine (e.g. "Goulds 3196"); populated only under
  INTAKE_TYPE_AWARE. Available as a corroboration/anchor signal, not required.
- **buyer location: ABSENT from AssetSpecs.** No `state`/`zip`/`ship_to` field on the
  request at sourcing time. `SourcingRun.facility_id` exists (placeholder UUID) but no
  site→state/zip resolution is wired into the sourcing path (site_settings.get_shipto holds
  ship-to per site but is not read by SourcingAgent). **CONFIRMED EXPECTED: buyer location
  may be absent → territory ranking must degrade gracefully to neutral.** The matcher will
  accept an optional `buyer_state`/`buyer_zip` param (None → neutral territory rank, and
  local_service hard-filter becomes a no-op inclusion rather than exclusion — see T1
  decision below).

## I3 — Notification seam + send machinery (the double-gate)

**Send machinery (CONFIRMED EXPECTED — stubbed/flagged):** `utils/email_sender.py`:
- `EMAIL_SEND_ENABLED` (env, default OFF, strict truthy parse `:58`). The conftest safety
  net (`conftest.py:62`) force-sets `email_sender.EMAIL_SEND_ENABLED = False` for every
  test — **no test can send by accident.**
- `GmailSender.send` (`:211`): if `not EMAIL_SEND_ENABLED` → prints `[EmailSender] STUBBED`
  and returns `SendResult(status="stubbed")`, ZERO network. Flag on + no creds →
  `status="error"` (fail-soft). Flag on + service → real send (unreachable in tests).
- `EmailMessage`/`SendResult` dataclasses; `record_sent_message` in supplier_registry
  persists one row per send attempt (`:870`, status "sent"|"stubbed"|"error").

**Notification seam:** there is no existing matched-request→notify hook post-sourcing. The
closest is the Tier 3 outreach flow (`rfq_send`, request-confirmation) which is
human-triggered, not automatic. The Night 5 notify layer is NEW: it records notification
*events* (a `supplier_notifications` table) for matched Tier 1 candidates, behind the
notify≫display asymmetry, and the send itself goes through `EmailSender.send` (stubbed).
**Double-gate asserted:** (1) `EMAIL_SEND_ENABLED=False` (conftest + repo default), (2)
TIER1_V2 flag gates the whole notify path. Zero live sends possible this session.

## I4 — Cache interaction (the poison-bug class)

**CONFIRMED FEASIBLE — registry-backed results bypass the price-cache write path.**
`known_parts.upsert_edges` (`:147`) keys edges by `_edge_id(source_url|url, vendor_name)`
and writes `{tier, price, price_date, ...}`. The write-back at `api_server:1231-1238`
includes tier_1 candidates. A registry-backed Tier 1 candidate with `price_tbd=True`
writes `price=None` (volatile field stays None) — so no fabricated price is cached — BUT
the durable edge (supplier→part mapping) would still be written, and on a later cache HIT
`_result_from_cached_edges` (`:996`) reconstructs a candidate WITHOUT the relationship
badge / aftermarket disclosure / registry provenance (it only carries `suitability`,
`match_type`, `tier`). That is the poison surface: **a registry-backed card cached then
served from cache loses its relationship provenance and could be served for a request the
registry no longer matches.**

**Decision (per brief T2 "fresh-per-run, no cache write"):** registry-backed Tier 1
candidates are computed fresh per run from the registry (a cheap local SQLite lookup) and
**excluded from the `known_parts` write-back**. They are also not served from the
cache-first path (the cache path reconstructs from `known_parts` only; registry matches are
re-derived on every run). This keeps the gate-2 trust guarantee: no registry card ever
enters staleness, and the purge guards (`test_known_parts.py`, `test_price_db.py`) stay
green because we touch neither store's write path for Tier 1.

---

## Findings vs. EXPECTED — summary

| Item | EXPECTED | Finding |
|------|----------|---------|
| I1 | honest-empty; report frontend contract | CONFIRMED honest-empty; contract documented above |
| I2 | class + manufacturer available; buyer location may be absent | CONFIRMED; detected_type→class, manufacturer→brand; no buyer loc on specs |
| I3 | reuse EmailSender behind stubs/flags; double-gate | CONFIRMED; EmailSender + conftest force-off + TIER1_V2 gate |
| I4 | registry-backed results fresh-per-run, bypass cache write | CONFIRMED feasible; decision: exclude Tier 1 from known_parts write-back |

**No contradiction with EXPECTED.** Proceeding to build tasks.

### Unspecified decisions (recorded here, surfaced in morning report)
1. **No-brand-row relationship badge:** a class-matched supplier with no `brand_id` row for
   the requested manufacturer carries NO relationship badge (brand-neutral). We do not
   fabricate AUTHORIZED/CARRIES. The notify gate still fires on the core-class OR brand-match
   rule (DXP has SEAL core → notify fires for a Goulds seal even with no brand row).
2. **AFTERMARKET disclosure** only attaches when the matched brand relationship is
   `AFTERMARKET_COMPATIBLE` (the disclosure flag + text on the candidate payload).
3. **local_service as hard filter** with no buyer zip: when `buyer_zip` is None,
   local_service suppliers are *included* (not excluded) — excluding them would silently
   drop onboarded local suppliers when the request carries no location (the common case
   today). The hard filter activates only when a buyer zip is present and the supplier is
   outside radius. This keeps local_service as the ONLY geographic hard filter per the
   brief while degrading gracefully (I2).
4. **buyer_state** threaded optionally from the run when available (future); None today →
   territory rank neutral (NATIONWIDE suppliers rank at top, others unchanged).
