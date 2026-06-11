import json
import time

import httpx

from swapai import codex_client, config, usage
from swapai.accounts import Account, RateWindow
from swapai.server import _ResponsesUsageTracker


def test_record_persists_totals_once(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    monkeypatch.setattr(config, "USAGE_FILE", tmp_path / "usage.jsonl")
    monkeypatch.setattr(config, "USAGE_STATE_FILE", tmp_path / "usage.json")

    usage.record("gpt-5.4", "account-1", 10, 4)

    state = json.loads(config.USAGE_STATE_FILE.read_text(encoding="utf-8"))
    assert state["requests"] == 1
    assert state["input_tokens"] == 10
    assert state["output_tokens"] == 4
    assert state["models"]["gpt-5.4"]["requests"] == 1
    assert usage.lifetime_stats().total_tokens == 14


def test_existing_percentages_become_learning_baselines(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    acc = Account(
        id="account-1",
        primary=RateWindow(used_percent=20, window_minutes=300,
                           resets_at=1000),
        secondary=RateWindow(used_percent=3, window_minutes=10080,
                             resets_at=2000),
    )

    usage.observe_limit_baselines(acc)
    assert acc.calib_baseline_percent == 20
    assert acc.weekly_baseline_percent == 3

    acc.primary.used_percent = 25
    acc.secondary.used_percent = 8
    usage.learn_limits(acc, 100)

    assert acc.learned_tokens_per_5h == 2000
    assert acc.learned_tokens_per_week == 2000


def test_weekly_capacity_learns_after_one_percent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    acc = Account(
        id="account-1",
        secondary=RateWindow(used_percent=3, window_minutes=10080,
                             resets_at=2000),
    )
    usage.observe_limit_baselines(acc)
    acc.secondary.used_percent = 4

    usage.learn_limits(acc, 100)

    assert acc.learned_tokens_per_week == 10_000


def test_rate_limit_refresh_clears_old_disabled_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_DIR", tmp_path)
    monkeypatch.setattr(config, "ACCOUNTS_DIR", tmp_path / "accounts")
    acc = Account(
        id="account-1",
        disabled_until=time.time() + 3600,
        primary=RateWindow(used_percent=100, window_minutes=300),
    )
    headers = httpx.Headers({
        "x-codex-primary-used-percent": "20",
        "x-codex-primary-window-minutes": "300",
        "x-codex-secondary-used-percent": "3",
        "x-codex-secondary-window-minutes": "10080",
    })

    codex_client.parse_rate_limits(headers, acc)

    assert acc.disabled_until == 0
    assert acc.calib_baseline_percent == 20
    assert acc.weekly_baseline_percent == 3


def test_stream_usage_tracker_handles_split_sse_data():
    tracker = _ResponsesUsageTracker()
    event = (
        b'data: {"type":"response.completed","response":{"usage":'
        b'{"input_tokens":12,"output_tokens":5}}}\n\n'
    )

    tracker.feed(event[:31])
    tracker.feed(event[31:])
    tracker.finish()

    assert tracker.completed
    assert tracker.input_tokens == 12
    assert tracker.output_tokens == 5
