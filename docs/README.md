# SwapAI documentation

This directory contains operational and developer documentation for SwapAI.
Start with the root [README](../README.md) for a short project overview.

## User guides

- [Getting started](getting-started.md) — install SwapAI, connect an account,
  configure authentication, and send a first request.
- [Configuration and storage](configuration.md) — environment variables,
  precedence, persisted files, networking, and credential handling.
- [API reference](api.md) — supported routes, authentication, request behavior,
  response formats, and errors.
- [Troubleshooting](troubleshooting.md) — diagnosis steps for OAuth, startup,
  model discovery, rate limits, and client connectivity.

## Maintainer guides

- [Architecture](architecture.md) — modules, request flow, account selection,
  usage accounting, and capacity learning.
- [Development](development.md) — local setup, tests, repository conventions,
  and a release checklist.

## Scope

The implementation is the source of truth when upstream Codex behavior changes.
Documentation describes version `0.1.0` of this repository and should be updated
with any public route, environment variable, storage format, or workflow change.
