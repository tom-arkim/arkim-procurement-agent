# Night 5 — Morning Report: Tier 1 Runtime (Matching + Notify≫Display)

**Branch:** `feature/tier1-runtime-overnight` (from `4cc06be` on `test/flag-on-integration`)
**HEAD:** `1f1013b`
**No push.**

Mission: onboarded suppliers (Nights 3–4) appear in live sourcing as honest,
relationship-backed Tier 1 cards (NO fabricated prices), and the notification
path exists with the research's conservative notify≫display asymmetry. A real
onboarded supplier exists in the registry (DXP Enterprises, onboarded Night 4) —
the Goulds-anchor test uses fixture suppliers, but DXP is available for morning
verification.

---

## Guardrail compliance

1. **Branch from `4cc06be` on `test/flag-on-integration`** — verified (`git rev-parse HEAD` = `4cc06be` before branch). Working tree clean of deleted/modified tracked files (only pre-existing untracked stale briefs/audit docs). ✅
2. **Single committer** — every commit is this session; no foreign commits appeared. ✅
3. **All new behavior behind `TIER1_V2`** (extends Nights 3–4). Flag-off = Tier 1 honest-empty, byte-identical, proven by inertness tests (`test_tier1_matcher.py::TestFlagGating`, `test_tier1_notify.py::TestFlagOffInertness`, `test_tier1_runtime_live.py::TestInertnessFlagOff`). ✅
4. **Mocks in pytest; NO live network, NO LIVE EMAIL SENDS.** The notification layer reuses the existing stubbed/flagged `EmailSender`. The double-gate is asserted: `EMAIL_SEND_ENABLED` defaults OFF + the conftest safety net force-sets it OFF for every test + `TIER1_V2` gates the whole notify path. `test_tier1_runtime_live.py::test_notify_events_recorded_zero_live_sends` asserts every event is `send_status="stubbed"` end-to-end through the real send seam (the default `GmailSender`). **Zero live calls this session.** ✅
5. **Do-not-touch** — `.env`, `audit/`, `scripts/*_self_test.py`, seed/demo fixtures, `known_parts.json`, `price_db.json`, `DEMO_MODE` gates, the security/allowlist surface, the phase3/§7.7 flaky orchestrator/persistence tests: untouched. Reused Nights 3–4's registry + matcher inputs (no schema change to `supplier_classes` / `supplier_brands` / `supplier_local_service`). ✅
6. **THE PURGE GUARD IS SACRED** — the cache-contamination purge guards and the "no price without a dated confirmed price_db entry" property stay green. Tier 1 cards carry NO fabricated prices — ever. The matcher's `Tier1Match` dataclass is price-free by construction; `to_candidate` surfaces a price ONLY from a dated `price_db` entry with `source="rfq"` (asserted live: `test_confirmed_quote_surfaces_price_on_card`). The purge-guard suites (`test_known_parts.py`, `test_price_db.py`) and `test_supplier_scope.py` are untouched green. Registry-backed Tier 1 is excluded from the `known_parts` write-back (I4) — it never enters staleness. ✅
7. **LIVE-FAITHFULNESS** — the matcher is tested through the REAL `_run_tier1` path with a fixture registry, via the API (TestClient): `test_tier1_runtime_live.py::TestLiveFaithfulMatcher`. ✅
8. **Investigation-gate** — read-only investigation reported first (`audit/NIGHT5_INVESTIGATION.md`); no contradiction with EXPECTED. ✅
9. **Iteration cap 5 per failing task** — hit one cross-test isolation hazard (the `assemble_recipient_set` manual `setattr`/`del` leak + the `api_server` DEMO_MODE-reload cache); resolved within the cap (monkeypatch.setattr + `DEMO_MODE` reset, mirroring the existing `onboarding_api` fixture). ✅
10. **Final act: this report. No push.** ✅

---

## Per-task status + hashes

| Task | Status | Commit | Notes |
|------|--------|--------|-------|
| I1–I4 investigation | ✅ done | `c473d2a` | `audit/NIGHT5_INVESTIGATION.md`. Findings match EXPECTED; no contradiction. |
| T1 — the matcher | ✅ done | `c473d2a` | `utils/procurement_agent/tier1_matcher.py` (class gate + brand amplifier + territory rank + local_service hard filter). 29 tests. |
| T2 — Tier 1 candidates from matches | ✅ done | `b518ae7` | `tier1_matcher.to_candidate` / `candidates_from_matches`; wired into `_run_tier1`; honest card data, no fabrication; fresh-per-run, no cache write. |
| T3 — notify≫display asymmetry | ✅ done | `67e69a0` | `utils/procurement_agent/tier1_notify.py` + `supplier_notifications` table. Two thresholds + per-RFQ cap (6) + class-gate on notify; events recorded; double-gate. 14 tests. |
| T4 — aftermarket disclosure | ✅ done | `b518ae7` | `is_aftermarket` + `aftermarket_disclosure` text reach the candidate payload (`aftermarketDisclosure` on the camelCase candidate). Frontend rendering is morning work. |
| T5 — inertness + regression | ✅ done | `1f1013b` | flag-off honest-empty byte-identical; live-faithful path via the API. 12 tests. |
| Overnight testing + this report | ✅ done | `1f1013b` | truth table, Goulds anchor, honesty, notification cap/gate, live-faithful — all in the three test files. |

**Commits (`4cc06be` → HEAD):**
- `c473d2a` feat(tier1): T1 matcher — class gate + brand amplifier + territory rank (Night 5)
- `b518ae7` feat(tier1): T2+T4 — honest candidates from matches + aftermarket disclosure (Night 5)
- `67e69a0` feat(tier1): T3 — notify≫display asymmetry + notification events (Night 5)
- `1f1013b` feat(tier1): T5 — inertness wall + live-faithful matcher path via the API (Night 5)

**New modules:** `utils/procurement_agent/tier1_matcher.py`, `utils/procurement_agent/tier1_notify.py` — clean, typed, fail-soft, standalone, built to the house standard where they can stand alone; matching the surrounding module conventions at the integration points (`_run_tier1`, `_run_sourcing_background`, `_transform_option`). `supplier_registry.py` gains the `supplier_notifications` table + record/get helpers (gated by `TIER1_V2`).

**Suite:** **1795 passed / 73 skipped** (baseline `1740 passed / 73 skipped` + 55 new tests: 29 matcher + 14 notify + 12 live-faithful). Run with `uv run pytest -q`.

**§7.7 flaky-under-load pair** (`test_orchestrator.py` + `test_persistence.py`): green this run (29/29 when run explicitly; also green in the full suite). Not mine — noted per guardrail 8. Not touched.

---

## The matcher truth table (results)

From `test_tier1_matcher.py` — all green:

| Property | Result |
|----------|--------|
| Class hard-gate excludes wrong-class ALWAYS (even brand-matched) | ✅ `TestClassHardGate::test_wrong_class_excluded_even_when_brand_matches` — a PUMP-only supplier carrying Goulds AUTHORIZED does NOT match a SEAL request. |
| Class match admits incidental (non-core) — core-ness is a ranker, not a gate | ✅ `test_class_match_admits_incidental_non_core` |
| Undetectable request class → no matches | ✅ `test_undetectable_request_class_returns_empty` |
| Non-onboarded supplier excluded (discovered/quoted ≠ Tier 1) | ✅ `test_non_onboarded_supplier_excluded` |
| Brand tri-state ordering AUTHORIZED > CARRIES > AFTERMARKET_COMPATIBLE | ✅ `TestBrandAmplifier::test_authorized_ranks_above_carries_above_aftermarket` |
| Brand-neutral still matches (brand is an amplifier, not a gate) | ✅ `test_brand_neutral_still_matches` |
| Brand-neutral ranks below AFTERMARKET_COMPATIBLE | ✅ `test_brand_neutral_ranks_below_aftermarket` |
| Unknown manufacturer → brand-neutral | ✅ `test_unknown_manufacturer_is_brand_neutral` |
| Territory RANKS, never filters (NATIONWIDE > state-match > state-no-match; all returned) | ✅ `TestTerritoryRanksNotFilters::test_nationwide_ranks_above_state_match` |
| No buyer location → degrade-graceful neutral (none filtered) | ✅ `test_no_buyer_location_degrades_to_neutral` |
| Local-service hard filter: included without buyer zip (I2) | ✅ `test_local_service_included_without_buyer_zip` |
| Local-service: included with buyer zip in range | ✅ `test_local_service_included_with_buyer_zip_in_range` |
| Local-service: excluded with buyer zip out of range (zero radius) | ✅ `test_local_service_excluded_with_buyer_zip_out_of_range` |
| Honesty: the matcher NEVER carries a price (no price field on Tier1Match) | ✅ `TestHonestyNoPrice::test_match_has_no_price_field` |
| Honesty: no price key anywhere in match-explanation | ✅ `test_no_price_anywhere_in_match_explanation` |
| Determinism: equal-score suppliers ordered by name | ✅ `TestDeterminism::test_equal_score_suppliers_ordered_by_name` |
| Determinism: repeated calls identical (fresh-per-run, no drift) | ✅ `test_repeated_calls_identical` |
| Flag-off → `[]` (byte-identical empty) | ✅ `TestFlagGating::test_flag_off_returns_empty` |
| Falsy-token parity (fail safe/closed) | ✅ `test_falsy_token_is_flag_off` |

---

## THE GOULDS ANCHOR (result)

`test_tier1_matcher.py::TestGouldsAnchor` — the load-bearing test — **green**:

- Fixture registry: (a) `goulds-auth.com` — an authorized Goulds SEAL distributor; (b) `aftermarket-seals.com` — an aftermarket fits-Goulds seal shop; (c) `pump-only.com` — an onboarded PUMP-only supplier (carrying Goulds AUTHORIZED).
- Request: class SEAL + manufacturer Goulds.
- **a & b match** (both SEAL); **a ranks first** (`test_a_ranks_first`); **b carries the aftermarket disclosure flag** (`test_b_carries_aftermarket_disclosure`); **c is EXCLUDED by the class hard-gate** (`test_a_and_b_match_c_excluded`).
- Every match carries the class-gate that admitted it + `onboarded=True` (`test_match_explanation_carries_class_gate`).

The Goulds anchor is ALSO asserted live-faithful through the real API: `test_tier1_runtime_live.py::test_matching_request_renders_tier1_card` (a & b render, a first, c excluded) and `test_wrong_class_renders_no_seal_card_for_pump_request` (a PUMP request renders c but NOT a/b).

---

## Findings vs. EXPECTED notes (investigation I1–I4)

Full report: `audit/NIGHT5_INVESTIGATION.md`. Summary:

| Item | EXPECTED | Finding |
|------|----------|---------|
| I1 | honest-empty; report the frontend candidate contract | CONFIRMED honest-empty (seed catalog `{"suppliers": []}` + `_seeded_tier1_candidates` permanently `[]`). Frontend contract documented in detail (the snake_case SourcingOption shape → `_transform_option` → camelCase `Candidate`: `vendor_name`, `merchant_type`, `base_price`/`price_tbd`, `lead_time_days`/`lead_time_source`, `source_url`, `found_part_number`, `suitability_score`, `confidence_score`, `match_type`, `vendor_authorization_status`, `is_oem_direct`, `in_stock`, `suitability_tier`→`relationship`, `comparison_artifact`, `confirmation_needed`). Price-honesty: `price_tbd=True` → `price=None` → "Quote Required" card. |
| I2 | class + manufacturer available; buyer location may be absent → territory degrades to neutral | CONFIRMED. `detected_type`→class (via the shared `part_type_classes` dictionary — the SAME one SCORING_V2's TypeGate uses); `manufacturer`→brand. NO buyer state/zip on `AssetSpecs` at sourcing time (`SourcingRun.facility_id` is a placeholder; `site_settings.get_shipto` is not read by SourcingAgent). Territory degrades gracefully to neutral; `local_service` degrade-graceful includes (not excludes) when no buyer zip. |
| I3 | reuse EmailSender behind its existing stubs/flags; double-gate | CONFIRMED. `utils/email_sender.py` `EMAIL_SEND_ENABLED` (default OFF) + conftest safety net + `TIER1_V2` gate = the double-gate. No existing matched-request→notify hook (the closest is the human-triggered Tier 3 `rfq_send`); the Night 5 notify layer is NEW and records events behind the stubbed seam. |
| I4 | registry-backed results fresh-per-run, bypass cache write | CONFIRMED feasible. Decision: registry-backed Tier 1 is excluded from the `known_parts` write-back (`is_registry_backed=True` marker) AND re-derived fresh at the persist boundary on BOTH the cache-hit and discovery paths (the cache-first path bypasses SourcingAgent, so without the re-derive a cache hit would crowd out registry Tier 1). Registry Tier 1 never enters staleness. |

**No contradiction with EXPECTED.** Proceeded to build tasks.

---

## Every unspecified decision

1. **No-brand-row relationship badge:** a class-matched supplier with no `brand_id` row for the requested manufacturer carries NO relationship badge (brand-neutral). We do not fabricate AUTHORIZED/CARRIES. The notify gate still fires on the core-class OR brand-match rule (a brand-neutral core-class match → notify on `core_class`). (I-decision 1)
2. **AFTERMARKET disclosure** attaches only when the matched brand relationship is `AFTERMARKET_COMPATIBLE` (`is_aftermarket` + `aftermarket_disclosure` text). (I-decision 2)
3. **`local_service` as hard filter with no buyer zip:** when `buyer_zip` is None, local_service suppliers are *included* (not excluded) — excluding would silently drop onboarded local suppliers when the request carries no location (the common case today). The hard filter activates only when a buyer zip is present and the branch is outside radius. This keeps `local_service` as the ONLY geographic hard filter per the brief while degrading gracefully (I2). (I-decision 3)
4. **`buyer_state`** threaded optionally from the run when available (future); None today → territory rank neutral (NATIONWIDE suppliers rank at the top of the neutral band, others unchanged). The matcher accepts `buyer_state`/`buyer_zip` params; the live path does not yet populate them (no buyer location on the request at sourcing time — I2). (I-decision 4)
5. **Composite rank weights** (`_W_BRAND=50`, `_W_CORE=25`, `_W_TERRITORY=15`, `_W_PERF=10`) are informed defaults consistent with the brief's "amplifier = brand, ranking = territory + is_core + performance"; calibration with real data is a later step (mirrors SCORING_V2's informed-defaults posture, CLEANUP §7.4). The brand AMPLIFIER is the strongest signal (an authorized channel is the best possible Tier 1 match).
6. **Notification cap default = 6** (sits in the brief's 5–8 range).
7. **Notify email content:** the notify is an FYI of a matched request (not an RFQ draft — the RFQ draft flow is the existing Tier 3 `rfq_send` path, separate). Recipient set via the registry's free cascade (`assemble_recipient_set`: cached contact → generic `sales@{domain}` → human-flag; no Apollo). A no-recipient notify is recorded as `stubbed` (queued, not silently dropped).
8. **Confirmed price on the card → `evidenceState="priced"`** (not `"quoted"`). The matcher's `to_candidate` reads `price_db` (`source="rfq"`) directly and surfaces a priced Tier 1 card (`price_tbd=False`). The State-C `"quoted"` overlay is the RFQ-reply (`review_items`) path, a separate mechanism. Either way the card carries a real, dated, confirmed price — not fabricated. (Asserted live: `test_confirmed_quote_surfaces_price_on_card`.)
9. **`_run_sourcing_background` re-derives registry Tier 1 at the persist boundary** (on both cache-hit and discovery paths). On the discovery path this means the matcher runs twice (once in `_run_tier1`, once at the persist boundary) — a cheap local SQLite lookup, acceptable, and it guarantees the displayed Tier 1 set and the notify set are identical (same `_tier1_matches`).

---

## Blockers

None. All build tasks complete; the suite is green; the gate-2 trust guarantee holds; zero live sends.

---

## Morning-verification inputs

1. **The real onboarded supplier — DXP Enterprises (`dxpe.com`):** `tier1_lifecycle='onboarded'`, 9 classes (PUMP, SEAL, BEARING, MOTOR, PACKING core; FILTER, HOSE, LUBRICANT, VALVE incidental), `ship_area=NATIONWIDE_US`, NO brand rows, NO local_service. With `TIER1_V2=1`, a Goulds mechanical-seal request should render DXP as a brand-neutral, core-class Tier 1 card (quote-expected framing — no confirmed `price_db` quote exists for DXP on a Goulds seal). Verify:
   ```bash
   TIER1_V2=1 uv run python -c "
   from utils import supplier_registry as sr; sr.TIER1_V2=True
   from utils.procurement_agent import tier1_matcher as tm
   m=tm.match_tier1(detected_type='mechanical seal', manufacturer='Goulds')
   print([(x.vendor_name, x.brand_relationship, x.is_core, round(x.score,1)) for x in m])
   "
   # Expect: [('DXP Enterprises', None, True, 40.0)]
   ```
   Then through the API: create a run with `detected_type='mechanical seal'`, `manufacturer='Goulds'`, confirm-intake, and inspect `sourcing_results.tier1` — DXP should appear with `registryBacked=true`, `price=null`, `relationship=null`, `tier1MatchExplanation.class_gate='SEAL'`.

2. **The live-faithful + inertness suites:**
   ```bash
   uv run pytest utils/procurement_agent/tests/test_tier1_matcher.py \
                 utils/procurement_agent/tests/test_tier1_notify.py \
                 utils/procurement_agent/tests/test_tier1_runtime_live.py -q
   # Expect: 55 passed.
   ```

3. **The purge-guard + SCORING_V2 inertness suites (untouched, must stay green):**
   ```bash
   uv run pytest utils/procurement_agent/tests/test_known_parts.py \
                 utils/procurement_agent/tests/test_price_db.py \
                 utils/procurement_agent/tests/test_scoring_t6_inertness.py \
                 utils/procurement_agent/tests/test_supplier_scope.py -q
   ```

4. **The full suite:**
   ```bash
   uv run pytest -q
   # Expect: 1795 passed, 73 skipped.
   ```

5. **The double-gate (no live sends):** `EMAIL_SEND_ENABLED` defaults OFF; the conftest safety net force-sets it OFF for every test. `test_tier1_runtime_live.py::test_notify_events_recorded_zero_live_sends` asserts every notification event is `send_status="stubbed"` end-to-end through the real `GmailSender`. No live email can fire this session.

---

## Frontend rendering (morning work — out of scope this night)

The DATA is on the candidate payload (`aftermarketDisclosure`, `registryBacked`, `tier1MatchExplanation`, `relationship`); the frontend `Candidate` type + `vendor-card.tsx` rendering of the registry-backed badge / aftermarket disclosure is morning work. The existing `relationship` Pill + `isAftermarket` amber Pill already render from the surfaced fields; the new `aftermarketDisclosure` text + `registryBacked` provenance need UI treatment. No frontend types were changed this night (the camelCase keys are additive on the candidate payload).

*No push.*
