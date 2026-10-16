import hashlib
import json

from app.cards import CardPreview, build_card
from app.models import BatchOrder, Order
from app.production_rules import ROUTES
from app.settings import Settings
from app.storage import (
    item_result,
    open_database,
    read_batch,
    read_production,
    record_event,
    register_production,
)
from app.trello import TrelloClient, TrelloError


class OrderConflict(Exception):
    pass


class OrderNotFound(Exception):
    pass


class BatchSendError(Exception):
    def __init__(self, message: str, result: dict, retryable: bool):
        super().__init__(message)
        self.result = result
        self.retryable = retryable


def submit_order(order: Order, settings: Settings, trello: TrelloClient, resolved=None) -> dict:
    """Reserve the order ID before the network call. Never blindly retry an uncertain POST."""
    card = build_card(order, settings.holidays)
    # Preserve fingerprints of single-product orders saved before multi-item support.
    fingerprint = hashlib.sha256(
        order.model_dump_json(exclude={"group_id", "item_id", "ready_at"}).encode()
    ).hexdigest()
    db = open_database(settings.database)
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT fingerprint,status,result,payload,revision FROM orders WHERE order_id=?",
            (order.order_id,),
        ).fetchone()
        if existing:
            db.commit()
            # IDs can contain underscores; a composite ID must never reuse another
            # batch's item (or a legacy single-product order) with the same spelling.
            identity = (
                json.loads(existing[3])
                if existing[3]
                else json.loads(existing[2] or "{}").get("preview", {})
            )
            if (identity.get("group_id", ""), identity.get("item_id", "")) != (
                order.group_id,
                order.item_id,
            ):
                raise OrderConflict(
                    "O identificador deste item já pertence a outro pedido. Use um novo número de pedido."
                )
            if existing[0] != fingerprint:
                raise OrderConflict(
                    "Esse número de pedido já foi usado com outros dados. Use um novo número."
                )
            if existing[1] == "sent":
                return {**item_result(existing[2:]), "duplicate": True}
            if existing[1] != "failed":
                raise OrderConflict(
                    "Esse pedido está em processamento ou sem confirmação. Confira o Trello antes de reenviar; a POC bloqueou uma possível duplicata."
                )
            db.execute(
                "UPDATE orders SET status='pending',payload=COALESCE(payload,?) WHERE order_id=?",
                (order.model_dump_json(), order.order_id),
            )
        else:
            db.execute(
                "INSERT INTO orders (order_id,fingerprint,status,payload) VALUES (?,?,'pending',?)",
                (order.order_id, fingerprint, order.model_dump_json()),
            )
        db.commit()
        posting = False
        try:
            list_id, template = resolved or trello.resolve_target(card)
            posting = True
            created = trello.create_card(card, list_id, template)
            result = {
                "card": created,
                "preview": card.model_dump(mode="json"),
                "duplicate": False,
                "details": order.model_dump(mode="json"),
                "revision": 1,
            }
            db.execute(
                "UPDATE orders SET status = 'sent', result = ? WHERE order_id = ?",
                (json.dumps(result), order.order_id),
            )
            if order.group_id and settings.production_automation:
                register_production(db, order.order_id, order.product_code)
            db.commit()
            return result
        except Exception as error:
            uncertain = posting and (not isinstance(error, TrelloError) or error.uncertain)
            if uncertain:
                db.execute(
                    "UPDATE orders SET status = 'unknown' WHERE order_id = ?", (order.order_id,)
                )
            else:
                db.execute(
                    "UPDATE orders SET status = 'failed' WHERE order_id = ?", (order.order_id,)
                )
            db.commit()
            raise
    finally:
        db.close()


def preview_batch(batch: BatchOrder, settings: Settings) -> dict:
    cards = [
        build_card(order, settings.holidays).model_dump(mode="json") for order in batch.orders()
    ]
    return {
        "order_id": batch.order_id,
        "cards": cards,
        "total": len(cards),
        "drafts": sum(card["status"] == "draft" for card in cards),
        "ready": sum(card["status"] == "ready" for card in cards),
    }


def submit_batch(batch: BatchOrder, settings: Settings, trello: TrelloClient) -> dict:
    """Resume known failures item by item, without ever repeating an uncertain POST."""
    payload = batch.model_dump(mode="json")
    payload["items"] = sorted(payload["items"], key=lambda item: item["item_id"])
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    db = open_database(settings.database)
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT fingerprint, status, result FROM order_batches WHERE order_id = ?",
            (batch.order_id,),
        ).fetchone()
        if existing:
            if existing[0] != fingerprint:
                db.commit()
                raise OrderConflict(
                    "Esse pedido já foi enviado com outros dados. Use um novo número de pedido."
                )
            if existing[1] == "sent":
                record_event(
                    db,
                    batch.order_id,
                    "duplicate_prevented",
                    "Reenvio reconhecido: os mesmos cards foram mantidos, sem duplicatas.",
                )
                db.commit()
                result = read_batch(db, batch.order_id)
                return {**result, "duplicate": True}
            if existing[1] in {"pending", "unknown"}:
                db.commit()
                raise OrderConflict(
                    "O envio anterior está em andamento ou sem confirmação. Confira os cards já criados antes de reenviar."
                )
            db.execute(
                "UPDATE order_batches SET status = 'pending' WHERE order_id = ?", (batch.order_id,)
            )
        else:
            db.execute(
                "INSERT INTO order_batches (order_id,fingerprint,status,payload) VALUES (?,?,'pending',?)",
                (batch.order_id, fingerprint, batch.model_dump_json()),
            )
            record_event(
                db,
                batch.order_id,
                "order_received",
                f"Pedido recebido com {len(batch.items)} produto(s).",
                key=f"{batch.order_id}:received",
            )
        db.commit()

        result = {
            "order_id": batch.order_id,
            "cards": [],
            "total": len(batch.items),
            "status": "pending",
            "duplicate": False,
            "warnings": [],
        }
        orders = batch.orders()
        try:
            # Resolve every board/list/template before creating the first card.
            resolved = [
                trello.resolve_target(build_card(order, settings.holidays)) for order in orders
            ]
            for order, target in zip(orders, resolved):
                created = submit_order(order, settings, trello, resolved=target)
                result["cards"].append(created)
                preview = created["preview"]
                board = "Digital" if preview["board_key"] == "digital" else "Sublimação"
                reason = (
                    " Faltam: " + ", ".join(preview["missing_fields"]) + "."
                    if preview["missing_fields"]
                    else " " + preview["deadline"]["explanation"]
                )
                record_event(
                    db,
                    batch.order_id,
                    "card_created",
                    f"{preview['product_name']} → {board} → {preview['list_name']}.{reason}",
                    item_id=order.item_id,
                    key=f"{order.order_id}:created",
                )
                db.execute(
                    "UPDATE order_batches SET result = ? WHERE order_id = ?",
                    (json.dumps(result), batch.order_id),
                )
                db.commit()
        except (TrelloError, OrderConflict) as error:
            uncertain = isinstance(error, OrderConflict) or error.uncertain
            result["status"] = (
                "unknown" if uncertain else "partial" if result["cards"] else "failed"
            )
            result["error"] = str(error)
            record_event(db, batch.order_id, "send_failed", str(error))
            db.execute(
                "UPDATE order_batches SET status = ?, result = ? WHERE order_id = ?",
                (result["status"], json.dumps(result), batch.order_id),
            )
            db.commit()
            raise BatchSendError(str(error), result, retryable=not uncertain) from None
        except Exception:
            # Unexpected interruptions must not unlock a possibly completed POST.
            result["status"] = "unknown"
            db.execute(
                "UPDATE order_batches SET status = 'unknown', result = ? WHERE order_id = ?",
                (json.dumps(result), batch.order_id),
            )
            db.commit()
            raise

        try:
            trello.link_order_items(result["cards"], batch.order_id)
        except TrelloError:
            result["warnings"].append(
                "Os cards foram criados, mas os links entre itens não foram confirmados."
            )
        verify_results(db, batch.order_id, result["cards"], trello)
        result["status"] = "sent"
        db.execute(
            "UPDATE order_batches SET status = 'sent', result = ? WHERE order_id = ?",
            (json.dumps(result), batch.order_id),
        )
        db.commit()
        return read_batch(db, batch.order_id)
    finally:
        db.close()


def verify_results(db, order_id: str, cards: list[dict], trello: TrelloClient):
    """A failed read-back never turns a confirmed card creation into a retryable POST."""
    for item in cards:
        preview = CardPreview.model_validate(item["preview"])
        production = read_production(db, preview.order_id)
        if production and production.step:
            preview = preview.model_copy(
                update={"list_name": ROUTES[production.product_code].lists[production.step]}
            )
        try:
            verification = trello.verify_card(item["card"], preview)
            verification["status"] = "confirmed" if verification["confirmed"] else "different"
            board = "Digital" if preview.board_key == "digital" else "Sublimação"
            message = (
                f"{preview.product_name}: confirmado no Trello, {board} → {verification['list_name']}."
                if verification["confirmed"]
                else f"{preview.product_name}: a posição atual no Trello difere da prévia ({verification['list_name']})."
            )
            event_key = f"{preview.order_id}:verified:{item.get('revision', 1)}:{verification['list_id']}:{verification['due']}:{verification['confirmed']}"
            record_event(
                db,
                order_id,
                "trello_confirmed" if verification["confirmed"] else "trello_changed",
                message,
                item_id=preview.item_id,
                key=event_key,
            )
        except TrelloError as error:
            verification = {"confirmed": False, "status": "unavailable", "error": str(error)}
        item["verification"] = verification
        saved = {
            key: value for key, value in item.items() if key not in {"pending_update", "production"}
        }
        db.execute(
            "UPDATE orders SET result=? WHERE order_id=? AND revision=?",
            (json.dumps(saved), preview.order_id, item.get("revision", 1)),
        )
    db.commit()


def get_batch(order_id: str, settings: Settings) -> dict:
    db = open_database(settings.database)
    try:
        result = read_batch(db, order_id)
        if result is None:
            raise OrderNotFound("Pedido não encontrado nesta POC.")
        return result
    finally:
        db.close()


def sync_batch(order_id: str, settings: Settings, trello: TrelloClient) -> dict:
    db = open_database(settings.database)
    try:
        result = read_batch(db, order_id)
        if result is None:
            raise OrderNotFound("Pedido não encontrado nesta POC.")
        verify_results(db, order_id, result["cards"], trello)
        return read_batch(db, order_id)
    finally:
        db.close()
