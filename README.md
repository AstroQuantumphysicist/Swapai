# SwapAI — the best Codex router

A fancy TUI that turns one or more **Codex (ChatGPT) OAuth accounts** into a
single **OpenAI-compatible API server** on your network, with automatic
account failover, per-subscription limit tracking, and token-usage analytics.

```
  ███████ ██     ██  █████  ██████   █████  ██
  ██      ██     ██ ██   ██ ██   ██ ██   ██ ██
  ███████ ██  █  ██ ███████ ██████  ███████ ██
       ██ ██ ███ ██ ██   ██ ██      ██   ██ ██
  ███████  ███ ███  ██   ██ ██      ██   ██ ██
        the best Codex router
```

## Features

- **Multi-account Codex OAuth login** — add as many ChatGPT accounts as you
  want; each is saved separately under `~/.swapai/accounts/`.
- **OpenAI-compatible server** — `/v1/chat/completions` and `/v1/models`,
  exposed on the network (`0.0.0.0` by default) and protected by an API key
  that you set in the TUI and that is saved to `~/.swapai/.env`.
- **Common-model detection** — probes each account and serves only the models
  that are available across **all** logged-in accounts (the intersection).
- **Live limit display** — per account, shows how much of the 5-hour (primary)
  and weekly (secondary) limit remains, with a colored bar and reset timer.
- **Automatic failover** — when the active account hits its limit, the whole
  API transparently switches to the next available account.
- **Usage analytics** — input/output tokens per hour, and a 24/7 capacity
  planner that estimates how many subscriptions you'd need to run nonstop.

## Install

```powershell
pip install -e .
```

## Run

```powershell
swapai            # launch the TUI
swapai serve      # headless: just run the API server
```

### TUI keys

| Key | Action |
|-----|--------|
| `a` | Add a Codex account (opens browser OAuth) |
| `d` | Delete the selected account |
| `r` | Refresh tokens & limits |
| `s` | Start / stop the API server |
| `k` | Set / generate the network API key |
| `q` | Quit |

## Use it

```bash
curl http://<host>:8788/v1/chat/completions \
  -H "Authorization: Bearer $SWAPAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.1","messages":[{"role":"user","content":"Hi"}]}'
```

## Notes

- Limit percentages come straight from ChatGPT's `x-codex-*` rate-limit
  response headers. The **token allowances** used for 24/7 planning are
  heuristics (ChatGPT does not publish exact token caps) and live in
  `swapai/usage.py` (`PLAN_TOKENS_PER_5H`) if you want to tune them.
- This uses the public Codex CLI OAuth client and the ChatGPT backend the same
  way the official Codex CLI does. Respect OpenAI's terms of use.
