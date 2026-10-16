import time
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi.testclient import TestClient

from app import oauth
from app.main import app
from app.settings import Settings
from app.trello import TrelloClient


def configure(monkeypatch, tmp_path):
    config = Settings(
        "",
        "",
        {"digital": "digital", "sublimacao": "sub"},
        frozenset(),
        tmp_path / "orders.db",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
    )
    monkeypatch.setattr(oauth, "get_settings", lambda: config)
    monkeypatch.setattr(oauth, "ROOT", tmp_path)
    return config


def begin(client):
    response = client.get("/trello/connect", follow_redirects=False)
    assert response.status_code == 303
    parameters = parse_qs(urlparse(response.headers["location"]).query)
    assert set(parameters["scope"][0].split()) == oauth.SCOPES
    assert "offline_access" not in parameters["scope"][0]
    assert parameters["code_challenge_method"] == ["S256"]
    assert len(parameters["code_challenge"][0]) == 43
    assert "HttpOnly" in response.headers["set-cookie"]
    return parameters["state"][0]


def test_only_board_scopes_and_single_use_callback(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    captured = []

    def exchange(code, verifier, settings):
        captured.append((code, verifier))
        return {
            "access_token": "test-access-token",
            "scope": " ".join(oauth.SCOPES),
            "expires_in": 3600,
        }

    monkeypatch.setattr(oauth, "exchange_code", exchange)
    with TestClient(app) as client:
        state = begin(client)
        result = client.get(
            "/trello/callback", params={"code": "test-code", "state": state}, follow_redirects=False
        )
        assert result.status_code == 303
        assert result.headers["location"] == "/?trello=connected"
        assert captured[0][0] == "test-code"
        assert len(captured[0][1]) == 43
        assert "test-access-token" in (tmp_path / ".env").read_text()
        assert (
            client.get("/trello/callback", params={"code": "test-code", "state": state}).status_code
            == 400
        )


def test_invalid_state_never_exchanges_code(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)

    def forbidden(*args):
        raise AssertionError("Invalid state must not reach token exchange")

    monkeypatch.setattr(oauth, "exchange_code", forbidden)
    with TestClient(app) as client:
        begin(client)
        assert client.get("/trello/callback?state=wrong&code=anything").status_code == 400
    assert not (tmp_path / ".env").exists()


def test_extra_permissions_are_never_saved(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        oauth,
        "exchange_code",
        lambda *args: {
            "access_token": "do-not-save",
            "scope": " ".join(oauth.SCOPES) + " write:enterprise:trello",
            "expires_in": 3600,
        },
    )
    with TestClient(app) as client:
        state = begin(client)
        assert (
            client.get("/trello/callback", params={"state": state, "code": "test"}).status_code
            == 502
        )
    assert not (tmp_path / ".env").exists()


def test_oauth_requests_use_bearer_not_query_credentials(monkeypatch, tmp_path):
    config = replace(
        configure(monkeypatch, tmp_path),
        access_token="test-access-token",
        token_expires_at=time.time() + 3600,
    )
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=[])

    trello = TrelloClient(config, transport=httpx.MockTransport(handler))
    trello.request("GET", "boards/digital/lists")
    assert captured[0].headers["Authorization"] == "Bearer test-access-token"
    assert "token" not in captured[0].url.params
    assert "key" not in captured[0].url.params
    trello.close()


def test_expired_oauth_token_requires_reconnection(monkeypatch, tmp_path):
    config = replace(
        configure(monkeypatch, tmp_path), access_token="expired", token_expires_at=time.time() - 1
    )
    assert not config.connected
    assert config.can_authorize
