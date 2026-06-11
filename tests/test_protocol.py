import socket

from fastapi.testclient import TestClient

from swapai import codex_client, config
from swapai.accounts import Account
from swapai.server import ServerThread, create_app


def test_current_model_is_not_rewritten():
    assert codex_client.normalize_model("gpt-5.5") == "gpt-5.5"
    assert codex_client.normalize_model("gpt-5.4-mini") == "gpt-5.4-mini"


def test_responses_payload_is_sanitized():
    payload = codex_client.prepare_responses_payload({
        "model": "gpt-5.4",
        "input": [{"role": "user", "content": "hi"}],
        "stream": False,
        "store": True,
        "max_output_tokens": 100,
    })
    assert payload["model"] == "gpt-5.4"
    assert payload["stream"] is True
    assert payload["store"] is False
    assert "max_output_tokens" not in payload
    assert payload["include"] == ["reasoning.encrypted_content"]


def test_native_response_routes_exist():
    paths = {route.path for route in create_app().routes}
    assert "/responses" in paths
    assert "/codex/responses" in paths
    assert "/v1/responses" in paths
    assert "/v1/codex/responses" in paths


def test_upstream_headers_use_configured_client_version():
    headers = codex_client.upstream_headers(Account(id="test", access_token="x"))
    assert headers["version"] == config.CODEX_CLIENT_VERSION
    assert headers["User-Agent"] == (
        f"{config.CODEX_ORIGINATOR}/{config.CODEX_CLIENT_VERSION}"
    )


def test_health_endpoint():
    response = TestClient(create_app()).get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_server_thread_reports_real_listener(monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setattr(config, "get_host", lambda: "127.0.0.1")
    monkeypatch.setattr(config, "get_port", lambda: port)
    server = ServerThread()
    try:
        server.start()
        assert server.running
    finally:
        server.stop()
    assert not server.running
