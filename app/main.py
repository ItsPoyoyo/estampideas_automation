from contextlib import asynccontextmanager, nullcontext

from fastapi import FastAPI, HTTPException, Path
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.cards import CardPreview, build_card
from app.demo import cutoff_example
from app.followup import complete_item, completion_context, preview_completion
from app.intake import IntakeAgent, IntakeError, IntakeRequest
from app.models import (
    BatchOrder,
    CompletionRequest,
    FollowupRequest,
    Order,
    ProductCode,
    ProductionConfirmation,
)
from app.oauth import router as oauth_router
from app.production import POLL_SECONDS, ProductionWatcher
from app.rules import PRODUCT_RULES
from app.service import (
    BatchSendError,
    OrderConflict,
    OrderNotFound,
    get_batch,
    preview_batch,
    submit_batch,
    submit_order,
    sync_batch,
)
from app.settings import ROOT, get_settings
from app.trello import TrelloClient, TrelloError


@asynccontextmanager
async def lifespan(application):
    watcher = ProductionWatcher(get_settings, lambda settings: TrelloClient(settings))
    application.state.production = watcher
    watcher.start()
    try:
        yield
    finally:
        watcher.stop()


app = FastAPI(title="Estampi — Pedidos → Trello", version="0.2.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
app.include_router(oauth_router)


@app.exception_handler(OrderNotFound)
def not_found(_request, error):
    return JSONResponse(status_code=404, content={"detail": str(error)})


@app.exception_handler(OrderConflict)
def order_conflict(_request, error):
    return JSONResponse(status_code=409, content={"detail": str(error)})


@app.exception_handler(TrelloError)
def trello_error(_request, error):
    return JSONResponse(
        status_code=error.status_code, content={"detail": str(error), "uncertain": error.uncertain}
    )


@app.exception_handler(ValidationError)
def invalid_details(_request, error):
    return JSONResponse(
        status_code=422, content={"detail": " · ".join(item["msg"] for item in error.errors())}
    )


def connected_settings():
    settings = get_settings()
    if not settings.connected:
        raise HTTPException(
            503, "Conecte o Trello para enviar. A prévia e o histórico continuam disponíveis."
        )
    return settings


def production_lock():
    watcher = getattr(app.state, "production", None)
    return watcher.lock if watcher else nullcontext()


@app.get("/", include_in_schema=False)
def home():
    return FileResponse(ROOT / "app" / "static" / "index.html")


@app.get("/health")
def health():
    settings = get_settings()
    return {
        "status": "ok",
        "trello_configured": settings.connected,
        "trello_authorization_available": settings.can_authorize,
        "token_expires_at": settings.token_expires_at,
        "holidays": sorted(settings.holidays),
        "ai_configured": settings.ai_configured,
        "ai_provider": "OpenAI",
        "ai_model": settings.openai_model,
        "production_automation": settings.production_automation,
        "production_poll_seconds": POLL_SECONDS,
    }


@app.get("/products")
def products():
    return [{"code": code, **rule.model_dump()} for code, rule in PRODUCT_RULES.items()]


@app.post("/intake/parse")
def parse_intake(request: IntakeRequest):
    agent = IntakeAgent(get_settings())
    try:
        return agent.extract(request.text)
    except IntakeError as error:
        raise HTTPException(error.status_code, str(error)) from None
    finally:
        agent.close()


@app.post("/orders/batch/preview")
def preview_order_batch(order: BatchOrder):
    return preview_batch(order, get_settings())


@app.get("/demo/cutoff")
def show_cutoff(product_code: ProductCode = "dtf_uv"):
    return cutoff_example(product_code, get_settings())


@app.get("/orders/batch/{order_id}")
def read_order_batch(order_id: str):
    return get_batch(order_id, get_settings())


@app.post("/orders/batch/{order_id}/sync")
def confirm_order_batch(order_id: str):
    settings = connected_settings()
    client = TrelloClient(settings)
    try:
        with production_lock():
            return sync_batch(order_id, settings, client)
    finally:
        client.close()


@app.post("/orders/batch/{order_id}/items/{item_id}/suggest")
def suggest_completion(order_id: str, item_id: str, request: FollowupRequest):
    settings = get_settings()
    context = completion_context(order_id, item_id, request.revision, settings)
    agent = IntakeAgent(settings)
    try:
        return agent.complete(request.text, context["order"], context["missing"])
    except IntakeError as error:
        raise HTTPException(error.status_code, str(error)) from None
    finally:
        agent.close()


@app.post("/orders/batch/{order_id}/items/{item_id}/preview")
def preview_item_completion(order_id: str, item_id: str, request: CompletionRequest):
    return preview_completion(order_id, item_id, request, get_settings())


@app.patch("/orders/batch/{order_id}/items/{item_id}")
def save_item_completion(order_id: str, item_id: str, request: CompletionRequest):
    settings = connected_settings()
    client = TrelloClient(settings)
    try:
        with production_lock():
            result = complete_item(order_id, item_id, request, settings, client)
        watcher = getattr(app.state, "production", None)
        if watcher:
            watcher.wake()
        return result
    finally:
        client.close()


@app.post("/orders/batch")
def create_order_batch(order: BatchOrder):
    settings = get_settings()
    if not settings.connected:
        raise HTTPException(
            503, "Conecte o Trello para criar os cards. A prévia funciona sem conexão."
        )
    client = TrelloClient(settings)
    try:
        result = submit_batch(order, settings, client)
        watcher = getattr(app.state, "production", None)
        if watcher:
            watcher.wake()
        return result
    except OrderConflict as error:
        raise HTTPException(409, str(error)) from None
    except BatchSendError as error:
        return JSONResponse(
            status_code=502,
            content={"detail": str(error), "result": error.result, "retryable": error.retryable},
        )
    finally:
        client.close()


@app.post("/orders/batch/{order_id}/items/{item_id}/production/enable")
def enable_production(order_id: str, item_id: str):
    return app.state.production.action(order_id, item_id, "enable")


@app.post("/orders/batch/{order_id}/items/{item_id}/production/pause")
def pause_production(order_id: str, item_id: str):
    return app.state.production.action(order_id, item_id, "pause")


@app.post("/orders/batch/{order_id}/items/{item_id}/production/resume")
def resume_production(order_id: str, item_id: str):
    return app.state.production.action(order_id, item_id, "resume")


@app.patch("/orders/batch/{order_id}/items/{item_id}/production/checks/{step}")
def confirm_production(
    order_id: str, item_id: str, request: ProductionConfirmation, step: int = Path(ge=0, le=2)
):
    return app.state.production.action(order_id, item_id, "confirm", step)


@app.post("/orders/validate", response_model=Order)
def validate_order(order: Order):
    return order


@app.post("/orders/preview", response_model=CardPreview)
def preview_order(order: Order):
    return build_card(order, get_settings().holidays)


@app.post("/orders")
def create_order(order: Order):
    settings = get_settings()
    if not settings.connected:
        raise HTTPException(
            503, "Conecte o Trello pelo botão no topo da página. A prévia funciona sem conexão."
        )
    client = TrelloClient(settings)
    try:
        return submit_order(order, settings, client)
    except OrderConflict as error:
        raise HTTPException(409, str(error)) from None
    except TrelloError as error:
        message = str(error)
        if error.uncertain:
            message += " O pedido foi bloqueado para evitar duplicação; confira o quadro."
        raise HTTPException(502, message) from None
    finally:
        client.close()
