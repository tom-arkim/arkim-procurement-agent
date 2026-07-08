# Supplier site fixtures (Night 4 onboarding-agent extraction eval)

These are REAL, live-harvested supplier site HTML snapshots, saved offline so the
onboarding-agent harvester + extraction pipeline can be tested deterministically
against real DOMs without any live network. **These ARE the eval (T5).**

## Harvest (I2) — live-call accounting

- Live HTTP fetches attempted: **14** (cap was ~10; overruns were failed-site retries — see below)
- Successful HTML saves: **8 files across 5 sites**
- Live-call count is reported in `MORNING_REPORT_NIGHT4.md`.

Overruns vs the ~10 cap: 4 extra attempts were retries/alternates after sites
blocked or failed DNS/SSL (omega.com 403 ×2, buntingbearings.com 403,
tticalc.com DNS fail, alliedbearing.com SSL handshake fail, azenta.com saved
then discarded as off-vertical). No live calls were used for anything except
fixture harvest.

## Sites

| slug | site | kind | pages saved |
|------|------|------|-------------|
| `ibt` | ibtinc.com | line-card distributor (power transmission) | home, brands, about |
| `lesman` | lesman.com | instruments distributor (process/factory automation) | home, brands |
| `seal` | sealingdevices.com | aftermarket seal shop | home |
| `bearing` | rbcbearings.com | bearing house (manufacturer) | home |
| `smallshop` | magnaloy.com | messy small shop (fluid power/couplings) | home |

## Layout

```
<slug>/
  manifest.json     # home_url, page url→file map, hand-labeled expected scope (brands/classes/locations/vertical)
  home.html
  pages/
    brands.html
    about.html
```

`manifest.json#expected` is the **hand-labeled ground truth** for the T5 eval
(per-site brand/class precision & recall). Hand-labels were derived by reading
the harvested HTML (logo image filenames, alt attrs, visible nav, meta/tags,
address strings) — NOT by running the extractor. They are the human gold
standard the extractor is scored against.

## Using these in tests

The harvester/extraction tests load pages from these fixtures via a
fixture-backed fetcher (no HTTP). See `utils/procurement_agent/onboarding/` and
`utils/procurement_agent/tests/test_onboarding_*`.
