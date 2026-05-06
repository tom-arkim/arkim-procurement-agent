# Compatibility shim — redirects utils.sourcing.* to utils.sourcing_archieved.*
# Required because the archived modules import from utils.sourcing.* (old path).
# The mutable state attributes below mirror sourcing_archieved/__init__.py so that
# llm_parsing._anthropic_complete() can read ANTHROPIC_API_KEY and _EXTRACTION_MODEL
# via `import utils.sourcing as _pkg` at call time. SourcingAgent._patch_sourcing_keys()
# writes to both this module and utils.sourcing_archieved at runtime.

import os

try:
    from tavily import TavilyClient as _TavilyClient
except ImportError:
    _TavilyClient = None

TAVILY_API_KEY    = os.environ.get("TAVILY_API_KEY")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
_EXTRACTION_MODEL = os.environ.get("OS_EXTRACTION_MODEL", "claude-haiku-4-5-20251001")
_tavily           = _TavilyClient(api_key=TAVILY_API_KEY) if TAVILY_API_KEY and _TavilyClient else None
