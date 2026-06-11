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


def record(model: str, account_id: str, prompt_tokens: int,
           completion_tokens: int) -> None:
    line = {
        "t": time.time(),
        "model": model,
        "account": account_id,
        "in": int(prompt_tokens or 0),
        "out": int(completion_tokens or 0),
    }
    with _lock:
        config.ensure_dirs()
        with config.USAGE_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")


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
# Below this used-percent the estimate is too quantization-noisy to trust.
MIN_LEARN_PERCENT = 5.0


def learn_capacity(acc, tokens: float, used_percent: float,
                   resets_at: float) -> None:
    """Update an account's learned 5h token capacity from a real request.

    We accumulate tiktoken-counted tokens consumed within the current primary
    window and divide by the fraction of the limit reported used:
        capacity ~= tokens_in_window / (used_percent / 100)
    The estimate taken at the *highest* used-percent in a window is the most
    accurate; hitting 100% yields the true capacity exactly.
    """
    prev = acc.calib_last_percent
    window_rolled = (
        used_percent < prev - 1.0
        or (acc.calib_anchor_reset and resets_at
            and abs(resets_at - acc.calib_anchor_reset) > 600)
    )
    if window_rolled:
        acc.window_tokens = 0.0
        acc.calib_confidence_percent = 0.0
        acc.calib_anchor_reset = resets_at
    if not acc.calib_anchor_reset and resets_at:
        acc.calib_anchor_reset = resets_at

    acc.window_tokens += max(0.0, tokens)
    acc.calib_last_percent = used_percent

    if used_percent >= MIN_LEARN_PERCENT and acc.window_tokens > 0:
        est = acc.window_tokens / (used_percent / 100.0)
        # Keep the estimate observed at the highest used-percent this window.
        if used_percent >= acc.calib_confidence_percent:
            acc.learned_tokens_per_5h = est
            acc.calib_confidence_percent = used_percent
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
    rows = _read()
    if not rows:
        return UsageStats()
    first = min(r["t"] for r in rows)
    span = max((time.time() - first) / 3600, 1 / 60)
    return UsageStats(
        requests=len(rows),
        input_tokens=sum(r.get("in", 0) for r in rows),
        output_tokens=sum(r.get("out", 0) for r in rows),
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
                         learned_caps: list[float] | None = None
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
        caps5h.append(effective_tokens_per_5h(p, lc))

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
