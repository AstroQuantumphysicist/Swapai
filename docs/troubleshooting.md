# Troubleshooting

## Start with these checks

```bash
python --version
python -m pip show swapai
swapai serve
curl http://127.0.0.1:8788/health
```

Also inspect the TUI activity panel and the selected account's error line.
Account state is persisted in `~/.swapai/accounts/*.json`; remove secrets before
sharing any diagnostic excerpt.

## `swapai` is not found

Activate the virtual environment in which the project was installed, then run:

```bash
python -m pip install -e .
python -m swapai
```

Using `python -m swapai` helps verify that the expected Python interpreter owns
the package.

## Server does not start

A message such as `Could not start API server ... The port may already be in
use` usually means another process owns the address. Choose another port:

```dotenv
SWAPAI_PORT=8789
```

If the port is free, verify that `SWAPAI_HOST` names an address available on the
machine. Use `127.0.0.1` for local-only operation. Restart SwapAI after editing
the environment.

## OAuth login times out

The temporary callback server must bind `localhost:1455`, and authorization
must finish within five minutes.

- Ensure another application is not using port 1455.
- Complete login in a browser on the same machine as SwapAI.
- If no browser opens, copy the URL from the activity panel manually.
- Check local firewall or endpoint-security rules affecting loopback listeners.
- Retry with **A** after the previous flow has closed.

## Account exists but no models appear

Press **R** to refresh and probe. Model probing makes an upstream request per
candidate and may be delayed or rate-limited. Check the selected account error
in the dashboard.

SwapAI reads candidates from `~/.codex/models_cache.json` when possible. A stale
cache can supply stale candidates; update the Codex CLI cache or temporarily
move that file so the fallback list is used, then restart SwapAI.

For multiple accounts, `/v1/models` returns only their intersection. One
account lacking a model removes it from the final list.

## API returns `401`

For a SwapAI authentication error, make sure the exact configured key is sent:

```text
Authorization: Bearer sk-swapai-...
```

The value may come from project `.env`, which overrides `~/.swapai/.env`. If the
error concerns token refresh, reconnect the affected account through the TUI.

## API returns `503`

The detail normally distinguishes these causes:

- no accounts are configured;
- every account is inside a persisted cooldown/rate-limit window;
- token refresh failed for all attempted accounts; or
- transport failures exhausted all attempts.

Open the TUI to inspect per-account state and reset countdowns. Press **R** to
refresh backend headers. Do not manually clear `disabled_until` while an account
is genuinely limited; the next upstream response can immediately restore it.

## Client receives SSE when JSON was expected

All Responses aliases always stream `text/event-stream`, even if the request
sets `stream: false`. Configure the client for OpenAI Responses streaming, use
`curl -N`, or call `/v1/chat/completions` for a buffered JSON response.

## Model request runs a different/default model

SwapAI normalizes empty or unrelated model names to its default. Query
`GET /v1/models` and use one of the returned IDs. Names beginning with `gpt-5.`
or containing `codex` pass through, but that alone does not guarantee that the
upstream account can serve them.

## Dashboard totals differ from expectations

Responses usage is counted only when the stream reaches a valid
`response.completed` event. Cancelled streams are therefore absent. Recent
charts use `usage.jsonl`, while lifetime totals use `usage.json`; deleting one
file can make views differ until new data arrives or state is reconstructed.

Token-based capacity is an estimate. It requires enough observed percentage
movement and may initially show `learning` or a plan estimate.

## Reset local state

Stop all SwapAI processes before changing files. Back up the directory if the
history matters:

```bash
mv ~/.swapai ~/.swapai.backup
```

On Windows, rename `%USERPROFILE%\.swapai` in PowerShell or Explorer. Restarting
creates a fresh directory. This removes local account access and analytics; it
does not necessarily revoke upstream OAuth authorization.
