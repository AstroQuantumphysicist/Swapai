"""Configuration, paths and .env handling for SwapAI."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv, set_key

APP_DIR = Path.home() / ".swapai"
ACCOUNTS_DIR = APP_DIR / "accounts"
USAGE_FILE = APP_DIR / "usage.jsonl"
ENV_FILE = APP_DIR / ".env"

# Codex CLI OAuth application (public client, PKCE flow)
OAUTH_ISSUER = "https://auth.openai.com"
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_PORT = 1455
OAUTH_REDIRECT_URI = f"http://localhost:{OAUTH_REDIRECT_PORT}/auth/callback"
OAUTH_SCOPES = "openid profile email offline_access"

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

# Candidate models probed per account; only the intersection across all
# accounts is exposed through the router.
CANDIDATE_MODELS = [
    "gpt-5",
    "gpt-5-codex",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "codex-mini-latest",
]

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
