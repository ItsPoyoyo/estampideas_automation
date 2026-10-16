"""Complete missing facts with optimistic revisions and an idempotent update of one card."""

import hashlib
import json

from app.cards import REQUIRED_DETAILS, CardPreview, build_card
from app.models import CompletionRequest, Order
from app.service import OrderConflict, OrderNotFound
from app.settings import Settings
from app.storage import open_database, read_batch, record_event, utc_now
from app.trello import TrelloClient, TrelloError


def missing_keys(order: Order) -> list[str]:
    return [field for field in REQUIRED_DETAILS if getattr(order, field) in (None, "")]


def load_item(db, order_id: str, item_id: str):
    row = db.execute(
        "SELECT payload,result,revision,status FROM orders WHERE order_id=?",
        (f"{order_id}__{item_id}",),
    ).fetchone()
    if not row:
        raise OrderNotFound("Item não encontrado neste pedido.")
    if row[3] != "sent" or not row[0]:
        raise OrderConflict(
            "Este item não tem dados confirmados para atualização. Confira o envio original."
        )
    order = Order.model_validate_json(row[0])
    if order.group_id != order_id or order.item_id != item_id:
        raise OrderConflict("O item não pertence a este pedido.")
    result = json.loads(row[1])
    result.pop("pending_update", None)
    return order, result, row[2]


def completed_order(order: Order, changes: dict) -> Order:
    if not changes:
        raise OrderConflict("Informe pelo menos um dos dados pendentes.")
    missing = missing_keys(order)
    if any(field not in missing for field in changes):
        raise OrderConflict(
            "A resposta contém um campo já preenchido. Confira os dados atuais antes de atualizar."
        )
    candidate = Order.model_validate({**order.model_dump(mode="json"), **changes})
    if not missing_keys(candidate):
        # Production cannot be promised before the final required facts actually arrive.
        candidate.ready_at = utc_now()
    return candidate


def completion_context(order_id: str, item_id: str, revision: int, settings: Settings) -> dict:
    db = open_database(settings.database)
    try:
        order, result, current_revision = load_item(db, order_id, item_id)
        if current_revision != revision:
            raise OrderConflict("Este item mudou. Atualize a página para ver os dados atuais.")
        return {"order": order, "preview": result["preview"], "missing": missing_keys(order)}
    finally:
        db.close()


def preview_completion(
    order_id: str, item_id: str, request: CompletionRequest, settings: Settings
) -> dict:
    context = completion_context(order_id, item_id, request.revision, settings)
    changes = request.changes.model_dump(mode="json", exclude_none=True)
    candidate = completed_order(context["order"], changes)
    return {
        "preview": build_card(candidate, settings.holidays).model_dump(mode="json"),
        "changes": changes,
        "revision": request.revision,
    }


def complete_item(
    order_id: str,
    item_id: str,
    request: CompletionRequest,
    settings: Settings,
    trello: TrelloClient,
) -> dict:
    changes = request.changes.model_dump(mode="json", exclude_none=True)
    fingerprint = hashlib.sha256(json.dumps(changes, sort_keys=True).encode()).hexdigest()
    db = open_database(settings.database)
    try:
        db.execute("BEGIN IMMEDIATE")
        previous, result, revision = load_item(db, order_id, item_id)
        operation = db.execute(
            "SELECT fingerprint,status,payload FROM item_updates WHERE order_id=? AND from_revision=?",
            (previous.order_id, request.revision),
        ).fetchone()
        if operation and operation[0] == fingerprint and operation[1] == "done":
            db.commit()
            return {**read_batch(db, order_id), "duplicate": True}
        if request.revision != revision:
            raise OrderConflict("Este item mudou. Atualize os dados antes de continuar.")
        if operation and operation[1] == "pending":
            raise OrderConflict("A atualização deste item já está em andamento.")
        if operation and operation[1] == "unknown" and operation[0] != fingerprint:
            raise OrderConflict("Confirme a atualização anterior antes de enviar dados diferentes.")
        if operation and operation[0] == fingerprint:
            candidate = Order.model_validate_json(operation[2])
        else:
            candidate = completed_order(previous, changes)
        updated = build_card(candidate, settings.holidays)
        db.execute(
            """INSERT INTO item_updates (order_id,from_revision,fingerprint,status,payload)
            VALUES (?,?,?,'pending',?) ON CONFLICT(order_id,from_revision) DO UPDATE SET
            fingerprint=excluded.fingerprint,status='pending',payload=excluded.payload""",
            (previous.order_id, revision, fingerprint, candidate.model_dump_json()),
        )
        record_event(
            db,
            order_id,
            "completion_received",
            f"{updated.product_name}: recebidos {', '.join(REQUIRED_DETAILS[field] for field in changes)}.",
            item_id=item_id,
            key=f"{previous.order_id}:completion:{revision}:received",
        )
        db.commit()
        try:
            verification = trello.update_card(
                result["card"], CardPreview.model_validate(result["preview"]), updated
            )
        except Exception as error:
            # A PUT can be checked and resumed with the same facts; it never creates another card.
            state = "unknown" if not isinstance(error, TrelloError) or error.uncertain else "failed"
            db.execute(
                "UPDATE item_updates SET status=? WHERE order_id=? AND from_revision=?",
                (state, previous.order_id, revision),
            )
            db.commit()
            raise
        new_result = {
            **result,
            "preview": updated.model_dump(mode="json"),
            "details": candidate.model_dump(mode="json"),
            "revision": revision + 1,
            "duplicate": False,
            "verification": verification,
        }
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE orders SET payload=?,result=?,revision=? WHERE order_id=? AND revision=?",
            (
                candidate.model_dump_json(),
                json.dumps(new_result),
                revision + 1,
                previous.order_id,
                revision,
            ),
        )
        db.execute(
            "UPDATE item_updates SET status='done',result=? WHERE order_id=? AND from_revision=?",
            (json.dumps(new_result), previous.order_id, revision),
        )
        confirmed = verification.get("confirmed", False)
        if updated.status == "ready" and confirmed:
            message = f"{updated.product_name}: mesmo card liberado para {verification['list_name']}. {updated.deadline.explanation}"
            kind = "card_released"
        else:
            message = f"{updated.product_name}: dados atualizados no mesmo card. " + (
                "Ainda faltam: " + ", ".join(updated.missing_fields) + "."
                if updated.missing_fields
                else "Aguardando confirmação da posição no Trello."
            )
            kind = "card_updated"
        record_event(
            db,
            order_id,
            kind,
            message,
            item_id=item_id,
            key=f"{previous.order_id}:completion:{revision}:saved",
        )
        if confirmed:
            board = "Digital" if updated.board_key == "digital" else "Sublimação"
            record_event(
                db,
                order_id,
                "trello_confirmed",
                f"{updated.product_name}: confirmado, {board} → {verification['list_name']}.",
                item_id=item_id,
                key=f"{previous.order_id}:verified:{revision + 1}",
            )
        db.commit()
        return read_batch(db, order_id)
    finally:
        db.close()
