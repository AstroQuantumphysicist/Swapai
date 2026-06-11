"""OpenAI-compatible FastAPI server backed by the Codex router."""

from __future__ import annotations

import threading
import time

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import config, codex_client, usage
from .router import router


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

    return app


class ServerThread:
    """Runs uvicorn in a background thread so the TUI stays responsive."""

    def __init__(self) -> None:
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.running = False
        self.host = config.get_host()
        self.port = config.get_port()

    def start(self) -> None:
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
        self.running = True

    def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        self.running = False
