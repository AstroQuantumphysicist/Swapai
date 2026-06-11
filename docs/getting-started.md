# Getting started

## 1. Install

SwapAI requires Python 3.11 or newer. A virtual environment avoids conflicts
with system packages.

```bash
python -m venv .venv
```

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install from the repository:

```bash
python -m pip install -e .
```

The package installs the `swapai` command. `python -m swapai` is equivalent.

## 2. Choose a safe listening address

SwapAI defaults to `0.0.0.0:8788`, making it reachable through every network
interface. For local-only use, create `.env` in the working directory:

```dotenv
SWAPAI_HOST=127.0.0.1
SWAPAI_PORT=8788
```

A project-local `.env` overrides values loaded from `~/.swapai/.env`.

## 3. Connect an account

Run the dashboard:

```bash
swapai
```

Press **A**. SwapAI opens the ChatGPT authorization page and starts a temporary
callback listener on `localhost:1455`. After approval, the browser redirects to
that listener and the resulting credentials are stored beneath
`~/.swapai/accounts/`.

SwapAI then probes candidate models. The probe can take a short time because it
checks each candidate against the account. Add other accounts the same way.

If the browser does not open, copy the authorization URL printed in the
activity panel into a browser on the same machine. The callback must be able to
reach port 1455 locally.

## 4. Configure API authentication

Press **K**, then generate or enter a key. The value is written to
`~/.swapai/.env` as `SWAPAI_API_KEY`. Existing clients should send:

```text
Authorization: Bearer <your key>
```

Without a configured key, protected routes accept unauthenticated traffic. Do
not use that mode on an untrusted network.

## 5. Start the server

Once an account exists, the TUI attempts to start the server automatically.
The server card shows the actual bind address and status. Press **S** to toggle
it manually.

For later headless operation:

```bash
swapai serve
```

Headless mode does not provide an account-login workflow; connect at least one
account through the TUI first.

## 6. Verify it

The health route does not require authentication:

```bash
curl http://127.0.0.1:8788/health
```

Expected shape:

```json
{"status":"ok","accounts":1}
```

List the models shared by all accounts:

```bash
curl http://127.0.0.1:8788/v1/models \
  -H "Authorization: Bearer $SWAPAI_API_KEY"
```

Send a Chat Completions request:

```bash
curl http://127.0.0.1:8788/v1/chat/completions \
  -H "Authorization: Bearer $SWAPAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","messages":[{"role":"user","content":"Hello"}]}'
```

## Next steps

- Review [configuration and storage](configuration.md), particularly if other
  users can access the machine.
- Consult the [API reference](api.md) for native Responses streaming.
- Use [troubleshooting](troubleshooting.md) if login or startup fails.
