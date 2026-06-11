# Configuration and storage

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `SWAPAI_API_KEY` | unset | Bearer token required by protected API routes |
| `SWAPAI_HOST` | `0.0.0.0` | Address passed to Uvicorn |
| `SWAPAI_PORT` | `8788` | TCP port passed to Uvicorn |
| `SWAPAI_CODEX_CLIENT_VERSION` | `0.133.0` | Version advertised to the Codex backend |

An invalid `SWAPAI_PORT` falls back to `8788`. Host and port are read whenever
the server starts; the Codex client version is read when `swapai.config` is
imported, so restart the process after changing it.

## Loading order

At startup, SwapAI:

1. loads `~/.swapai/.env` if it exists; then
2. loads a project-local `.env` with override enabled.

Consequently, project-local values take precedence. Values already present in
the process environment can also be replaced by the local `.env`. Pressing **K**
in the TUI writes the API key to `~/.swapai/.env` and updates the running
process.

Example local-only configuration:

```dotenv
SWAPAI_HOST=127.0.0.1
SWAPAI_PORT=8788
SWAPAI_API_KEY=sk-swapai-use-a-long-random-value
```

## Persistent files

All application state is under `~/.swapai/`:

| Path | Contents |
| --- | --- |
| `.env` | API key written by the TUI |
| `accounts/<id>.json` | OAuth tokens, account metadata, model list, limits, and learned capacity |
| `usage.jsonl` | Append-only per-request history used by recent analytics |
| `usage.json` | Lifetime aggregate totals and model/account breakdowns |

SwapAI also reads `~/.codex/models_cache.json`, when available, to build the
candidate model list. If it cannot read that cache, it uses a built-in fallback
list.

Deleting an account in the TUI removes its account JSON file. It does not erase
historical entries from either usage file or revoke the authorization at the
identity provider.

## Security guidance

Account JSON files contain access, refresh, and ID tokens in plain text. Treat
them as credentials:

- Restrict access to your home directory and `~/.swapai`.
- Do not copy account files into issue reports, backups shared with others, or
  source control.
- Do not expose `0.0.0.0` on an untrusted network without an API key and an
  appropriate firewall.
- Prefer `127.0.0.1` when all clients run on the same machine.
- Put a TLS-terminating reverse proxy in front of SwapAI before carrying API
  keys or prompts over an untrusted network. SwapAI itself serves plain HTTP.
- Rotate a compromised API key with **K** and restart or reconfigure clients.

Authentication uses direct string comparison and is intended as simple bearer
protection, not as a multi-tenant authorization system. The public `/health`
route reports the number of loaded accounts but exposes no tokens or addresses.

## Upstream client version

The backend expects the `originator`, `version`, and `User-Agent` fields to
match Codex CLI conventions. Override `SWAPAI_CODEX_CLIENT_VERSION` only when an
upstream protocol change requires it. An arbitrary or stale value can cause
upstream requests to fail.
