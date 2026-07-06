# T4 — Cache-Revalidation Regression Report (Phase 1)

**Branch:** `feature/run-capture-overnight`
**Commits under test:** T1 `a922458` · T2 `b9f18f8` · T3 `27c8913`
**Date:** 2026-07-05

## What T4 verified

The cache-revalidation fix (T1/T2/T3) against the contamination documented in the
baseline 50-run (`scripts/harness_results_20260704T183210Z.json`): the shared
`unknown|UNKNOWN-PN` bucket in `utils/price_db.json` served motor + pump URLs at
a hard-coded `suitability_score=50.0` onto every PN-less (vague) query.

T4 re-ran the **12 `vague-*` parts** through the live API harness with:
- backend flags `INTAKE_TYPE_AWARE=1`, `SCORING_V2=1`, `RUN_CAPTURE=1`
- `utils/price_db.json` + `utils/known_parts.json` **backed up then deleted** first
  (backup in `audit/t4-cache-backup/*.pre-t4`)
- a fresh backend on `:8001` carrying T1+T2+T3 (the first re-run accidentally hit
  a stale pre-T1 backend left over from the baseline 50-run — killed and re-run;
  see "False start" below)

Fresh results: `scripts/harness_results_20260705T193359Z.json`.
Comparison tool: `scripts/t4_before_after_compare.py`.

## Before/after table

`contam` = motor/pump-URL candidates surfacing on a wrong-class vague query
(motor hosts: `emotorsdirect.ca`, `electricmotorwarehouse.com`, `mrosupply.com`;
pump host: `shoppumps.com`). `wf` = harness post-hoc wrongness flags
(noun-class mismatch + above the 30% floor).

| part | before | after | before contam | after contam |
|---|---|---|---|---|
| vague-valve | 4 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-solenoid | 4 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-starter | 4 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-hose | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-level | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-transmitter | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-gasket | 0 wf / 0 contam (baseline errored) | **0 wf / 0 contam** | — | — |
| vague-belting | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-chain | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-gearbox-oil | 0 wf / 4 contam | **0 wf / 0 contam** | MOTOR,PUMP | — |
| vague-motor | 0 wf | 0 wf | (motor URLs correct here) | — |
| vague-washdown-motor | 0 wf | 0 wf | (motor URLs correct here) | — |

**Success criterion met:** motor URLs do NOT appear on valve / solenoid / starter
/ hose / level (nor on transmitter, gasket, belting, chain, gearbox-oil). Pump
URLs do NOT appear on any vague part.

The baseline contamination signature (eMotors Direct, MROSupply,
Electric Motor Warehouse, Pump Products/shoppumps.com — all at suit=50.0) is
**gone** from every vague part.

## T1 confirmed (unconditional — phase3 backport candidate)

After the full 12-part vague run, **`utils/price_db.json` was not recreated**
— the null-PN guard (`_make_key` returns `""` for null-mfg/PN specs →
`get_cached_prices` returns `{}`, `save_price` no-ops) held for every PN-less
query. No `unknown|UNKNOWN-PN` bucket was written. Verified directly:

```
_is_cacheable_identity("Unknown", "UNKNOWN-PN") -> False
_make_key("Unknown", "UNKNOWN-PN")             -> ""
save_price("Unknown", "UNKNOWN-PN", ...)        -> no-op
```

`utils/known_parts.json` was recreated with one real-identity key only
(`goulds|3196`, 2 edges) — a manufacturer-real / model-real entry T1 correctly
permits (the guard blocks null-PN, not real-identity caching). This is the
precision T1 was designed for: it stops the cross-query shared bucket without
disabling legitimate caching.

## T2 confirmed

Zero wrongness flags on every vague part — no confirmed-different noun-class
candidate surfaced from either cache seam. (The fresh run hit no cache at all
for vague parts because T1 blocks null-PN reads, so T2's drop logic was
exercised by the unit suite — `TestTier2CacheTypeGate`,
`TestKnownPartsCacheTypeGate` — rather than the live harness. The harness
confirms the live-discovery path + T1 together already keep vague queries
clean; T2 is the defense-in-depth for real-identity keys that cache a
wrong-type listing.)

## T3 confirmed (unconditional — phase3 backport candidate)

No cache-served candidate was marked `match_type="Exact OEM"`. The price_db
serve path sets `"Functional Alternative"` explicitly (commit `27c8913`); the
known_parts path already defaulted honestly. No frontend enum change was
required (`PnMatchLevel` derives from `pn_match_status`, which stays `None`
for cache hits).

## PURGE

The poisoned `unknown|UNKNOWN-PN` bucket (13 vendors) was deleted from
`utils/price_db.json`. The cleaned file preserves the 22 legitimate
real-identity entries (skf, allen-bradley, baldor, grundfos, etc.). T1 prevents
the bucket from re-creating. `price_db.json` is gitignored, so this is a
working-copy cleanup, not a committed file change; the purge note lives in
`CLEANUP.md` §7.1 (status → RESOLVED).

## Suite

`uv run pytest -q` → **1549 passed, 73 skipped** (green) with T1+T2+T3 applied.

## False start (process note)

The first harness re-run (`harness_results_20260705T190432Z.json`) is
**invalid** and discarded: the `uv run uvicorn` invocation failed to bind
`127.0.0.1:8001` (`Errno 10048`) because a backend left over from the baseline
50-run (PID 22412, started 2026-07-04 09:16:20 — before T1 was committed) was
still occupying the port. My process exited; the stale pre-T1 backend served
the harness, so the "after" results were old-code behavior (the
`unknown|UNKNOWN-PN` bucket was recreated, motor URLs surfaced). I killed the
stale backend, restarted with the T1+T2+T3 code, cleared the caches, and
re-ran. The stale-process bug is a harness-operational note, not a code
defect — the lesson for future harness runs: confirm the serving backend's
start time / commit before trusting results.
