"""Token usage tracking, tiktoken counting, capacity learning, sub math."""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config

_lock = threading.Lock()


def data_dir() -> Path:
    """Return the directory where SwapAI persists accounts and usage."""
    return config.APP_DIR

# ---------------------------------------------------------------------------
# tiktoken counting (o200k_base = GPT-4o/GPT-5 family encoding)
# ---------------------------------------------------------------------------
_encoder = None
_encoder_failed = False


def _get_encoder():
    global _encoder, _encoder_failed
    if _encoder is None and not _encoder_failed:
        try:
            import tiktoken
            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception:
            _encoder_failed = True
    return _encoder


def count_text(text: str) -> int:
    if not text:
        return 0
    enc = _get_encoder()
    if enc is not None:
        try:
            return len(enc.encode(text, disallowed_special=()))
        except Exception:
            pass
    return max(1, len(text) // 4)  # fallback heuristic


def count_messages(messages: list[dict]) -> int:
    """tiktoken count of an OpenAI chat message list (+~4 tok/msg framing)."""
    total = 0
    for m in messages or []:
        total += 4
        content = m.get("content", "")
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict):
                    total += count_text(str(c.get("text", "")))
        else:
            total += count_text(str(content))
    return total


# ---------------------------------------------------------------------------
# Plan allowances (per 5h primary window). Heuristic seeds; the live learner
# overrides these per account once it observes a real used-percent movement.
# ---------------------------------------------------------------------------
PLAN_TOKENS_PER_5H = {
    "free": 40_000,
    "plus": 1_500_000,
    "pro": 12_000_000,
    "business": 3_000_000,
    "team": 3_000_000,
    "enterprise": 20_000_000,
}
DEFAULT_PLAN_TOKENS_PER_5H = 1_500_000
PRIMARY_WINDOW_HOURS = 5
WEEKLY_WINDOW_HOURS = 7 * 24


def record(model: str, account_id: str, prompt_tokens: int,
           completion_tokens: int) -> None:
    now = time.time()
    prompt_tokens = int(prompt_tokens or 0)
    completion_tokens = int(completion_tokens or 0)
    line = {
        "t": now,
        "model": model,
        "account": account_id,
        "in": prompt_tokens,
        "out": completion_tokens,
    }
    with _lock:
        config.ensure_dirs()
        state = _load_state_unlocked()
        with config.USAGE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        state["requests"] = int(state.get("requests", 0)) + 1
        state["input_tokens"] = int(
            state.get("input_tokens", 0)) + prompt_tokens
        state["output_tokens"] = int(
            state.get("output_tokens", 0)) + completion_tokens
        if not state.get("first_request_at"):
            state["first_request_at"] = now
        state["last_request_at"] = now
        _increment_breakdown(
            state.setdefault("models", {}), model or "?",
            prompt_tokens, completion_tokens)
        _increment_breakdown(
            state.setdefault("accounts", {}), account_id or "?",
            prompt_tokens, completion_tokens)
        _save_state_unlocked(state)


def _increment_breakdown(target: dict, key: str, input_tokens: int,
                         output_tokens: int) -> None:
    row = target.setdefault(
        key, {"requests": 0, "input_tokens": 0, "output_tokens": 0})
    row["requests"] = int(row.get("requests", 0)) + 1
    row["input_tokens"] = int(row.get("input_tokens", 0)) + input_tokens
    row["output_tokens"] = int(row.get("output_tokens", 0)) + output_tokens


def _state_from_rows() -> dict:
    rows = _read()
    state = {
        "version": 1,
        "requests": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "first_request_at": 0.0,
        "last_request_at": 0.0,
        "models": {},
        "accounts": {},
    }
    for row in rows:
        when = float(row.get("t", 0) or 0)
        tk_in = int(row.get("in", 0) or 0)
        tk_out = int(row.get("out", 0) or 0)
        state["requests"] += 1
        state["input_tokens"] += tk_in
        state["output_tokens"] += tk_out
        if when and not state["first_request_at"]:
            state["first_request_at"] = when
        if when:
            state["last_request_at"] = max(state["last_request_at"], when)
        _increment_breakdown(
            state["models"], row.get("model") or "?", tk_in, tk_out)
        _increment_breakdown(
            state["accounts"], row.get("account") or "?", tk_in, tk_out)
    return state


def _load_state_unlocked() -> dict:
    if config.USAGE_STATE_FILE.exists():
        try:
            return json.loads(config.USAGE_STATE_FILE.read_text(
                encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
    return _state_from_rows()


def _save_state_unlocked(state: dict) -> None:
    tmp = config.USAGE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(config.USAGE_STATE_FILE)


def usage_state() -> dict:
    """Return persistent lifetime totals and per-model/account breakdowns."""
    with _lock:
        state = _load_state_unlocked()
        if not config.USAGE_STATE_FILE.exists():
            config.ensure_dirs()
            _save_state_unlocked(state)
        return state


def _read(since: float | None = None) -> list[dict]:
    if not config.USAGE_FILE.exists():
        return []
    rows = []
    with config.USAGE_FILE.open(encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if since is not None and row.get("t", 0) < since:
                continue
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Capacity learning
# ---------------------------------------------------------------------------
# Primary usage moves quickly enough to wait for a stronger signal. Weekly
# usage moves much more slowly, so one full percentage point is actionable.
PRIMARY_MIN_LEARN_PERCENT = 5.0
WEEKLY_MIN_LEARN_PERCENT = 1.0


def observe_limit_baselines(acc) -> None:
    """Anchor existing backend usage before counting local traffic."""
    if acc.primary.window_minutes > 0 or acc.primary.used_percent > 0:
        _observe_window_baseline(
            acc, "calib", acc.primary.used_percent, acc.primary.resets_at)
    if acc.secondary.window_minutes > 0 or acc.secondary.used_percent > 0:
        _observe_window_baseline(
            acc, "weekly", acc.secondary.used_percent,
            acc.secondary.resets_at)


def _observe_window_baseline(acc, prefix: str, used_percent: float,
                             resets_at: float) -> None:
    baseline_name = f"{prefix}_baseline_percent"
    anchor_name = f"{prefix}_anchor_reset"
    tokens_name = "window_tokens" if prefix == "calib" else (
        "weekly_window_tokens")
    last_name = "calib_last_percent" if prefix == "calib" else (
        "weekly_last_percent")
    baseline = getattr(acc, baseline_name)
    anchor = getattr(acc, anchor_name)
    last = getattr(acc, last_name)
    rolled = (
        baseline < 0
        or used_percent < last - 1.0
        or (anchor and resets_at and abs(resets_at - anchor) > 600)
    )
    if rolled:
        setattr(acc, baseline_name, used_percent)
        setattr(acc, anchor_name, resets_at)
        setattr(acc, tokens_name, 0.0)
        confidence = (
            "calib_confidence_percent"
            if prefix == "calib" else "weekly_confidence_percent")
        setattr(acc, confidence, 0.0)
    setattr(acc, last_name, used_percent)


def learn_limits(acc, tokens: float) -> None:
    """Learn both 5-hour and weekly token capacities from local traffic."""
    _learn_window(
        acc, "calib", tokens, acc.primary.used_percent,
        acc.primary.resets_at, "learned_tokens_per_5h")
    _learn_window(
        acc, "weekly", tokens, acc.secondary.used_percent,
        acc.secondary.resets_at, "learned_tokens_per_week")
    acc.save()


def _learn_window(acc, prefix: str, tokens: float, used_percent: float,
                  resets_at: float, learned_name: str) -> None:
    """Estimate capacity from usage growth after the persisted baseline.

    If SwapAI starts when the backend is already at 20%, only movement beyond
    that 20% is attributed to locally observed tokens.
    """
    _observe_window_baseline(acc, prefix, used_percent, resets_at)
    tokens_name = "window_tokens" if prefix == "calib" else (
        "weekly_window_tokens")
    last_name = "calib_last_percent" if prefix == "calib" else (
        "weekly_last_percent")
    confidence_name = (
        "calib_confidence_percent"
        if prefix == "calib" else "weekly_confidence_percent")
    baseline_name = f"{prefix}_baseline_percent"

    tracked = getattr(acc, tokens_name) + max(0.0, tokens)
    setattr(acc, tokens_name, tracked)
    setattr(acc, last_name, used_percent)
    delta = max(0.0, used_percent - getattr(acc, baseline_name))
    threshold = (
        PRIMARY_MIN_LEARN_PERCENT
        if prefix == "calib" else WEEKLY_MIN_LEARN_PERCENT)
    if delta >= threshold and tracked > 0:
        estimate = tracked / (delta / 100.0)
        if delta >= getattr(acc, confidence_name):
            setattr(acc, learned_name, estimate)
            setattr(acc, confidence_name, delta)


def learn_capacity(acc, tokens: float, used_percent: float,
                   resets_at: float) -> None:
    """Backward-compatible primary-window learner."""
    _learn_window(
        acc, "calib", tokens, used_percent, resets_at,
        "learned_tokens_per_5h")
    acc.save()


def effective_tokens_per_5h(plan: str, learned: float = 0.0) -> float:
    if learned and learned > 0:
        return learned
    return float(PLAN_TOKENS_PER_5H.get((plan or "").lower(),
                                        DEFAULT_PLAN_TOKENS_PER_5H))


def effective_tokens_per_hour(plan: str, learned: float = 0.0) -> float:
    return effective_tokens_per_5h(plan, learned) / PRIMARY_WINDOW_HOURS


def plan_tokens_per_5h(plan: str) -> int:
    return PLAN_TOKENS_PER_5H.get((plan or "").lower(),
                                  DEFAULT_PLAN_TOKENS_PER_5H)


def plan_tokens_per_hour(plan: str) -> float:
    return plan_tokens_per_5h(plan) / PRIMARY_WINDOW_HOURS


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------
@dataclass
class UsageStats:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    span_hours: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def input_per_hour(self) -> float:
        return self.input_tokens / self.span_hours if self.span_hours else 0.0

    @property
    def output_per_hour(self) -> float:
        return self.output_tokens / self.span_hours if self.span_hours else 0.0

    @property
    def tokens_per_hour(self) -> float:
        return self.total_tokens / self.span_hours if self.span_hours else 0.0


def stats_last_hours(hours: float = 1.0) -> UsageStats:
    since = time.time() - hours * 3600
    rows = _read(since)
    if not rows:
        return UsageStats(span_hours=hours)
    first = min(r["t"] for r in rows)
    span = max((time.time() - first) / 3600, 1 / 60)
    return UsageStats(
        requests=len(rows),
        input_tokens=sum(r.get("in", 0) for r in rows),
        output_tokens=sum(r.get("out", 0) for r in rows),
        span_hours=span,
    )


def lifetime_stats() -> UsageStats:
    state = usage_state()
    if not state.get("requests"):
        return UsageStats()
    first = float(state.get("first_request_at", 0) or time.time())
    span = max((time.time() - first) / 3600, 1 / 60)
    return UsageStats(
        requests=int(state.get("requests", 0)),
        input_tokens=int(state.get("input_tokens", 0)),
        output_tokens=int(state.get("output_tokens", 0)),
        span_hours=span,
    )


def per_model_breakdown(hours: float | None = None) -> dict[str, dict]:
    since = time.time() - hours * 3600 if hours else None
    rows = _read(since)
    out: dict[str, dict] = {}
    for r in rows:
        m = r.get("model") or "?"
        d = out.setdefault(m, {"requests": 0, "in": 0, "out": 0})
        d["requests"] += 1
        d["in"] += r.get("in", 0)
        d["out"] += r.get("out", 0)
    return out


def throughput_series(minutes: int = 60, buckets: int = 30) -> list[int]:
    """Total tokens per time-bucket over the last `minutes` (for sparkline)."""
    now = time.time()
    since = now - minutes * 60
    rows = _read(since)
    width = (minutes * 60) / buckets
    series = [0] * buckets
    for r in rows:
        idx = int((r.get("t", now) - since) / width)
        if 0 <= idx < buckets:
            series[idx] += r.get("in", 0) + r.get("out", 0)
    return series


# ---------------------------------------------------------------------------
# 24/7 capacity planning
# ---------------------------------------------------------------------------
@dataclass
class SubscriptionPlan:
    tokens_per_hour_needed: float
    capacity_per_sub_per_hour: float
    plan_used: str
    subs_needed: int
    current_subs: int
    sustainable: bool
    total_capacity_per_hour: float = 0.0
    learned: bool = False


def subscriptions_needed(accounts_plans, tokens_per_hour: float,
                         learned_caps: list[float] | None = None,
                         weekly_caps: list[float] | None = None
                         ) -> SubscriptionPlan:
    """Compute subs needed for 24/7.

    `accounts_plans`  : list of plan strings (one per account).
    `learned_caps`    : optional matching list of learned tokens/5h per acct;
                        0/None falls back to the plan heuristic.
    """
    plans = list(accounts_plans)
    caps5h = []
    any_learned = False
    for i, p in enumerate(plans or ["plus"]):
        lc = 0.0
        if learned_caps and i < len(learned_caps):
            lc = learned_caps[i] or 0.0
        if lc > 0:
            any_learned = True
        primary_hour = effective_tokens_per_5h(p, lc) / PRIMARY_WINDOW_HOURS
        weekly = 0.0
        if weekly_caps and i < len(weekly_caps):
            weekly = weekly_caps[i] or 0.0
        weekly_hour = weekly / WEEKLY_WINDOW_HOURS if weekly > 0 else 0.0
        effective_hour = (
            min(primary_hour, weekly_hour) if weekly_hour > 0
            else primary_hour)
        caps5h.append(effective_hour * PRIMARY_WINDOW_HOURS)
        if weekly > 0:
            any_learned = True

    if not plans:
        caps5h = [effective_tokens_per_5h("plus")]

    # Reference capacity = average per-sub hourly capacity across accounts.
    per_sub_hour = (sum(caps5h) / len(caps5h)) / PRIMARY_WINDOW_HOURS
    total_hour = sum(caps5h) / PRIMARY_WINDOW_HOURS

    ref_plan = max(set(plans), key=plans.count) if plans else "plus"
    needed = 0
    if tokens_per_hour > 0 and per_sub_hour > 0:
        needed = math.ceil(tokens_per_hour / per_sub_hour)
    return SubscriptionPlan(
        tokens_per_hour_needed=tokens_per_hour,
        capacity_per_sub_per_hour=per_sub_hour,
        plan_used=ref_plan,
        subs_needed=needed,
        current_subs=len(plans),
        sustainable=len(plans) >= needed and total_hour >= tokens_per_hour,
        total_capacity_per_hour=total_hour,
        learned=any_learned,
    )
