# CLAUDE.md

Operational guide for Claude Code working in the Arkim Procurement Agent repository.
Read this first, every session. It describes the repo **as it is today**, not as it will be.

> **Companion documents (deeper context — read when relevant; paths relative to repo root):**
> - `docs/arkim_procurement_code_standard.md` — the code-quality & testing standard this repo follows. **Authoritative for how to write and test code.**
> - `docs/arkim_procurement_agent_brief.md` — product/architecture intent (what the system is and why).
> - `CLEANUP.md` (repo root) — known technical debt backlog.
> - Rollout plan (sequencing — what's built when, by whom): **internal-only, deliberately not committed to this repo.** See §0.1.

### 0.1 Repo visibility — this repo is PRIVATE (was public)

`github.com/tom-arkim/arkim-procurement-agent` is now a **private** GitHub repository (confirmed: an unauthenticated fetch returns 404). It was **public earlier in development** — treat anything committed before the visibility flip as having been publicly visible. A full-history secret scan found **no committed credentials**, so exposure risk is low; rotate anything you're unsure about. Guidance that still holds:

- **The rollout plan stays out of the repo.** It names who builds what when (internal sequencing + personnel); keep it in an internal channel (Notion/Drive). A private repo makes this lower-stakes, but there's no reason to track it here.
- **Don't commit secrets regardless of visibility.** This file and `docs/arkim_procurement_code_standard.md` name an internal reviewer (Sergei) and §6 below enumerates live security gaps (RBAC not enforced, price-cache collision, email-send disabled) — fine for a private internal repo, but never add credentials or customer data, and don't treat "private" as licence to.

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

- **Two virtualenvs exist: `venv` and `venv_win`.** On Windows use `venv_win`. Confirm which has deps installed before running.
- Required env: `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`. Optional model overrides: `OS_EXTRACTION_MODEL`, `BRAND_INTEL_MODEL`, `SPEC_ENRICH_MODEL` (default `claude-haiku-4-5`).
- Dev reseed endpoint: `/api/dev/reseed-handoffs` reloads seeded maintenance handoffs.

---

## 4. Testing — READ THIS

**A pytest suite EXISTS but is not yet runnable as a gate.** ~3,354 lines across 11 files at `utils/procurement_agent/tests/` — real `Test*` classes, per-function DB-isolation fixtures (`conftest.py`), offline mocking via `patch("requests.post")` + internal-function patches — covering live code (intake, sourcing, orchestrator, persistence, approval_rules, outreach, spec_comparison, brand_intelligence) plus some archived modules. Separately, `scripts/` holds live-integration probes (`test_dynamic_discovery.py`, `test_llm.py`) that hit real Tavily/Anthropic; those are not unit tests.

**Why it is not yet a regression net:**
- **Not runnable in-place** — both venvs (`venv`, `venv_win`) are broken (built from a now-missing Anaconda at a pre-rename path); borrowing site-packages fails on `tiktoken`'s compiled extension. The suite has **not been executed here**, so its current green/red status is unconfirmed.
- **No pytest config** (`pyproject.toml` / `[tool.pytest.ini_options]`) and **no gate** (no pre-commit, no Docker build step).
- Suite lives at `utils/procurement_agent/tests/`, not the standard's `tests/unit/`.

### The first hardening task
**Make the existing suite runnable and wire it into a gate** (per `docs/arkim_procurement_code_standard.md` §2): repair the env (uv + `uv.lock`), add the pytest config, establish a **green baseline**, then add the pre-commit + Docker-build gate. This supersedes any earlier "write a suite from scratch" framing — the suite already exists; it needs an environment and a gate, not authoring.

Until a green baseline is confirmed: when asked to "run tests," do **not** report pass/fail you have not actually observed — the environment does not run yet.

---

## 5. How to work here (conventions)

- **Investigation first.** For any non-trivial change, report findings before writing code. Don't start editing on a guess.
- **Show `git diff` (and `git status` for untracked files) before committing.** A known Claude Code failure mode is missing untracked files — check `git status`, not just `git diff`.
- **One commit per logical change.** Conventional house style: `HEL-### <summary>` (e.g. `HEL-901 Add canonical part entity`). Branches `feature/HEL-###-<desc>` / `bugfix/HEL-###-<desc>`.
- **Keep Streamlit pages matched to `utils/` return shapes** when you change the core (see §2 gotcha).
- **Update `/design/interactions.md`** in the same change as any user-facing behavior change.
- **Once a test suite exists, run `pytest tests/unit` after every commit** and report the real result.
- **Stay in scope.** State out-of-scope items explicitly; don't drive-by refactor or expand scope. Watch for generic exception handlers that mask bugs.
- Follow the code standard (`docs/arkim_procurement_code_standard.md`) for all new code: injector DI, custom exceptions → central handlers → `{"detail": ...}`, CamelModel, layered `app/` structure, dense type annotations, stdlib pipe-delimited logging.

---

## 6. Hard constraints & watch-outs

- **`utils/sourcing_archieved/`** (note the typo) is dead/archived code **still in the active import path** — `SourcingAgent` imports it. Do **not** touch it without auditing all `SourcingAgent` call sites first. Several CLEANUP items live here. (CLEANUP §1.1)
- **`EMAIL_SEND_ENABLED = False`** — Tier 3 outreach emails are never sent. The "Confirm outreach" flow marks vendors "Awaiting" with no real communication. Don't assume email send works. (CLEANUP §2.2)
- **Highest-risk debt items** (CLEANUP §3.3, §4.1):
  - `price_db.py` cache is keyed by **part number only** — a manufacturer collision silently serves the wrong price. Key on `(manufacturer, part_number)` when touching this.
  - **RBAC is not enforced** — any caller can supply any `approver_role`. Acceptable for prototype, not production.
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

*This file describes current reality. When the architecture changes (test suite stood up, Postgres migration, durable execution, React migration), update this file in the same change.*
