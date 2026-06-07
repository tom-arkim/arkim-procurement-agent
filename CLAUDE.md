# CLAUDE.md

Operational guide for Claude Code working in the Arkim Procurement Agent repository.
Read this first, every session. It describes the repo **as it is today**, not as it will be.

> **Companion documents (deeper context — read when relevant):**
> - `docs/arkim_procurement_code_standard.md` — the code-quality & testing standard this repo follows. **Authoritative for how to write and test code.** Includes the integration patterns (§9 below references them).
> - `docs/arkim_procurement_agent_brief.md` — product/architecture intent (what the system is and why).
> - `CLEANUP.md` (repo root) — known technical debt backlog.
> - Rollout plan + BOM placement brief (sequencing: arcs, resourcing, knowledge-architecture/BOM placement, the Apollo/search-provider architecture) — kept in internal docs (Notion/Drive) or `docs/` if committed; not required reading for routine code tasks, but the source of truth for the sourcing/supplier architecture decisions in §9.
>
> **Repo visibility: PRIVATE.** Internal/candid content (the security-gap notes in §6, CLEANUP.md, the code standard) is appropriate to keep here. Write honestly.

---

## 1. What this repo is

An AI procurement agent for industrial maintenance parts (companion to the Arkim maintenance platform). Five-phase workflow: intake → inventory → multi-tier sourcing → spec comparison → approval. Targets mid-market F&B / pharma manufacturers.

**Current status: prototype being hardened.** The product logic works end-to-end for demos; it is being hardened into a production system. Expect rough edges, stubs, and known debt (see §6, CLEANUP.md).

---

## 2. Architecture as it actually is (read carefully — there's a gotcha)

Two parallel front ends sit over **one shared backend core** (`utils/`). They do **not** talk to each other.

```
                    ┌──────────────────────────────┐
                    │   utils/  (the real core)     │
                    │   sourcing pipeline, agents,  │
                    │   scoring, intake, brand-     │
                    │   intelligence, persistence   │
                    └──────────────────────────────┘
                       ▲                        ▲
        imports in-process                 imports in-process
                       │                        │
        ┌──────────────┴───────┐    ┌───────────┴───────────────┐
        │  Streamlit front end │    │  FastAPI (api_server.py)  │
        │  app.py →            │    │  ~22 REST endpoints       │
        │  pages/sourcing_     │    │  uvicorn api_server:app   │
        │  runs.py             │    │      --port 8001          │
        │  (NO HTTP, in-proc)  │    └───────────┬───────────────┘
        └──────────────────────┘                │ HTTP /api/*
                                                 │
                                    ┌────────────┴─────────────┐
                                    │  Next.js / React         │
                                    │  frontend/  (next dev    │
                                    │      --port 3000)        │
                                    └──────────────────────────┘
```

**The gotcha:** Streamlit imports `utils/` **directly, in-process** — it never goes through FastAPI. FastAPI (`api_server.py`) is a thin REST wrapper over the *same* `utils/`, consumed **only** by the React frontend. Both run against the same SQLite DB (WAL mode), separate ports, no conflict.

**Consequences:**
- **Harden `utils/`, and you harden both front ends at once.** This is the right target for backend work.
- **Harden `api_server.py` (validation, error handling, auth), and you only harden the React path** — Streamlit bypasses all of it. Do this as part of frontend/endpoint work (Arc 1), not core hardening.
- **Any `utils/` change is immediately live in Streamlit** with no contract layer between them. A changed return shape hits `pages/sourcing_runs.py` directly. When you change `utils/`, keep the Streamlit pages matched in the same change.

---

## 3. Running it

```bash
# Streamlit front end (the current primary build/demo surface)
streamlit run app.py            # NOT chat_app.py — the README is stale; chat_app.py does not exist

# FastAPI backend (consumed by the React frontend)
uvicorn api_server:app --reload --port 8001

# React frontend
cd frontend && next dev --port 3000
```

- **Environment is uv-managed (`.venv`, Python 3.11).** Use `uv sync --group dev` then `uv run ...`. The old `venv`/`venv_win` directories have been deleted — `.venv` is canonical. `requirements.txt` is gone; dependencies live in `pyproject.toml` + `uv.lock`.
- Required env: `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`. Optional: `APOLLO_API_KEY` (supplier validation/contact — system no-ops cleanly without it), `PARALLEL_API_KEY` (alternate search provider, when wired). Model overrides: `OS_EXTRACTION_MODEL`, `BRAND_INTEL_MODEL`, `SPEC_ENRICH_MODEL` (default `claude-haiku-4-5`).
- Dev reseed endpoint: `/api/dev/reseed-handoffs` reloads seeded maintenance handoffs.

---

## 4. Testing

**A pytest suite exists and is GREEN: 360 passing** on Python 3.11 via uv (offline, mocked externals). It lives at `utils/procurement_agent/tests/`.

**How to run it:**
```bash
uv sync --group dev
uv run pytest        # config in pyproject.toml [tool.pytest.ini_options]
```

History note: the suite was once reported as "289 passing" but unverifiable because the old venvs were broken (ABI mismatch). The blocker was environmental — the tests were always real. Do not repeat the old "no suite / 289 fictional" framing; it was wrong. The suite has since grown to 360 (characterization tests + the failure-handling fixes + the Apollo client tests added cases).

**Coverage is measured (not gated).** Current picture: `utils/` core is well-covered (intake 88%, spec-comparison 84%, sourcing 78%); `price_db.py` ~61%; `api_server.py` was 0% and is now ~77% after a deliberate characterization-testing pass that locked in every workflow endpoint's contract (including the background-task endpoints). **Before refactoring any area, confirm its coverage** — the net is real but uneven.

When asked to "run tests," actually run them (`uv run pytest`) and report the real count. Never fabricate a result.

### Next hardening steps
With a green baseline and the API characterization net, structural refactors are substantially unblocked — but verify per-area coverage first. Priority (see `docs/arkim_procurement_code_standard.md` §2):
1. Sourcing-quality fixes (Tier 2/3 leakage) — in well-covered `utils/`, safe.
2. The Apollo/search integration (see §9) — new, build to the integration patterns.
3. Structural refactors (DI, layered `app/`, exception hierarchy, CamelModel, `api_server.py` decomposition) — gated on per-area coverage; CamelModel + exception-contract changes are React-coupled (coordinate with the frontend, Arc 1). The api_server.py background-task endpoints had three failure-swallowing bugs fixed (sourcing failure now → `Phase.ERROR`; request-confirmation validates ids; send_message returns honest errors) — these are done; the mixed 404/409/422 detail-shape inconsistency is deferred to the exception-hierarchy refactor.

---

## 5. How to work here (conventions)

- **Investigation first.** For any non-trivial change, report findings before writing code. Don't start editing on a guess.
- **Show `git diff` (and `git status` for untracked files) before committing.** A known Claude Code failure mode is missing untracked files — check `git status`, not just `git diff`.
- **One commit per logical change. Don't bundle unrelated files.** Commit convention: **Conventional Commits** (`type: summary`, e.g. `fix: key price cache on (manufacturer, part_number)`) — there is no Jira yet. Once Jira is adopted, prefix with the ticket: `HEL-### type: summary`. Branches: `feature/<desc>` / `bugfix/<desc>` now; `feature/HEL-###-<desc>` once Jira exists. See `docs/arkim_procurement_code_standard.md` §3.
- **Keep Streamlit pages matched to `utils/` return shapes** when you change the core (see §2 gotcha).
- **Update `design/interactions.md`** in the same change as any user-facing behavior change.
- **Run the suite after every commit** (`uv run pytest`) and report the real result. It runs green (360) on `.venv`; never claim a pass you didn't run.
- **Stay in scope.** State out-of-scope items explicitly; don't drive-by refactor or expand scope. Watch for generic exception handlers that mask bugs.
- Follow the code standard (`docs/arkim_procurement_code_standard.md`) for all new code: injector DI, custom exceptions → central handlers → `{"detail": ...}`, CamelModel, layered `app/` structure, dense type annotations, stdlib pipe-delimited logging. Note the current code largely predates the standard — it's the target to converge toward, not the current state.
- **Building new code in a codebase that predates the standard.** Much of the existing code doesn't yet have DI / layered `app/` / the exception hierarchy. For new code: write it to the standard *where it can stand alone* (a new module — e.g. an Apollo client, a search adapter — should be clean, typed, tested, fail-soft from the start), but **don't retrofit the surrounding code mid-feature** (don't bolt DI onto api_server.py just to add one endpoint — that's a separate, coverage-gated refactor). Match the immediate surrounding module's conventions for integration points, build new standalone modules to the standard, and flag (in CLEANUP.md) where new clean code abuts old non-compliant code so the refactor boundary is visible. The goal is new code that Sergei accepts as-is — clean, tested, house-style — without it forcing a premature refactor of everything it touches.

---

## 6. Hard constraints & watch-outs

- **`utils/sourcing_archieved/`** (note the typo) is dead/archived code **still in the active import path** — `SourcingAgent` imports it. Do **not** touch it without auditing all `SourcingAgent` call sites first. Several CLEANUP items live here. (CLEANUP §1.1)
- **`EMAIL_SEND_ENABLED = False`** — Tier 3 outreach emails are never sent. The "Confirm outreach" flow marks vendors "Awaiting" with no real communication. Don't assume email send works. (CLEANUP §2.2)
- **Highest-risk debt items** (CLEANUP §4.1):
  - `price_db.py` cache PN-collision is **fixed** — keyed on `(manufacturer, part_number)`; old PN-only on-disk entries cleanly miss and re-populate (verified, no migration needed). Keep the composite key when touching this.
  - **RBAC is not enforced** — any caller can supply any `approver_role`; `approve` also ignores approval-rule thresholds. Acceptable for prototype, not production. (Needs auth infra — Arc 1.)
- **Two front ends, one core** (§2) — remember which layer your change affects.
- **Streamlit-bypasses-FastAPI** — hardening `api_server.py` validation does **not** protect the Streamlit path.

---

## 7. Where things live

```
app.py                      # Streamlit entry — switches to pages/sourcing_runs.py
pages/sourcing_runs.py      # Streamlit UI (imports utils/ directly)
api_server.py               # FastAPI app (~22 endpoints, thin wrapper over utils/)
frontend/                   # Next.js/React (calls FastAPI over HTTP)
utils/                      # THE CORE — harden here
  procurement_agent/        # orchestrator, agents, state (persistence, approval_rules)
  sourcing_archieved/       # DEAD — still imported; don't touch without auditing (§6)
  price_db.py               # price cache (PN-collision risk — §6)
  brand_intelligence.py     # LLM manufacturer-relationship cache
  ... (sourcing, scoring, intake, vision, quoting, audit_log)
data/                       # SQLite DBs (WAL), seeded handoffs
scripts/                    # CLI tools + the two live-integration probes (NOT unit tests)
design/interactions.md      # behavior documentation — keep in sync
```

---

## 8. Direction (so you don't optimize for the wrong target)

- **Backend hardening (`utils/`) is the current focus.** Stabilize the procurement workflows and their contracts first.
- **The React frontend is migrated/hardened in Arc 1**, against stable endpoints, after the backend settles. Streamlit is the fast build/demo harness for now and is retired then.
- **The deliberate divergences from the house standard** (Postgres + pgvector over DynamoDB, Alembic, pytest-as-a-gate, DBOS durable execution) are conscious choices — see the code standard §7. Build toward them; Sergei reviews once there's a hardened working product.
- Don't introduce new features on the Streamlit layer expecting them to be permanent — the durable surface is the React/FastAPI path.

---

## 9. Integration patterns (sourcing / supplier / external providers)

These are the **rules new sourcing and supplier code must follow** so each integration is consistent with the last. They encode architecture decisions from the rollout plan / BOM brief — follow them; don't reinvent per feature.

**External providers (search, Apollo, future APIs):**
- **Fail-soft, always.** An external-provider call must never raise into the sourcing pipeline. On error/timeout/rate-limit/missing-key → return `None`/empty and log; the pipeline degrades to the heuristic or skips the step, never crashes or hangs the run. (Same discipline as the failure-handling fixes in §4 — a failure must surface or degrade, never be swallowed as fake success *or* allowed to blow up the run.)
- **No-op cleanly without a key.** If a provider's API key is unset, its client returns empty and the system runs without it. Don't make a key mandatory for the pipeline to function.

**Search providers (Tavily, Parallel) sit behind ONE swappable interface:**
- New search providers implement the common interface and normalize their output into **one rich common candidate shape** — preserve richer fields (e.g. Parallel's confidence scores), don't flatten to the lowest-information provider's format.
- Provider choice is config/runtime, not hard-coded. Code that names a specific provider inline (`tavily_...`) outside the provider adapter is a smell — route through the interface.
- (Plan: run two providers behind the interface — dual free tiers + a real-world A/B; query-both early, then route-by-quality with per-provider credit tracking.)

**Paid API calls are cache-checked first:**
- **Always check the local store before spending a credit.** Supplier validation/contact (Apollo org-enrich = 1 credit) checks `supplier_registry.lookup_by_domain(domain)` first; only a cache miss calls the API, and the result is **written back** so the next encounter is free.
- Cache freshness: the enrich-date/re-enrich-on-staleness logic applies to `confirmed`-but-not-onboarded suppliers only. **Tier 1 / onboarded suppliers are exempt** — the onboarding relationship is their source of truth.

**Suitability before contact (Tier 3):**
- The search provider *discovers* domains; Apollo *validates* (US + requirement-match) and *resolves a contact*. They are sequential, not interchangeable. Validate suitability **before** spending a contact-resolution call on a candidate.
- An Apollo **miss does not auto-reject** a candidate — annotate it "unconfirmed, flag for human" (mirror the existing annotate-don't-remove suitability-floor pattern); auto-rejecting on a miss silently drops niche suppliers Apollo doesn't cover.
- **Never automate past a supplier's CAPTCHA / contact form.** Contact resolution order: cached store → Apollo people (sales/CS title + verified email) → generic inbox (`sales@`/`info@`) → human-in-the-loop. Structured quote submission means suppliers fill *Arkim's* form, not Arkim filling theirs.

> Full reasoning for these patterns is in the rollout plan §3a (Apollo clarifier) and the BOM placement brief (canonical identity, the three-level sameness / link-don't-merge / under-merge principles). When building the entity-resolution / equivalence layer, read those — and note that layer spans the `onboarding` and `core` services (cross-repo work, Arc 2), which must be reviewed before Layer 1 is built (confirm the `Assets.asset_model_id` FK and ingestion outputs).

---

*This file describes current reality. When the architecture changes (Postgres migration, durable execution, React migration, the Apollo/search integration landing), update this file in the same change.*
