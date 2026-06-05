# Arkim Procurement Agent — Code Quality & Testing Standard

**Status:** Internal — v1.0
**Derived from:** `arkim.core` and `assistantchat.messaging` house standards
**Open to review:** Sergei, once a hardened working product exists

---

## Purpose

This document defines the code-quality and testing standard for the procurement-agent repository. It **adopts the Arkim house standard verbatim** wherever the two reference services (`arkim.core`, `assistantchat.messaging`) agree, and makes a small number of **deliberate, justified upgrades** where the procurement agent does things the CRUD services don't.

The divergences are called out explicitly (Section 7) so they read as conscious architectural choices, not accidental drift — and so they're a clean basis for Sergei's review.

---

## 1. House Standard — Adopt Verbatim

Both reference repos agree on the following. These are not choices; they are Arkim conventions. The procurement agent matches them exactly so it slots into existing infrastructure with no friction.

### Dependency management
- **uv** with a committed **`uv.lock`** at repo root.
- **hatchling** build backend; `[tool.hatch.build.targets.wheel] packages = ["app"]`.
- **Python 3.11** (pin basedpyright and Docker base image to 3.11).
- Local setup: `uv venv .venv` then `uv sync --group dev`.
- Config/secrets via environment variables + `python-dotenv` (`.env` from a checked-in `.env.template` documenting every variable). No `pydantic-settings`, no central Settings object — read config at point of use via `os.environ` / `os.getenv`.

> **Migration note (done):** the procurement agent has moved to uv + `uv.lock` as the single dependency source. The legacy raw venvs (`venv`, `venv_win`) and `requirements.txt` have been removed.

### Linting & formatting — ruff
Copy this block verbatim into `pyproject.toml`:

```toml
[tool.ruff]
lint.select = [
    "E",   # pycodestyle errors
    "F",   # pyflakes
    "B",   # bugbear
    "UP",  # pyupgrade
    "I",   # isort
    "ARG", # unused arguments
]
lint.ignore = ["E501", "E741", "B904", "B008"]
```

- `ruff` subsumes black + isort + flake8. No standalone configs.
- Line length not enforced (E501 ignored); formatter reflows to 88 default but long lines/strings aren't flagged.
- Import ordering enforced via `I` (isort), auto-fixed.
- `B008` ignored because FastAPI `Depends`/`Injected()` defaults need it; `B904` ignored (bare `raise CustomException` without `from e` is house style).

### Type checking — basedpyright
Copy verbatim:

```toml
[tool.basedpyright]
pythonVersion = "3.11"
typeCheckingMode = "standard"
include = ["app"]
reportMissingImports = true
reportMissingTypeStubs = false
venvPath = "."
venv = ".venv"
```

- `standard` mode (not strict). Tests are not type-checked (`include = ["app"]`).
- Type annotations are dense throughout the house codebase and effectively required (the build fails on type errors). Match this: annotate every parameter and return, modern syntax (`X | None`, `list[...]`, `StrEnum`).

### Pre-commit
Copy verbatim (all hooks `language: system` — use the project's installed tools):

```yaml
repos:
  - repo: local
    hooks:
      - id: ruff-check
        name: ruff check
        entry: ruff check --fix
        language: system
        types: [python]
      - id: ruff-format
        name: ruff format
        entry: ruff format
        language: system
        types: [python]
      - id: basedpyright
        name: basedpyright
        entry: basedpyright
        language: system
        types: [python]
        pass_filenames: false
        always_run: true
```

> The procurement agent **adds a pytest hook** to this — see Section 2.

### Architecture & layering
Mirror the house layered layout under `app/`:

```
app/
  routes/           # thin FastAPI routers — function endpoints, no controller classes
  services/         # business logic; raise domain exceptions
  infrastructure/
    clients/        # boto3 / httpx / Anthropic / Tavily wrappers
    repositories/   # persistence (Postgres via SQLAlchemy — see §7)
    http/           # typed clients to sibling services
  models/           # Pydantic models
  middleware/
  utils/
  di_container.py
  main.py
  exception_handlers.py
tests/
  unit/             # mirrors app/ — see §2
```

- **No `__init__.py`** in `app/` (namespace packages, per `arkim.core` rule). `tests/` may use `__init__.py` where pytest discovery needs it.
- Routes are thin; business logic lives in services; persistence in repositories; external boundaries in clients.

### Dependency injection — injector + fastapi-injector
This is "the single most important house pattern." Adopt exactly:
- `@inject` on every service/repository `__init__`; dependencies are typed constructor params (concrete classes, no interfaces).
- Centralized wiring in `app/di_container.py` (`ApplicationModule.configure` with `binder.bind(Concrete, Concrete)`, `.in_singleton_scope()` for expensive clients).
- Routes receive deps via `Injected(ConcreteClass)`, never raw FastAPI `Depends()`.
- No global singletons, no module-level instantiation.

### Error handling
- Custom exception hierarchy in `app/models/exceptions.py`: a `BusinessException` base with `.message`, subclasses `NotFoundException`, `AlreadyExistsException`, `ValidationException`, `ForbiddenException`, `InternalServerException`.
- Services raise semantic exceptions; they never build HTTP responses.
- Central mapping in `app/exception_handlers.py` (`register_exception_handlers(app)`): NotFound→404, AlreadyExists→409, Validation→400, Forbidden→403, InternalServer→500, generic Business→400, unhandled→500.
- Uniform response envelope: `{"detail": "<message>"}` for 4xx; `{}` or `{"detail": "Internal server error"}` for 500; 422 left as FastAPI default.
- HTTP/infra clients log rich context and re-raise (don't swallow). Service layer may degrade gracefully (return an error-result object) where that's the right behavior.

### API schema & casing
- Pydantic v2 everywhere. Request/response models extend `CamelModel` (fastapi-camelcase): snake_case in Python/DB, camelCase on the wire.
- Enums are `StrEnum`.
- App mounted under a service prefix (e.g. `/api/procurement`) with docs at `/api/procurement/docs`.

### Logging
- stdlib `logging`, `logger = logging.getLogger(__name__)` per module.
- Central config in `app/utils/logging_config.py`: level INFO, pipe-delimited format `%(asctime)s | %(levelname)-8s | %(name)s | %(message)s` to stdout (CloudWatch/Grafana friendly), noisy libs (httpx/botocore/boto3/urllib3) → WARNING, health/docs access logs filtered.
- Log warnings/errors (and necessary background-task info) only — no debug noise. Error handlers use structured `extra={...}` with `exc_info=True`.
- Pipe-delimited text, not JSON.

### Service-to-service calls
- Cross-service HTTP goes through a shared `ServiceCallingFactory` producing `httpx.AsyncClient` wrappers that auto-forward identity headers (`authorization`, `company_id`, `INTERNAL_REQUEST_SIGNATURE`, `X-Arkim-*`) from the inbound request via a context manager. Per-service base URLs from `<NAME>_SERVICE_URL` env vars. Never raw `httpx` for sibling-service calls.

### Async-first
- Entire stack is async (`async def`, `AsyncMock`, `httpx.AsyncClient`). `asyncio_mode = "auto"` in pytest (async tests need no decorator).

### Containerization & deploy
- Single-stage `python:3.11-slim` Dockerfile that **bakes the quality gate into the build**.
- Deploys tag-triggered to AWS via Bitbucket Pipelines + OIDC (`dev-*` / `test-*` / `prod-*` tags → `deploy.sh`).
- Match the house `bitbucket-pipelines.yml` shape.

---

## 2. Testing — Stronger Than Reference (deliberate upgrade)

**The most important divergence.** Both reference repos run **zero tests in CI** — the Docker gate is lint + type + format only, pytest never runs automatically, no coverage anywhere. `assistantchat.messaging` has good unit-test *patterns* but they're not enforced; `arkim.core` has no unit tests at all (only a live-server integration runner).

The procurement agent is the codebase being **hardened and refactored heavily** (entity resolution, canonical BOM, equivalence engine, durable-execution migration). That work is exactly what needs a real regression gate — and exactly what the "tests exist but never run" pattern fails to provide. So the procurement agent makes testing a real gate.

### Adopt the messaging repo's pytest patterns exactly
- **Framework:** pytest + pytest-asyncio (`asyncio_mode = "auto"`) + pytest-mock available.
- **Config** (copy verbatim):

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
python_files = ["test_*.py", "*_test.py"]
python_classes = ["Test*"]
python_functions = ["test_*"]
addopts = [
    "-v",
    "--tb=short",
    "--strict-config",
    "--strict-markers",
]
```

- **Structure:** `tests/unit/` mirroring `app/` (`clients/`, `services/`, `nodes/`, `utils/`, etc.). Classes `Test<Subject>`, methods `test_<behavior>`, Arrange/Act/Assert blocks, fixtures defined locally per test class.
- **conftest:** `tests/unit/conftest.py` with a `test_app` / `test_client` fixture pair built from a test DI injector. No root conftest.

### Mocking patterns (the most important part for an agent)
The procurement agent has heavy LLM (Anthropic) + web-search (Tavily) surface. Use the messaging repo's two patterns:

1. **DI-level mocking via a test injector** — a `TestInfrastructureModule` that mocks only the infrastructure clients (DB, Tavily, Anthropic) and layers over the real `ApplicationModule`, so business logic stays real:

```python
class TestInfrastructureModule(Module):
    @singleton
    @provider
    def provide_tavily_client(self) -> TavilyClient:
        mock = Mock(spec=TavilyClient)
        mock.search = AsyncMock(return_value=...)
        return mock

def create_test_injector() -> Injector:
    return Injector([TestInfrastructureModule, ApplicationModule])
```

2. **LLM mocked at the SDK boundary** — `Mock(spec=Client)` + `AsyncMock` for the Anthropic/Bedrock client, with hand-built response dicts matching the SDK schema. Assert the exact outbound call via `assert_called_once_with(...)`.

- Use `unittest.mock` (`Mock(spec=...)`, `AsyncMock`, `patch`) as the primary mocking library, consistent with the house pattern. `respx`/`pytest-mock` available if a specific HTTP-level mock is cleaner.
- **Mock external services at the client boundary** — never hit live Tavily/Anthropic in unit tests. The suite must run offline and green as a regression gate.

### The upgrade: pytest runs as a real gate
Two additions neither reference repo has:

1. **Pre-commit pytest hook** — add to `.pre-commit-config.yaml`:

```yaml
      - id: pytest
        name: pytest (unit)
        entry: pytest tests/unit
        language: system
        types: [python]
        pass_filenames: false
        always_run: true
```

2. **pytest in the Docker build gate** — alongside lint/type/format, so a failing test fails the build the same way a type error does:

```dockerfile
RUN . "${VENV_PATH}/bin/activate" && \
    ruff check app/ && \
    ruff format --check app/ && \
    basedpyright && \
    pytest tests/unit
```

> Unit tests only in the build gate (offline, mocked). Live-integration probes (real Tavily/Anthropic) stay manual / separate, as in the house repos.

### Coverage — measured, not blocking (initially)
- Add `pytest-cov`; produce a coverage report in CI but **no hard threshold yet** (a threshold on a young suite is busywork).
- Measure from day one to see gaps; introduce a threshold once the suite matures.

### What to test first (the hardening prerequisite)
The first hardening task is standing up the suite over the **pure, deterministic backend logic** — the surfaces that have generated bugs and that the refactor will touch:
- Scoring functions (TCA/TLV weighting, suitability, authority scoring).
- Part-number normalization / prefix lookup (the entity-resolution foundation).
- Cross-tier dedup, suitability floor, rejection-reason precedence (the filter logic with a bug history — Sealit123 variants, no_match leakage).
- Intake sufficiency assessment (the gate logic behind the over-asking issues).

These are pure-ish, testable offline without Tavily/Anthropic. Establish a real green baseline here **before** refactoring `utils/`.

---

## 3. Commit & Branch Convention

Match the existing Arkim workflow (both reference repos use Jira ticket IDs; branch naming follows the house pattern):
- **Commits:** `HEL-### <summary>` (e.g. `HEL-901 Add canonical part entity and many-to-many BOM association`).
- **Branches:** `feature/HEL-###-<short-desc>`, `bugfix/HEL-###-<short-desc>`.
- One commit per logical change. Show diff before committing. (Working conventions carried from prior build discipline.)

---

## 4. Documentation Discipline

- Module-level triple-quoted summary on every file; class docstring stating responsibility; method docstrings in imperative mood, with `Args:`/`Returns:` on non-trivial methods.
- Comments only for non-obvious logic.
- **Update `/design/interactions.md` in the same change as any user-facing behavior change** (carried convention).
- DRY / KISS / YAGNI; remove dead code on refactor (no commented-out blocks).

---

## 5. The Quality Gate (summary)

A change must pass, locally (pre-commit) and in the Docker build:
1. `ruff check` (lint)
2. `ruff format --check` (format)
3. `basedpyright` (type check, standard mode)
4. **`pytest tests/unit`** (the procurement-agent upgrade)

Plus, measured but not blocking: coverage report.

---

## 6. Persistence — Deliberate Divergence (Postgres, not DynamoDB)

**The reference services use DynamoDB** (no ORM, no migrations, schemaless). **The procurement agent uses SQLAlchemy + Postgres** (currently SQLite, Postgres-ready). This is a conscious, justified divergence — flagged for Sergei's review.

**Why Postgres, not DynamoDB:**
- **Entity resolution & the canonical BOM** are relational by nature — parts ↔ equipment-models many-to-many, cross-manufacturer equivalences, part-to-datasheet associations. These are join-heavy relational queries that DynamoDB serves poorly.
- **Spec-equivalence and the relationship graph** want `pgvector` (vector retrieval over spec sheets) and graph-style queries over canonical entities. pgvector keeps vectors next to relational data with no separate sync layer.
- **Durable execution (DBOS)** — the chosen seed-stage substrate — is Postgres-native (library + Postgres, no separate cluster). Choosing Postgres for the data layer and DBOS for durability is one coherent decision.

**Consequence — Alembic (a second deliberate addition):**
The house standard has **no migration tooling** (DynamoDB is schemaless). A Postgres procurement agent with a real relational schema (canonical parts, BOM associations, tenant overrides) **needs migrations**. Adopt **Alembic**:
- Add Alembic, write an initial migration from the current schema.
- Replace `create_all()` with `alembic upgrade head` in the startup path.
- (Already flagged in `CLEANUP.md` (repo root) §3.2.)

This is "stronger than reference" by necessity, not preference — the data model demands it.

---

## 7. Summary of Deliberate Divergences (for Sergei's review)

Everything in Sections 1, 3, 4 matches the house standard verbatim. The procurement agent deviates in exactly four places, each justified by what it does that the CRUD services don't:

| Divergence | House standard | Procurement agent | Why |
|---|---|---|---|
| **Test gate** | Tests exist but never run in CI; no coverage | pytest runs in pre-commit + Docker build; coverage measured | Heavy hardening/refactor needs a real regression net the CRUD services don't |
| **Persistence** | DynamoDB, schemaless, no ORM | SQLAlchemy + Postgres + pgvector | Relational BOM, entity resolution, vector retrieval, graph queries fit Postgres, not DynamoDB |
| **Migrations** | None (schemaless) | Alembic | A real relational schema needs managed migrations |
| **Durable execution** | None | DBOS (Postgres-native), seed-stage | Long-running, human-gated, audit-critical workflow can't hold state in process memory |

Everything else — uv, ruff, basedpyright, pre-commit, Docker-as-gate, injector DI, exception hierarchy, CamelModel, layered layout, stdlib logging, env-var config, HEL/Jira commits, async-first — is the house standard unchanged.

---

*These divergences are documented as conscious choices so they can be reviewed, not discovered. Sergei to weigh in once a hardened working product exists to react to.*
