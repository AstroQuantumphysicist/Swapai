# Architecture

## Component map

| Module | Responsibility |
| --- | --- |
| `swapai.__main__` | CLI dispatch to TUI or headless server |
| `swapai.config` | Paths, environment loading, defaults, and API-key helpers |
| `swapai.accounts` | Account data model, JSON persistence, OAuth PKCE, token refresh |
| `swapai.oauth` | Compatibility exports for OAuth callers |
| `swapai.codex_client` | Upstream headers/payloads, model probes, limit parsing, chat adapter |
| `swapai.router` | Account selection, model intersection, and failover cursor |
| `swapai.server` | FastAPI routes, Responses relay, and background Uvicorn thread |
| `swapai.usage` | Usage persistence, token counting, statistics, and capacity learning |
| `swapai.tui` | Textual dashboard and interactive operations |
| `swapai/tui.tcss` | Dashboard layout and visual styling |

## Startup paths

`swapai` loads configuration and starts `SwapAIApp`. The app reloads persisted
accounts, starts Uvicorn in a daemon thread when accounts exist, and runs model
and rate-limit probes in Textual workers.

`swapai serve` loads configuration, starts the same `ServerThread`, then keeps
the main process alive until interrupted. It does not instantiate Textual or
perform interactive OAuth.

## Request flow

```text
client
  │ HTTP + optional bearer key
  ▼
FastAPI server ──► global Router ──► Account
  │                                  │
  │ normalized request               │ OAuth access token
  ▼                                  ▼
codex_client / async relay ──► ChatGPT Codex backend
  │
  ├── parses x-codex-* limit headers
  ├── persists account state
  └── records completed usage
```

The router reloads account JSON before each public model or completion request.
Its active index is process-local, while account limits and disabled times are
persisted. It rotates from the active index and skips accounts without access
tokens or accounts whose `disabled_until` is in the future.

## Failover

A rate-limited account is disabled until the parsed exhausted-window reset. If
no useful reset is available, the router applies a short fallback cooldown.
Transport exceptions also move the cursor to the next account. Retries are
bounded by the number of configured accounts, preventing an infinite loop.

`Router` uses a lock for reload, selection, and cursor updates. Network work is
performed outside that lock.

## Model discovery

Candidate models come from the local Codex model cache or a fallback list.
`probe_models` sends a minimal Responses request for each candidate. HTTP `200`
and `429` both establish that the model is recognized. The API advertises only
the intersection across accounts so that a request can survive account
failover without changing model availability.

## Rate limits and capacity learning

`parse_rate_limits` reads primary and secondary `x-codex-*` headers and stores
`RateWindow` objects. Missing window lengths receive five-hour and seven-day
defaults when a used percentage is present.

Capacity is distinct from rate-limit percentage:

1. The first observed backend percentage becomes a baseline.
2. SwapAI counts local traffic with `tiktoken` (`o200k_base`) when available.
3. Percentage movement after the baseline is attributed to locally counted
   tokens.
4. A capacity estimate is accepted after a minimum signal (5% primary, 1%
   weekly), preferring estimates with greater observed movement.
5. The planner uses the stricter hourly capacity when both learned primary and
   weekly estimates exist.

Plan constants are only initial heuristics. They are not account entitlements.

## Usage persistence

Each completed, countable request is appended to `usage.jsonl`. In the same
process lock, aggregate state is updated and atomically replaced through a
`.tmp` file as `usage.json`. Recent charts read the event log; lifetime totals
read the aggregate state. If aggregate state is missing or invalid, it can be
rebuilt from event rows.

The lock protects threads in one process. Running multiple SwapAI processes
against the same home directory is not a supported safe-write configuration.

## Streaming resource ownership

The Responses route creates one `httpx.AsyncClient` per upstream attempt. On a
successful request, ownership passes to the relay generator, which closes both
the response and client in `finally`. It inspects copied SSE bytes for the
completion event but yields original chunks unchanged.
