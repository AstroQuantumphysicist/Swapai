"""Configuration, paths and .env handling for SwapAI."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv, set_key

APP_DIR = Path.home() / ".swapai"
ACCOUNTS_DIR = APP_DIR / "accounts"
USAGE_FILE = APP_DIR / "usage.jsonl"
USAGE_STATE_FILE = APP_DIR / "usage.json"
ENV_FILE = APP_DIR / ".env"

# Codex CLI OAuth application (public client, PKCE flow)
OAUTH_ISSUER = "https://auth.openai.com"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_PORT = 1455
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_REDIRECT_PORT}/auth/callback"
OAUTH_SCOPES = "openid profile email offline_access"

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Mirrors the official Codex CLI: User-Agent and `version` header must
# follow the `{originator}/{version}` format or the backend rejects the
# request. Bump this when the wire format changes.
CODEX_ORIGINATOR = "codex_cli_rs"
CODEX_CLIENT_VERSION = os.environ.get("SWAPAI_CODEX_CLIENT_VERSION", "0.133.0")

# Default Codex-compatible model. The backend will accept a request for
# any string, but unknown names silently get mapped to a default — use
# this explicitly so the user sees the real model name in the dashboard.
DEFAULT_CODEX_MODEL = "gpt-5.4"


def _candidate_models() -> list[str]:
    """Use the installed Codex model cache, with a current fallback list."""
    cache = Path.home() / ".codex" / "models_cache.json"
    try:
        data = json.loads(cache.read_text(encoding="utf-8"))
        rows = data.get("models", data) if isinstance(data, dict) else data
        models = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            model = row.get("slug") or row.get("id") or row.get("model")
            if model and model not in models:
                models.append(model)
        if models:
            return models
    except (OSError, ValueError, TypeError):
        pass
    return ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]


# Candidate models probed per account; only the intersection across all
# accounts is exposed through the router.
CANDIDATE_MODELS = _candidate_models()

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8788


def ensure_dirs() -> None:
    ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    ensure_dirs()
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)
    # a project-local .env wins if present
    load_dotenv(override=True)


def get_api_key() -> str | None:
    return os.environ.get("SWAPAI_API_KEY") or None


def set_api_key(value: str) -> None:
    ensure_dirs()
    ENV_FILE.touch(exist_ok=True)
    set_key(str(ENV_FILE), "SWAPAI_API_KEY", value)
    os.environ["SWAPAI_API_KEY"] = value


def generate_api_key() -> str:
    return "sk-swapai-" + secrets.token_urlsafe(32)


def get_port() -> int:
    try:
        return int(os.environ.get("SWAPAI_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT


def get_host() -> str:
    return os.environ.get("SWAPAI_HOST", DEFAULT_HOST)
