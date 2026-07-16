"""
Shared pytest fixtures for procurement_agent tests.

Each test session gets a fresh SQLite database at data/test_procurement.sqlite.
The fixture drops and recreates all tables before every test function to
guarantee isolation without file I/O overhead of creating a new file each time.
"""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Ensure project root is importable when running pytest from any directory.
import sys
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)
))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from utils.procurement_agent.state.persistence import Base, _make_engine

_DATA_DIR = os.path.join(_ROOT, "data")
_TEST_DB_PATH = os.path.join(_DATA_DIR, "test_procurement.sqlite")
TEST_DB_URL = f"sqlite:///{_TEST_DB_PATH}"


@pytest.fixture(autouse=True)
def _neutralize_external_api_keys(monkeypatch):
    """Session-wide safety net: no test can make a live external API call.

    pytest loads .env (python-dotenv is a project dependency), so real
    APOLLO/ANTHROPIC/TAVILY keys are present in the test process. Setting them
    empty for EVERY test means a default-constructed client (e.g. ApolloClient())
    is DISABLED unless a test explicitly injects a key — so accidental live calls
    / credit spend (in dev or CI) are structurally impossible, not merely
    discouraged by "remember to mock".

    Mirrors the per-fixture neutralization in test_api_server.py: empty (not
    deleted) because load_dotenv(override=False) skips already-set vars; the
    ApolloClient reads APOLLO_API_KEY from os.environ at construction time, so the
    empty value disables it. monkeypatch reverts after each test. A test that
    needs a key sets it explicitly (its own setenv/api_key=... runs after this
    autouse setup and wins).
    """
    for var in ("APOLLO_API_KEY", "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "PARALLEL_API_KEY"):
        monkeypatch.setenv(var, "")
    # Gmail is an external call too: with the google libs now installed, a real
    # GMAIL_SERVICE_ACCOUNT_FILE in .env would let a test build a live service and hit
    # the network (observed: fetch_replies returning real inbox data). Empty these so
    # the send/read path is uncredentialled by default — a test that needs creds injects
    # a mock service or sets the var explicitly (its setenv runs after this and wins).
    for var in ("GMAIL_SERVICE_ACCOUNT_JSON", "GMAIL_SERVICE_ACCOUNT_FILE",
                "GMAIL_OAUTH_TOKEN_FILE"):
        monkeypatch.setenv(var, "")
    # The send gate now reads EMAIL_SEND_ENABLED from the env (default off, opt-in). A
    # real .env can set it True, and pytest loads .env — so force the gate OFF for every
    # test, deterministically and regardless of import order. No test can send by
    # accident; a test that needs the live path sets it True explicitly (runs after this
    # autouse setup and wins).
    import utils.email_sender as _email_sender
    monkeypatch.setattr(_email_sender, "EMAIL_SEND_ENABLED", False)


# Every feature flag the codebase reads, with the module attributes that BIND the
# flag at import time (the attr is what matters once the module is loaded — the
# env pin alone can't fix an already-imported module, and a setattr alone can't
# fix a module imported later in the session). Maintained by grep over the
# os.environ flag-read sites; add new flags here when they're introduced.
_FEATURE_FLAG_ENVS = (
    "TIER1_V2", "RANKING_BANDS_V1", "SUPPLIER_PORTAL_V1", "SEND_GOVERNANCE_V1",
    "INTAKE_CHANNELS_V1", "DEMO_MODE", "INTAKE_TYPE_AWARE", "SCORING_V2",
    "RUN_CAPTURE", "QUOTE_SUBMIT_V1",
)
# (module in sys.modules, attribute, pinned default) — import-bound flag bindings.
_FEATURE_FLAG_MODULE_ATTRS = (
    ("utils.supplier_registry",          "TIER1_V2",              False),
    ("utils.sourcing_archieved.scoring", "SCORING_V2",            False),
    ("api_server",                       "DEMO_MODE",             False),
    ("api_server",                       "SUPPLIER_PORTAL_V1",    False),
    ("utils.claim_tokens",               "CLAIM_TOKENS_ENABLED",  False),
    ("utils.run_capture",                "RUN_CAPTURE",           False),
    ("utils.run_labels",                 "RUN_CAPTURE",           False),
    ("utils.eval_export",                "RUN_CAPTURE",           False),
)


@pytest.fixture(autouse=True)
def _pin_feature_flags_off(monkeypatch):
    """Session-wide flag hygiene: every feature flag is pinned to its DEFAULT-OFF
    state for every test, so the suite's behavior cannot depend on the shell
    environment ($env: leakage) or on which test ran first (module reloads under
    a flag — the TIER1_V2 leak that broke TestConfirmIntake three times).

    Two layers, following the TIER1_V2 idiom in test_sourcing_agent.py:
      1. env pinned to "" (strict-truthy parses that as off; empty-not-deleted so
         load_dotenv(override=False) can't resurrect a value) — this makes any
         LATER first-import bind the flag off;
      2. the import-bound module ATTRIBUTES are set False for modules already in
         sys.modules — this fixes a module imported earlier under a polluted env.
         Modules are looked up, never imported here (importing api_server from
         conftest would run its module-level DB init before per-test patching).

    A test that exercises a flag opts in explicitly (its own monkeypatch
    setenv/setattr runs after this autouse setup and wins) — same contract as
    _neutralize_external_api_keys above. EMAIL_SEND_ENABLED is pinned there.
    """
    for var in _FEATURE_FLAG_ENVS:
        monkeypatch.setenv(var, "")
    for mod_name, attr, default in _FEATURE_FLAG_MODULE_ATTRS:
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, attr):
            monkeypatch.setattr(mod, attr, default)


@pytest.fixture(scope="function")
def db_url():
    """Return the test DB URL and reset tables before each test."""
    os.makedirs(_DATA_DIR, exist_ok=True)
    engine = _make_engine(TEST_DB_URL)
    # Drop and recreate all tables for a clean slate.
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    engine.dispose()
    yield TEST_DB_URL
    # Cleanup: drop tables after test (leave file for inspection if needed).
    engine2 = _make_engine(TEST_DB_URL)
    Base.metadata.drop_all(engine2)
    engine2.dispose()
