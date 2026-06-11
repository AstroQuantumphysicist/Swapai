"""Low-level client for the ChatGPT Codex backend + rate-limit parsing."""

from __future__ import annotations

import json
import time
import uuid

import httpx

from . import config
from .accounts import Account, RateWindow, refresh_account


def _headers(acc: Account) -> dict:
    # Mirrors the official Codex CLI request shape. The backend rejects
    # requests that don't carry the `{originator}/{version}` User-Agent
    # plus the matching `version` header, so both must be set.
    version = config.CODEX_CLIENT_VERSION
    originator = config.CODEX_ORIGINATOR
    h = {
        "Authorization": f"Bearer {acc.access_token}",
        "Content-Type": "application/json",
        "originator": originator,
        "version": version,
        "User-Agent": f"{originator}/{version}",
        "session_id": str(uuid.uuid4()),
    }
    if acc.account_id:
        h["chatgpt-account-id"] = acc.account_id
    return h


def normalize_model(requested: str) -> str:
    """Map any requested model name to a Codex-compatible one.

    The backend silently coerces unknown models to a default; this helper
    makes the coercion explicit and keeps the dashboard honest. Mirrors
    the `normalize_codex_compat_model` logic from the Rust reference.
    """
    r = (requested or "").strip()
    if not r:
        return config.DEFAULT_CODEX_MODEL
    lower = r.lower()
    if ("codex" in lower
            or lower == config.DEFAULT_CODEX_MODEL
            or lower.startswith("gpt-5.4")
            or lower.startswith("gpt-5.3")
            or lower.startswith("gpt-5.2")):
        return r
    return config.DEFAULT_CODEX_MODEL


def _base_payload(model: str, instructions: str, input_items: list[dict],
                  max_output_tokens: int | None = None) -> dict:
    """Build the canonical Codex 'responses' payload.

    These extra fields (`include`, `tools`, `tool_choice`,
    `parallel_tool_calls`) are required by the backend; without them the
    request is rejected with a 4xx.
    """
    payload: dict = {
        "model": normalize_model(model),
        "instructions": instructions or "You are a helpful assistant.",
        "input": input_items,
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": True,
    }
    if max_output_tokens is not None:
        payload["max_output_tokens"] = int(max_output_tokens)
    return payload


def ensure_token(acc: Account) -> bool:
    if acc.token_expired():
        return refresh_account(acc)
    return True


def parse_rate_limits(headers: httpx.Headers, acc: Account) -> None:
    """Read x-codex-* rate-limit headers into the account windows.

    Defensive: if the backend sends a used_percent but omits window_minutes,
    fall back to a sensible default (5h primary, 7d secondary) so the TUI
    meter is still rendered instead of being hidden as "—".
    """
    def f(name: str, default: float = 0.0) -> float:
        try:
            return float(headers.get(name, default))
        except (TypeError, ValueError):
            return default

    now = time.time()
    p_used = f("x-codex-primary-used-percent")
    p_win = f("x-codex-primary-window-minutes", 300)  # default 5h
    p_reset = f("x-codex-primary-reset-after-seconds")
    if headers.get("x-codex-primary-used-percent") is not None or p_used > 0:
        win = int(p_win) if p_win > 0 else 300
        acc.primary = RateWindow(
            label=f"{int(win/60)}h" if win else "primary",
            used_percent=p_used,
            window_minutes=win,
            resets_at=now + p_reset if p_reset else acc.primary.resets_at,
        )
    s_used = f("x-codex-secondary-used-percent")
    s_win = f("x-codex-secondary-window-minutes", 10080)  # default 7d weekly
    s_reset = f("x-codex-secondary-reset-after-seconds")
    if headers.get("x-codex-secondary-used-percent") is not None or s_used > 0:
        win = int(s_win) if s_win > 0 else 10080
        acc.secondary = RateWindow(
            label="weekly" if win >= 10000 else f"{int(win/60)}h",
            used_percent=s_used,
            window_minutes=win,
            resets_at=now + s_reset if s_reset else acc.secondary.resets_at,
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
    available: list[str] = []
    last_err: str = ""
    with httpx.Client(timeout=30) as client:
        for model in config.CANDIDATE_MODELS:
            payload = _base_payload(
                model, "ping",
                _to_responses_input([{"role": "user", "content": "ping"}]),
                max_output_tokens=16,
            )
            try:
                with client.stream(
                    "POST", f"{config.CODEX_BASE_URL}/responses",
                    headers=_headers(acc), json=payload,
                ) as resp:
                    # Always parse rate-limit headers regardless of status.
                    parse_rate_limits(resp.headers, acc)
                    status = resp.status_code
                    if status in (200, 429):
                        # 429 still proves the model is recognized.
                        available.append(model)
                    else:
                        last_err = f"{model} -> HTTP {status}"
                    # Drain the stream so the connection can be reused
                    # and the server sees the request as fully consumed.
                    try:
                        for _ in resp.iter_lines():
                            pass
                    except Exception:
                        pass
            except Exception as exc:  # noqa: BLE001
                last_err = f"{model} -> {exc}"
                continue
    acc.models = available
    acc.save()
    if not available and last_err:
        acc.last_error = last_err
        acc.save()
    return available


def probe_limits(acc: Account) -> bool:
    """Refresh rate-limit headers without probing every model.

    Cheaper than `probe_models`: a single tiny request that we close as soon
    as headers arrive. Returns True if any rate-limit info was observed.
    """
    if not ensure_token(acc):
        return False
    candidates = acc.models or config.CANDIDATE_MODELS or [config.DEFAULT_CODEX_MODEL]
    model = candidates[0]
    try:
        with httpx.Client(timeout=15) as client:
            with client.stream(
                "POST", f"{config.CODEX_BASE_URL}/responses",
                headers=_headers(acc),
                json=_base_payload(
                    model, "ping",
                    _to_responses_input(
                        [{"role": "user", "content": "."}]),
                    max_output_tokens=1,
                ),
            ) as resp:
                parse_rate_limits(resp.headers, acc)
                try:
                    for _ in resp.iter_lines():
                        pass
                except Exception:
                    pass
    except Exception as exc:  # noqa: BLE001
        acc.last_error = f"probe_limits: {exc}"
        acc.save()
        return False
    return (acc.primary.used_percent > 0
            or acc.primary.window_minutes > 0
            or acc.secondary.used_percent > 0
            or acc.secondary.window_minutes > 0)


def chat_completion(acc: Account, body: dict) -> tuple[dict, int]:
    """Non-streaming chat completion. Returns (openai_response, status)."""
    if not ensure_token(acc):
        return {"error": {"message": "token refresh failed"}}, 401
    model = body.get("model", config.DEFAULT_CODEX_MODEL)
    messages = body.get("messages", [])
    payload = _base_payload(
        model,
        _system_instructions(messages),
        _to_responses_input(
            [m for m in messages if m.get("role") != "system"]),
        max_output_tokens=body.get("max_tokens"),
    )

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
