"""Codex OAuth accounts: login (PKCE), storage, refresh, and rate-limit state."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, field, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

from . import config


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_jwt(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


@dataclass
class RateWindow:
    """A single ChatGPT usage window (e.g. 5h primary or weekly secondary)."""

    label: str = ""
    used_percent: float = 0.0
    window_minutes: int = 0
    resets_at: float = 0.0  # epoch seconds

    @property
    def remaining_percent(self) -> float:
        return max(0.0, 100.0 - self.used_percent)


@dataclass
class Account:
    id: str
    email: str = ""
    plan: str = ""
    account_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expires_at: float = 0.0
    models: list[str] = field(default_factory=list)
    primary: RateWindow = field(default_factory=RateWindow)
    secondary: RateWindow = field(default_factory=RateWindow)
    last_error: str = ""
    disabled_until: float = 0.0  # epoch; account skipped while now < this
    # ---- learned capacity (tiktoken calibration) --------------------
    window_tokens: float = 0.0          # tiktoken tokens used this 5h window
    learned_tokens_per_5h: float = 0.0  # inferred real capacity
    calib_last_percent: float = 0.0     # last observed primary used-percent
    calib_confidence_percent: float = 0.0  # used-% at which estimate was taken
    calib_anchor_reset: float = 0.0     # resets_at we're tracking the window by

    # ---- persistence -------------------------------------------------
    @property
    def path(self) -> Path:
        return config.ACCOUNTS_DIR / f"{self.id}.json"

    def save(self) -> None:
        config.ensure_dirs()
        data = asdict(self)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Account":
        data = json.loads(path.read_text(encoding="utf-8"))
        prim = RateWindow(**data.pop("primary", {}) or {})
        sec = RateWindow(**data.pop("secondary", {}) or {})
        acc = cls(**{k: v for k, v in data.items()
                     if k in cls.__dataclass_fields__})
        acc.primary = prim
        acc.secondary = sec
        return acc

    def delete(self) -> None:
        if self.path.exists():
            self.path.unlink()

    # ---- state helpers ----------------------------------------------
    @property
    def is_rate_limited(self) -> bool:
        return time.time() < self.disabled_until

    @property
    def status(self) -> str:
        if self.is_rate_limited:
            return "limited"
        if self.last_error:
            return "error"
        return "ready"

    def token_expired(self) -> bool:
        return time.time() >= (self.expires_at - 60)


def list_accounts() -> list[Account]:
    config.ensure_dirs()
    out = []
    for p in sorted(config.ACCOUNTS_DIR.glob("*.json")):
        try:
            out.append(Account.load(p))
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# OAuth (PKCE) login
# ---------------------------------------------------------------------------
class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state_expected: str = ""
    error: str | None = None

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if not parsed.path.startswith("/auth/callback"):
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("state", [""])[0] != _CallbackHandler.state_expected:
            _CallbackHandler.error = "state mismatch"
        else:
            _CallbackHandler.code = qs.get("code", [None])[0]
            _CallbackHandler.error = qs.get("error", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body style='font-family:sans-serif;background:#0b0e14;"
            b"color:#e6e6e6;text-align:center;padding-top:80px'>"
            b"<h1>SwapAI</h1><p>Login complete. You can close this tab and "
            b"return to the terminal.</p></body></html>"
        )

    def log_message(self, *_):  # silence
        return


class LoginFlow:
    """Drives a single interactive Codex OAuth login."""

    def __init__(self) -> None:
        self.verifier = _b64url(secrets.token_bytes(48))
        challenge = _b64url(hashlib.sha256(self.verifier.encode()).digest())
        self.state = _b64url(secrets.token_bytes(24))
        params = {
            "response_type": "code",
            "client_id": config.OAUTH_CLIENT_ID,
            "redirect_uri": config.OAUTH_REDIRECT_URI,
            "scope": config.OAUTH_SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        self.auth_url = (
            f"{config.OAUTH_ISSUER}/oauth/authorize?"
            + urllib.parse.urlencode(params)
        )
        self._server: HTTPServer | None = None

    def open_browser(self) -> None:
        try:
            webbrowser.open(self.auth_url)
        except Exception:
            pass

    def wait_for_code(self, timeout: float = 300) -> str:
        _CallbackHandler.code = None
        _CallbackHandler.error = None
        _CallbackHandler.state_expected = self.state
        self._server = HTTPServer(("localhost", config.OAUTH_REDIRECT_PORT),
                                  _CallbackHandler)
        self._server.timeout = 1
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._server.handle_request()
            if _CallbackHandler.code or _CallbackHandler.error:
                break
        self._server.server_close()
        if _CallbackHandler.error:
            raise RuntimeError(f"OAuth error: {_CallbackHandler.error}")
        if not _CallbackHandler.code:
            raise TimeoutError("Timed out waiting for OAuth callback")
        return _CallbackHandler.code

    def exchange(self, code: str) -> Account:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{config.OAUTH_ISSUER}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": config.OAUTH_REDIRECT_URI,
                    "client_id": config.OAUTH_CLIENT_ID,
                    "code_verifier": self.verifier,
                },
            )
            resp.raise_for_status()
            tok = resp.json()
        return _account_from_tokens(tok)


def _account_from_tokens(tok: dict) -> Account:
    access = tok.get("access_token", "")
    refresh = tok.get("refresh_token", "")
    id_token = tok.get("id_token", "")
    expires_in = tok.get("expires_in", 3600)
    claims = _decode_jwt(id_token) or _decode_jwt(access)
    email = claims.get("email", "")
    auth_claims = claims.get("https://api.openai.com/auth", {}) or {}
    account_id = auth_claims.get("chatgpt_account_id", "")
    plan = auth_claims.get("chatgpt_plan_type", "") or claims.get(
        "chatgpt_plan_type", "")
    acc = Account(
        id=secrets.token_hex(6),
        email=email or account_id or "unknown",
        plan=plan,
        account_id=account_id,
        access_token=access,
        refresh_token=refresh,
        id_token=id_token,
        expires_at=time.time() + expires_in,
    )
    return acc


def refresh_account(acc: Account) -> bool:
    """Refresh the access token in place. Returns True on success."""
    if not acc.refresh_token:
        return False
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{config.OAUTH_ISSUER}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": acc.refresh_token,
                    "client_id": config.OAUTH_CLIENT_ID,
                },
            )
            resp.raise_for_status()
            tok = resp.json()
        acc.access_token = tok.get("access_token", acc.access_token)
        if tok.get("refresh_token"):
            acc.refresh_token = tok["refresh_token"]
        acc.expires_at = time.time() + tok.get("expires_in", 3600)
        acc.last_error = ""
        acc.save()
        return True
    except Exception as exc:  # noqa: BLE001
        acc.last_error = f"refresh failed: {exc}"
        acc.save()
        return False
