"""Quick diagnostic: verify API key loads and Anthropic API responds."""
import os, sys, requests

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

key = os.environ.get("ANTHROPIC_API_KEY", "")
print(f"API key present : {bool(key)}")
print(f"API key preview : {key[:20]}..." if key else "API key preview : (empty)")

if not key:
    print("ERROR: ANTHROPIC_API_KEY not loaded — check .env file")
    sys.exit(1)

print("\nTesting claude-sonnet-4-6 ...")
try:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-sonnet-4-6",
            "max_tokens": 50,
            "messages":   [{"role": "user", "content": "Reply with exactly: OK"}],
        },
        timeout=20,
    )
    print(f"Status : {resp.status_code}")
    print(f"Body   : {resp.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")

print("\nTesting claude-haiku-4-5-20251001 ...")
try:
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key":         key,
            "anthropic-version": "2023-06-01",
            "content-type":      "application/json",
        },
        json={
            "model":      "claude-haiku-4-5-20251001",
            "max_tokens": 50,
            "messages":   [{"role": "user", "content": "Reply with exactly: OK"}],
        },
        timeout=20,
    )
    print(f"Status : {resp.status_code}")
    print(f"Body   : {resp.text[:500]}")
except Exception as e:
    print(f"Exception: {e}")
