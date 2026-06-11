"""OpenAI-compatible FastAPI server backed by the Codex router."""

from __future__ import annotations

import json
import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config, codex_client, usage
from .router import router


class _ResponsesUsageTracker:
    """Inspect copied SSE bytes without changing the client stream."""

    def __init__(self) -> None:
        self._buffer = b""
        self.input_tokens = 0
        self.output_tokens = 0
        self.completed = False

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._parse_line(line.rstrip(b"\r"))

    def finish(self) -> None:
        if self._buffer:
            self._parse_line(self._buffer.rstrip(b"\r"))
            self._buffer = b""

    def _parse_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except (ValueError, UnicodeDecodeError):
            return
        if event.get("type") != "response.completed":
            return
        response = event.get("response", {}) or {}
        token_usage = response.get("usage", {}) or {}
        self.input_tokens = int(token_usage.get("input_tokens", 0) or 0)
        self.output_tokens = int(token_usage.get("output_tokens", 0) or 0)
        self.completed = True


def create_app() -> FastAPI:
    app = FastAPI(title="SwapAI", version="0.1.0")

    def check_auth(authorization: str | None) -> None:
        key = config.get_api_key()
        if not key:
            return  # no key configured -> open (warned in TUI)
        provided = ""
        if authorization and authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()
        if provided != key:
            raise HTTPException(status_code=401, detail="Invalid API key")

    @app.get("/health")
    def health():
        return {"status": "ok", "accounts": len(router.accounts)}

    @app.get("/v1/models")
    def models(authorization: str | None = Header(default=None)):
        check_auth(authorization)
        router.reload()
        data = [
            {"id": m, "object": "model", "owned_by": "swapai"}
            for m in router.common_models()
        ]
        return {"object": "list", "data": data}

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               authorization: str | None = Header(default=None)):
        check_auth(authorization)
        body = await request.json()
        router.reload()

        attempts = max(1, len(router.accounts))
        last_err = None
        for _ in range(attempts):
            acc = router.active_account()
            if acc is None:
                raise HTTPException(
                    status_code=503,
                    detail="No available accounts (all rate-limited).")
            try:
                resp, status = codex_client.chat_completion(acc, body)
            except Exception as exc:  # noqa: BLE001
                acc.last_error = str(exc)
                acc.save()
                router.mark_limited(acc)
                last_err = str(exc)
                continue
            if status == 429:
                # Limit hit: the tokens accumulated this window are the true
                # capacity. Snap the learned estimate to that exact point.
                usage.learn_capacity(acc, 0.0,
                                     max(acc.primary.used_percent, 100.0),
                                     acc.primary.resets_at)
                router.mark_limited(acc)
                last_err = "rate limited"
                continue
            if status >= 400:
                last_err = resp.get("error", {}).get("message", "error")
                return JSONResponse(status_code=status, content=resp)
            u = resp.get("usage", {})
            # tiktoken-count the real traffic so capacity learning does not
            # depend on the backend's self-reported usage.
            tk_in = usage.count_messages(body.get("messages", []))
            answer = ""
            try:
                answer = resp["choices"][0]["message"]["content"] or ""
            except Exception:
                pass
            tk_out = usage.count_text(answer)
            prompt_tokens = u.get("prompt_tokens") or tk_in
            completion_tokens = u.get("completion_tokens") or tk_out
            usage.record(body.get("model", ""), acc.id,
                         prompt_tokens, completion_tokens)
            # Feed the calibrator: how many tiktoken tokens moved the limit.
            usage.learn_capacity(
                acc, tk_in + tk_out,
                acc.primary.used_percent, acc.primary.resets_at)
            return resp
        raise HTTPException(status_code=503,
                            detail=f"All accounts exhausted: {last_err}")

    async def relay_response(upstream: httpx.Response,
                             client: httpx.AsyncClient, acc, body: dict):
        tracker = _ResponsesUsageTracker()
        try:
            async for chunk in upstream.aiter_bytes():
                tracker.feed(chunk)
                yield chunk
        finally:
            tracker.finish()
            await upstream.aclose()
            await client.aclose()
            if tracker.completed:
                usage.record(
                    body.get("model", ""), acc.id,
                    tracker.input_tokens, tracker.output_tokens)
                usage.learn_limits(
                    acc, tracker.input_tokens + tracker.output_tokens)

    @app.post("/responses")
    @app.post("/codex/responses")
    @app.post("/v1/responses")
    @app.post("/v1/codex/responses")
    async def responses(request: Request,
                        authorization: str | None = Header(default=None)):
        """Stream the native Codex Responses protocol used by Pi and Codex."""
        check_auth(authorization)
        body = codex_client.prepare_responses_payload(await request.json())
        router.reload()

        attempts = max(1, len(router.accounts))
        last_err = "no accounts configured"
        for _ in range(attempts):
            acc = router.active_account()
            if acc is None:
                break
            if not codex_client.ensure_token(acc):
                last_err = acc.last_error or "token refresh failed"
                router.mark_limited(acc)
                continue

            client = httpx.AsyncClient(
                timeout=httpx.Timeout(300, connect=30),
                follow_redirects=True,
            )
            try:
                upstream = await client.send(
                    client.build_request(
                        "POST",
                        f"{config.CODEX_BASE_URL}/responses",
                        headers=codex_client.upstream_headers(acc),
                        json=body,
                    ),
                    stream=True,
                )
                codex_client.parse_rate_limits(upstream.headers, acc)
            except Exception as exc:  # noqa: BLE001
                await client.aclose()
                acc.last_error = str(exc)
                acc.save()
                router.mark_limited(acc)
                last_err = str(exc)
                continue

            if upstream.status_code == 429:
                detail = (await upstream.aread()).decode("utf-8", "replace")
                await upstream.aclose()
                await client.aclose()
                router.mark_limited(acc)
                last_err = detail or "rate limited"
                continue

            if upstream.status_code >= 400:
                raw = (await upstream.aread()).decode("utf-8", "replace")
                await upstream.aclose()
                await client.aclose()
                acc.last_error = raw
                acc.save()
                try:
                    content = json.loads(raw)
                except ValueError:
                    content = {"error": {"message": raw or "upstream error"}}
                return JSONResponse(
                    status_code=upstream.status_code,
                    content=content,
                )

            if acc.last_error:
                acc.last_error = ""
                acc.save()
            return StreamingResponse(
                relay_response(upstream, client, acc, body),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        raise HTTPException(
            status_code=503,
            detail=f"No available Codex account: {last_err}",
        )

    return app


class ServerThread:
    """Runs uvicorn in a background thread so the TUI stays responsive."""

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.host = config.get_host()
        self.port = config.get_port()

    @property
    def running(self) -> bool:
        return bool(
            self._server
            and self._server.started
            and not self._server.should_exit
            and self._thread
            and self._thread.is_alive()
        )

    def start(self, timeout: float = 5.0) -> None:
        if self.running:
            return
        self.host = config.get_host()
        self.port = config.get_port()
        cfg = uvicorn.Config(create_app(), host=self.host, port=self.port,
                             log_level="warning", access_log=False)
        self._server = uvicorn.Server(cfg)
        self._server.install_signal_handlers = lambda: None
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.running:
                return
            if not self._thread.is_alive():
                break
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(
            f"Could not start API server on {self.host}:{self.port}. "
            "The port may already be in use.")

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
