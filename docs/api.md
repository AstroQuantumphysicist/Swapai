# API reference

The default base address is `http://localhost:8788`. FastAPI also exposes its
generated OpenAPI UI at `/docs` while the server is running.

## Authentication

If `SWAPAI_API_KEY` is set, every `/v1/*`, `/responses`, and `/codex/*` route
requires this header:

```text
Authorization: Bearer <SWAPAI_API_KEY>
```

Missing, malformed, or incorrect credentials return HTTP `401`. If no key is
configured, those routes are open. `GET /health` is always public.

## Routes

### `GET /health`

Returns server health and the number of accounts currently held by the global
router.

```json
{"status":"ok","accounts":2}
```

A successful health response proves that HTTP is listening; it does not prove
that an upstream account is currently usable.

### `GET /v1/models`

Returns OpenAI-style model objects:

```json
{
  "object": "list",
  "data": [
    {"id": "gpt-5.4", "object": "model", "owned_by": "swapai"}
  ]
}
```

The list is the intersection of probed models across authenticated accounts. If
an account has not completed probing, candidate models are returned temporarily.
An empty list means no common model is currently known.

### `POST /v1/chat/completions`

Accepts a practical subset of the OpenAI Chat Completions schema. The adapter
uses:

- `model`
- `messages`, including system, user, and assistant text
- `max_tokens` (accepted by the adapter, although the current upstream payload
  does not forward a maximum output field)

It converts messages to the Codex Responses wire format, consumes the upstream
SSE stream, and returns one non-streaming Chat Completions object. Setting
`stream: true` does not make this endpoint stream.

```json
{
  "model": "gpt-5.4",
  "messages": [
    {"role": "system", "content": "Answer concisely."},
    {"role": "user", "content": "What is PKCE?"}
  ]
}
```

Tools, multimodal Chat Completions parts other than recognized text parts, and
other OpenAI options are not fully adapted by this endpoint. Prefer the native
Responses endpoint when a client supports it.

### Responses aliases

All these paths invoke the same handler:

- `POST /v1/responses`
- `POST /responses`
- `POST /v1/codex/responses`
- `POST /codex/responses`

The request is normalized for the ChatGPT Codex backend:

- `model` is normalized;
- `stream` is forced to `true`;
- `store` is forced to `false`;
- default `instructions`, `input`, `include`, `tool_choice`, and
  `parallel_tool_calls` values are supplied when absent; and
- `max_output_tokens` is removed because the current backend rejects it.

The successful response is relayed as `text/event-stream` without rewriting
its event bytes. Use a streaming-capable client or `curl -N`.

```bash
curl -N http://127.0.0.1:8788/v1/responses \
  -H "Authorization: Bearer $SWAPAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"Hello"}'
```

## Model normalization

An empty or unrelated model name is replaced by SwapAI's default Codex model.
Candidate model names, names containing `codex`, and names beginning with
`gpt-5.` pass through. To avoid silent substitution, select an ID returned by
`GET /v1/models`.

## Routing and retries

For each request, SwapAI reloads account files and chooses the current usable
account. A `429` or transport failure marks that account unavailable and tries
the next account, up to the number of configured accounts. Other upstream HTTP
errors are returned immediately rather than retried on another account.

## Common errors

| Status | Meaning |
| --- | --- |
| `401` | Invalid/missing SwapAI API key, or an upstream token refresh failure |
| `4xx/5xx` | Upstream rejected the payload; body normally contains its error |
| `503` | No account is available, all accounts are exhausted, or all attempts failed |

FastAPI may return `422` for framework-level request validation. Malformed JSON
can also produce a framework error.

## Usage accounting

A successful Chat Completions call is recorded before the response returns. A
Responses stream is recorded only after a `response.completed` event containing
usage is observed. Interrupted streams or streams without that event are not
added to totals.
