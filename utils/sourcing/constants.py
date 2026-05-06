# Shim: scoring.py and orchestrator.py import from utils.sourcing.constants.
# By the time this runs, utils.sourcing_archieved.constants is already in
# sys.modules (it is the first import in sourcing_archieved/__init__.py).
from utils.sourcing_archieved.constants import *  # noqa: F401,F403
