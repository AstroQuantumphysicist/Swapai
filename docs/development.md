# Development guide

## Setup

```bash
git clone <repository-url>
cd SwapAPI
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m pip install pytest
```

Run tests:

```bash
pytest -q
```

The unit suite uses temporary paths and local FastAPI/Uvicorn instances. It does
not require a live ChatGPT account. Manual OAuth, model probing, rate-limit
headers, and terminal rendering still require integration testing.

## Repository layout

```text
.
├── README.md             Project overview and quick start
├── docs/                 User and maintainer documentation
├── pyproject.toml        Package metadata, dependencies, and console script
├── swapai/               Application package
│   ├── accounts.py       OAuth, account model, and persistence
│   ├── codex_client.py   Upstream protocol adapter
│   ├── config.py         Runtime configuration
│   ├── router.py         Selection and failover
│   ├── server.py         FastAPI and Uvicorn integration
│   ├── tui.py            Textual application
│   ├── tui.tcss          TUI stylesheet
│   └── usage.py          Accounting and capacity model
└── tests/                Protocol and usage tests
```

See [architecture.md](architecture.md) for runtime relationships.

## Design conventions

- Keep upstream protocol adaptation in `codex_client.py`; keep HTTP route and
  resource-lifetime concerns in `server.py`.
- Persist account mutations with `Account.save()` whenever they must survive a
  process restart.
- Do not log or include OAuth tokens in exceptions, fixtures, screenshots, or
  documentation.
- Keep network operations outside router locks.
- Preserve original SSE bytes in the Responses relay. Side-channel inspection
  must not alter event framing.
- When adding an environment variable or route, update the root README and the
  corresponding reference document.
- Storage changes should remain tolerant of older account JSON. `Account.load`
  currently filters unknown fields and supplies defaults for newly introduced
  dataclass fields.

## Testing changes

At minimum, run `pytest -q`. Add focused tests for:

- request normalization and required upstream headers;
- route aliases and authentication behavior;
- account rotation after rate limits or transport failure;
- malformed/missing rate-limit headers;
- split SSE chunks and incomplete streams;
- persistence migration and aggregate reconstruction; and
- capacity calculations at boundary values.

When manually testing, avoid using production credentials in a repository-local
home directory. Verify both launch modes:

```bash
swapai
swapai serve
```

Then test health, models, Chat Completions, and a complete Responses stream.

## Versioning and releases

The version currently appears in both `pyproject.toml` and `swapai/__init__.py`;
the FastAPI application also advertises a version in `server.py`. Keep all
three synchronized.

Suggested release checklist:

1. Run the full tests on supported Python versions.
2. Exercise OAuth and both API protocols against a live disposable account.
3. Verify the default Codex client version and candidate models.
4. Review dependency minimums and generated OpenAPI behavior.
5. Update documentation and screenshots for visible changes.
6. Synchronize version strings and summarize compatibility changes.
7. Build/install in a clean virtual environment and smoke-test the console
   entry point.

## Documentation style

Use relative links within the repository, fenced examples that can be copied,
and explicit warnings around credentials/network exposure. Describe observed
behavior rather than promising upstream guarantees. The implementation remains
the source of truth for rapidly changing Codex protocol details.
