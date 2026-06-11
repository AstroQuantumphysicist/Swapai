"""Low-level client for the ChatGPT Codex backend + rate-limit parsing."""

from __future__ import annotations

import json
import time
import uuid

import httpx

from . import config
from .accounts import Account, RateWindow, refresh_account


def _headers(acc: Account) -> dict:
    h = {
        "Authorization": f"Bearer {acc.access_token}",
        "Content-Type": "application/json",
        "OpenAI-Beta": "responses=experimental",
        "originator": "codex_cli_rs",
        "User-Agent": "swapai/0.1 (codex-router)",
        "session_id": str(uuid.uuid4()),
    }
    if acc.account_id:
        h["chatgpt-account-id"] = acc.account_id
    return h


def ensure_token(acc: Account) -> bool:
    if acc.token_expired():
        return refresh_account(acc)
    return True


def parse_rate_limits(headers: httpx.Headers, acc: Account) -> None:
    """Read x-codex-* rate-limit headers into the account windows."""
    def f(name: str, default: float = 0.0) -> float:
        try:
            return float(headers.get(name, default))
        except (TypeError, ValueError):
            return default

    now = time.time()
    p_used = f("x-codex-primary-used-percent")
    p_win = f("x-codex-primary-window-minutes")
    p_reset = f("x-codex-primary-reset-after-seconds")
    if headers.get("x-codex-primary-used-percent") is not None:
        acc.primary = RateWindow(
            label=f"{int(p_win/60)}h" if p_win else "primary",
            used_percent=p_used,
            window_minutes=int(p_win),
            resets_at=now + p_reset if p_reset else 0.0,
        )
    s_used = f("x-codex-secondary-used-percent")
    s_win = f("x-codex-secondary-window-minutes")
    s_reset = f("x-codex-secondary-reset-after-seconds")
    if headers.get("x-codex-secondary-used-percent") is not None:
        acc.secondary = RateWindow(
            label="weekly" if s_win >= 10000 else f"{int(s_win/60)}h",
            used_percent=s_used,
            window_minutes=int(s_win),
            resets_at=now + s_reset if s_reset else 0.0,
        )
    # Flag account as limited if either window is exhausted. Only the reset
    # times of *exhausted* windows count toward how long we disable it, so a
    # far-off weekly reset can't inflate a short primary cooldown.
    exhausted_resets = []
    if acc.primary.used_percent >= 100:
        exhausted_resets.append(acc.primary.resets_at or now + 300)
    if acc.secondary.used_percent >= 100:
        exhausted_resets.append(acc.secondary.resets_at or now + 300)
    if exhausted_resets:
        acc.disabled_until = max(exhausted_resets)
    acc.save()


def _to_responses_input(messages: list[dict]) -> list[dict]:
    """Convert OpenAI chat messages to Codex 'responses' input items."""
    items = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict) and c.get("type") in ("text", "input_text"):
                    parts.append({"type": "input_text",
                                  "text": c.get("text", "")})
            content_items = parts or [{"type": "input_text", "text": ""}]
        else:
            kind = "output_text" if role == "assistant" else "input_text"
            content_items = [{"type": kind, "text": str(content)}]
        items.append({"type": "message", "role": role, "content": content_items})
    return items


def probe_models(acc: Account) -> list[str]:
    """Return the subset of candidate models that respond for this account."""
    if not ensure_token(acc):
        return []
    available = []
    with httpx.Client(timeout=30) as client:
        for model in config.CANDIDATE_MODELS:
            payload = {
                "model": model,
                "instructions": "ping",
                "input": _to_responses_input(
                    [{"role": "user", "content": "ping"}]),
                "stream": True,
                "store": False,
                "max_output_tokens": 16,
            }
            try:
                with client.stream(
                    "POST", f"{config.CODEX_BASE_URL}/responses",
                    headers=_headers(acc), json=payload,
                ) as resp:
                    if resp.status_code in (200, 429):
                        # 429 still proves the model is recognized.
                        parse_rate_limits(resp.headers, acc)
                        available.append(model)
                    resp.close()
            except Exception:
                continue
    acc.models = available
    acc.save()
    return available


def chat_completion(acc: Account, body: dict) -> tuple[dict, int]:
    """Non-streaming chat completion. Returns (openai_response, status)."""
    if not ensure_token(acc):
        return {"error": {"message": "token refresh failed"}}, 401
    model = body.get("model", "gpt-5.1")
    messages = body.get("messages", [])
    payload = {
        "model": model,
        "instructions": _system_instructions(messages),
        "input": _to_responses_input(
            [m for m in messages if m.get("role") != "system"]),
        "stream": True,
        "store": False,
    }
    if body.get("max_tokens"):
        payload["max_output_tokens"] = body["max_tokens"]

    text_parts: list[str] = []
    usage = {"input_tokens": 0, "output_tokens": 0}
    status = 200
    with httpx.Client(timeout=180) as client:
        with client.stream("POST", f"{config.CODEX_BASE_URL}/responses",
                           headers=_headers(acc), json=payload) as resp:
            parse_rate_limits(resp.headers, acc)
            status = resp.status_code
            if status != 200:
                detail = resp.read().decode("utf-8", "replace")
                if status == 429:
                    acc.disabled_until = max(acc.disabled_until,
                                             time.time() + 300)
                    acc.save()
                return ({"error": {"message": detail or "upstream error",
                                   "code": status}}, status)
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    evt = json.loads(data)
                except Exception:
                    continue
                etype = evt.get("type", "")
                if etype == "response.output_text.delta":
                    text_parts.append(evt.get("delta", ""))
                elif etype == "response.completed":
                    u = evt.get("response", {}).get("usage", {}) or {}
                    usage["input_tokens"] = u.get("input_tokens", 0)
                    usage["output_tokens"] = u.get("output_tokens", 0)

    text = "".join(text_parts)
    response = {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": usage["input_tokens"],
            "completion_tokens": usage["output_tokens"],
            "total_tokens": usage["input_tokens"] + usage["output_tokens"],
        },
    }
    return response, status


def _system_instructions(messages: list[dict]) -> str:
    sys = [m.get("content", "") for m in messages if m.get("role") == "system"]
    return "\n\n".join(str(s) for s in sys) if sys else "You are a helpful assistant."
