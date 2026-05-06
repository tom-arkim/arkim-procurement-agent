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
