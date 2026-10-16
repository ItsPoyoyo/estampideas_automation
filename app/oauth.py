"""Local Trello connection: board scopes only, PKCE, state, and no offline access."""

import base64
import hashlib
import secrets
import threading
import time
from urllib.parse import urlencode

import httpx
from dotenv import set_key
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.settings import ROOT, Settings, get_settings

router = APIRouter(prefix="/trello", tags=["Trello connection"])
SCOPES = {"read:board:trello", "write:board:trello"}
CALLBACK = "http://127.0.0.1:8001/trello/callback"
COOKIE = "estampi_oauth_state"
_pending: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


@router.get("/connect", include_in_schema=False)
def connect():
    settings = get_settings()
    if not settings.can_authorize:
        raise HTTPException(503, "Configure o Client ID e o Client Secret do Trello no .env.")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    with _lock:
        now = time.time()
        for old_state in list(_pending):
            if _pending[old_state][1] < now:
                del _pending[old_state]
        _pending[state] = (verifier, now + 600)
    parameters = {
        "client_id": settings.oauth_client_id,
        "scope": " ".join(sorted(SCOPES)),
        "redirect_uri": CALLBACK,
        "response_type": "code",
        "prompt": "consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        "https://auth.atlassian.com/authorize?" + urlencode(parameters), status_code=303
    )
    response.set_cookie(COOKIE, state, max_age=600, httponly=True, samesite="lax", path="/trello")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def exchange_code(code: str, verifier: str, settings: Settings) -> dict:
    try:
        with httpx.Client(timeout=20) as client:
            response = client.post(
                "https://auth.atlassian.com/oauth/token",
                json={
                    "client_id": settings.oauth_client_id,
                    "client_secret": settings.oauth_client_secret,
                    "grant_type": "authorization_code",
                    "redirect_uri": CALLBACK,
                    "code": code,
                    "code_verifier": verifier,
                },
            )
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            502,
            "Não foi possível concluir a autorização com o Trello. Clique em Conectar Trello novamente.",
        ) from None


@router.get("/callback", include_in_schema=False)
def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    cookie = request.cookies.get(COOKIE, "")
    if not state or not cookie or not secrets.compare_digest(state, cookie):
        raise HTTPException(400, "Autorização inválida. Inicie a conexão pela página da POC.")
    with _lock:
        session = _pending.pop(state, None)
    if not session or session[1] < time.time():
        raise HTTPException(
            400, "A tentativa de conexão expirou. Clique em Conectar Trello novamente."
        )
    if error:
        return finish("denied")
    if not code:
        raise HTTPException(400, "Código de autorização ausente.")
    token = exchange_code(code, session[0], get_settings())
    granted = token.get("scope", "")
    granted = set(granted.split()) if isinstance(granted, str) else set(granted)
    lifetime = token.get("expires_in", 0)
    if (
        granted != SCOPES
        or not token.get("access_token")
        or not isinstance(lifetime, (int, float))
        or not 0 < lifetime <= 604800
    ):
        raise HTTPException(
            502,
            "O Trello retornou permissões ou duração diferentes do esperado. A conexão não foi salva.",
        )
    # No refresh token/offline access: the user can reconnect when this short-lived token expires.
    with _lock:
        set_key(ROOT / ".env", "TRELLO_ACCESS_TOKEN", token["access_token"])
        set_key(ROOT / ".env", "TRELLO_TOKEN_EXPIRES_AT", str(time.time() + lifetime))
    return finish("connected")


def finish(status: str):
    response = RedirectResponse("/?trello=" + status, status_code=303)
    response.delete_cookie(COOKIE, path="/trello")
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response
