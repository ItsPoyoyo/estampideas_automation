"""Local checklist watcher: explicit confirmations advance only this app's POC cards.

One server process owns the worker and its lock. SQLite preserves setup/move intents
so a restart or a lost response can be reconciled before any further write.
"""

import json
import logging
import threading
from dataclasses import dataclass

from app.cards import CardPreview
from app.models import Order
from app.production_rules import CHECKLIST_NAME, CONFIRMATIONS, ROUTES, ProductionFlow
from app.service import OrderConflict, OrderNotFound, get_batch
from app.storage import open_database, read_production, record_event, register_production, utc_now
from app.trello import TrelloClient, TrelloError

POLL_SECONDS = 10
logger = logging.getLogger(__name__)


@dataclass
class Context:
    order: Order
    result: dict
    revision: int
    flow: ProductionFlow | None
    updating: bool


class ProductionWatcher:
    def __init__(self, settings_provider, client_factory=TrelloClient):
        self.settings_provider = settings_provider
        self.client_factory = client_factory
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.changed = threading.Event()
        self.thread = None
        self.last_checked_at = None
        self.last_error = ""

    def start(self):
        if self.settings_provider().production_automation:
            self.thread = threading.Thread(target=self._loop, name="trello-production", daemon=True)
            self.thread.start()

    def wake(self):
        self.changed.set()

    def stop(self):
        self.stopping.set()
        self.wake()
        if self.thread:
            self.thread.join(timeout=25)

    def _loop(self):
        while not self.stopping.is_set():
            self.changed.clear()
            try:
                self.run_once()
            except Exception as error:
                # Avoid logging requests, credentials, or raw provider responses.
                logger.error("Production check failed (%s)", type(error).__name__)
                self.last_error = "Não foi possível concluir a checagem automática."
            self.changed.wait(POLL_SECONDS)

    @staticmethod
    def _context(settings, child_id: str) -> Context:
        db = open_database(settings.database)
        try:
            row = db.execute(
                "SELECT payload,result,revision,status FROM orders WHERE order_id=?", (child_id,)
            ).fetchone()
            if not row:
                raise OrderNotFound("Item não encontrado nesta POC.")
            if row[3] != "sent" or not row[0]:
                raise OrderConflict("Use um item enviado com dados salvos nesta versão da POC.")
            order = Order.model_validate_json(row[0])
            if order.product_code not in ROUTES or not order.group_id:
                raise OrderConflict(
                    "A automação de produção está disponível para DTF UV e Windflag em pedidos com itens."
                )
            parent = db.execute(
                "SELECT status FROM order_batches WHERE order_id=?", (order.group_id,)
            ).fetchone()
            if not parent or parent[0] != "sent":
                raise OrderConflict("Conclua o envio do pedido antes de automatizar a produção.")
            updating = db.execute(
                "SELECT 1 FROM item_updates WHERE order_id=? AND from_revision=? AND status IN ('pending','unknown')",
                (child_id, row[2]),
            ).fetchone()
            result = json.loads(row[1])
            preview = CardPreview.model_validate(result["preview"])
            if (
                preview.order_id != child_id
                or preview.group_id != order.group_id
                or preview.item_id != order.item_id
            ):
                raise OrderConflict("A identidade do item não corresponde ao pedido salvo.")
            flow = read_production(db, child_id)
            if flow and flow.product_code != order.product_code:
                raise OrderConflict("O produto não corresponde à automação salva.")
            return Context(order, result, row[2], flow, bool(updating))
        finally:
            db.close()

    @staticmethod
    def _save(settings, context, *, verification=None, moved=False, event=None):
        flow = context.flow
        db = open_database(settings.database)
        try:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE production_flows SET state=? WHERE order_id=?",
                (flow.model_dump_json(), context.order.order_id),
            )
            if verification is not None:
                result = {
                    key: value
                    for key, value in context.result.items()
                    if key not in {"production", "pending_update"}
                }
                result.update(verification=verification, revision=context.revision + int(moved))
                db.execute(
                    "UPDATE orders SET result=?,revision=? WHERE order_id=? AND revision=?",
                    (
                        json.dumps(result),
                        result["revision"],
                        context.order.order_id,
                        context.revision,
                    ),
                )
            if event:
                kind, message, key = event
                record_event(
                    db,
                    context.order.group_id,
                    kind,
                    message,
                    item_id=context.order.item_id,
                    key=key,
                )
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _read(client, context):
        preview = CardPreview.model_validate(context.result["preview"])
        if (
            preview.board_key != ROUTES[context.order.product_code].board_key
            or preview.product_code != context.order.product_code
        ):
            raise TrelloError(
                "O produto e o quadro não correspondem à rota de produção salva.", status_code=409
            )
        current = client.read_owned_card(context.result["card"]["id"], preview)
        if current.get("closed") or current.get("dueComplete") or current.get("isTemplate"):
            raise TrelloError(
                "O card foi concluído, arquivado ou marcado como modelo. Confira antes de retomar.",
                status_code=409,
            )
        expected_due = preview.deadline.due_at.isoformat() if preview.deadline else None
        if (
            current["name"] != preview.title
            or not current["desc"].startswith(preview.description)
            or not client.same_due(current.get("due"), expected_due)
        ):
            raise TrelloError(
                "Título, descrição ou prazo foram alterados no Trello. Confira antes de retomar.",
                status_code=409,
            )
        return preview, current

    @staticmethod
    def _lists(client, context):
        route = ROUTES[context.order.product_code]
        lists = client.board_lists(route.board_key)
        return [client.find_one(lists, name, "etapa de produção")["id"] for name in route.lists]

    def _prepare(self, settings, client, context):
        """Reconcile each setup POST by a unique name before considering another POST."""
        flow = context.flow
        card_id = context.result["card"]["id"]
        checklists = client.request(
            "GET",
            f"cards/{card_id}/checklists",
            params={"checkItems": "all", "fields": "name", "checkItem_fields": "name,state"},
        )
        matches = [item for item in checklists if item["name"] == CHECKLIST_NAME]
        if len(matches) > 1:
            raise TrelloError(
                "Há mais de um checklist da POC. Confira o card antes de retomar.", status_code=409
            )
        if flow.checklist_id:
            if len(matches) != 1 or matches[0]["id"] != flow.checklist_id:
                raise TrelloError(
                    "O checklist da automação foi removido ou alterado. Confira o card.",
                    status_code=409,
                )
            checklist = matches[0]
        elif matches:
            checklist = matches[0]
            flow.checklist_id = checklist["id"]
            flow.pending_create = ""
            self._save(settings, context)
        else:
            if flow.pending_create:
                raise TrelloError(
                    "A criação do checklist ficou sem confirmação. Aguardando a leitura no Trello, sem criar outro.",
                    uncertain=True,
                )
            flow.pending_create = "checklist"
            self._save(settings, context)
            try:
                checklist = client.request(
                    "POST",
                    f"cards/{card_id}/checklists",
                    json={"name": CHECKLIST_NAME, "pos": "bottom"},
                )
                if not isinstance(checklist, dict) or not checklist.get("id"):
                    raise TrelloError("A criação do checklist não foi confirmada.", uncertain=True)
            except TrelloError as error:
                if not error.uncertain:
                    flow.pending_create = ""
                raise
            flow.checklist_id = checklist["id"]
            flow.pending_create = ""
            checklist.setdefault("checkItems", [])
            self._save(settings, context)
        items = checklist.get("checkItems", [])
        for index, label in enumerate(CONFIRMATIONS):
            matches = [item for item in items if item["name"] == label]
            if len(matches) > 1 or (
                flow.item_ids[index] and (not matches or matches[0]["id"] != flow.item_ids[index])
            ):
                raise TrelloError(
                    "Uma confirmação do checklist foi alterada ou duplicada. Confira o card.",
                    status_code=409,
                )
            if not matches:
                if flow.pending_create:
                    raise TrelloError(
                        "Uma confirmação ficou sem resposta. Aguardando a leitura no Trello, sem duplicar o item.",
                        uncertain=True,
                    )
                flow.pending_create = f"item:{index}"
                self._save(settings, context)
                try:
                    item = client.request(
                        "POST",
                        f"checklists/{flow.checklist_id}/checkItems",
                        json={"name": label, "pos": "bottom", "checked": False},
                    )
                    if not isinstance(item, dict) or not item.get("id"):
                        raise TrelloError(
                            "A criação da confirmação não foi confirmada.", uncertain=True
                        )
                except TrelloError as error:
                    if not error.uncertain:
                        flow.pending_create = ""
                    raise
                matches = [item]
            flow.item_ids[index] = matches[0]["id"]
            flow.checked[index] = matches[0].get("state") == "complete"
            if flow.pending_create == f"item:{index}":
                flow.pending_create = ""
            self._save(settings, context)

    def _finish_move(self, settings, client, context, current):
        flow = context.flow
        next_step = flow.pending_step
        if not all(flow.checked[:next_step]):
            raise TrelloError(
                "Uma confirmação foi removida durante a movimentação. Confira o checklist e a etapa do card.",
                status_code=409,
            )
        next_list = ROUTES[flow.product_code].lists[next_step]
        preview = CardPreview.model_validate(context.result["preview"]).model_copy(
            update={"list_name": next_list}
        )
        verification = client.describe_card(current, context.result["card"], preview)
        verification["status"] = "confirmed" if verification["confirmed"] else "different"
        if not verification["confirmed"]:
            raise TrelloError("A nova etapa não foi confirmada no Trello.", status_code=409)
        flow.step = next_step
        flow.pending_step = None
        flow.status = "finished" if flow.step == 3 else "active"
        flow.error = ""
        flow.checked_at = utc_now().isoformat()
        flow.current_list = next_list
        message = f"{preview.product_name}: '{CONFIRMATIONS[flow.step - 1]}' confirmado → {flow.current_list}. Mesmo card, movimentado automaticamente."
        self._save(
            settings,
            context,
            verification=verification,
            moved=True,
            event=(
                "production_moved",
                message,
                f"{context.order.order_id}:production:step:{flow.step}",
            ),
        )

    def _cycle(self, settings, client, context):
        flow = context.flow
        route = ROUTES[flow.product_code]
        preview, current = self._read(client, context)
        lists = self._lists(client, context)
        listing = next(
            (
                item["name"]
                for item in client.board_lists(route.board_key)
                if item["id"] == current["idList"]
            ),
            "Lista não disponível",
        )
        flow.current_list = listing
        flow.checked_at = utc_now().isoformat()
        if flow.pending_step is not None and current["idList"] == lists[flow.pending_step]:
            self._prepare(settings, client, context)
            self._finish_move(settings, client, context, current)
            return
        pending_order = preview.status == "draft" and flow.step == 0 and listing == "ENGATILHADOS"
        if current["idList"] != lists[flow.step] and not pending_order:
            raise TrelloError(
                "O card foi movido manualmente para outra etapa. A automação está pausada para revisão.",
                status_code=409,
            )
        self._prepare(settings, client, context)
        if not all(flow.checked[: flow.step]):
            raise TrelloError(
                "Uma etapa já processada foi desmarcada. Confira as confirmações antes de retomar.",
                status_code=409,
            )
        missing = preview.missing_fields
        out_of_order = any(
            flow.checked[index] and not all(flow.checked[:index]) for index in range(3)
        )
        if missing or out_of_order:
            flow.status = "blocked"
            flow.error = (
                "Complete os dados do pedido: " + ", ".join(missing) + "."
                if missing
                else "Confirme as etapas na ordem do checklist."
            )
        else:
            flow.status = "active"
            flow.error = ""
        expected = preview.model_copy(
            update={"list_name": route.lists[flow.step] if flow.step else preview.list_name}
        )
        verification = client.describe_card(current, context.result["card"], expected)
        verification["status"] = "confirmed" if verification["confirmed"] else "different"
        self._save(settings, context, verification=verification)
        if flow.step == 3:
            flow.status = "finished"
            self._save(settings, context)
            return
        if flow.status == "blocked" or not flow.checked[flow.step]:
            return
        flow.pending_step = flow.step + 1
        self._save(settings, context)
        try:
            client.request(
                "PUT",
                f"cards/{context.result['card']['id']}",
                json={"idList": lists[flow.pending_step]},
            )
        except TrelloError as error:
            if not error.uncertain:
                flow.pending_step = None
            raise
        _, moved = self._read(client, context)
        if moved["idList"] != lists[flow.pending_step]:
            raise TrelloError(
                "O Trello não confirmou a mudança de etapa. Confira o card.", status_code=409
            )
        self._finish_move(settings, client, context, moved)

    def run_once(self):
        settings = self.settings_provider()
        if not settings.production_automation or not settings.connected:
            return
        with self.lock:
            db = open_database(settings.database)
            try:
                ids = [
                    row[0]
                    for row in db.execute(
                        "SELECT order_id FROM production_flows WHERE json_extract(state,'$.status') NOT IN ('paused','finished')"
                    )
                ]
            finally:
                db.close()
            client = self.client_factory(settings)
            try:
                for child_id in ids:
                    if self.stopping.is_set():
                        break
                    try:
                        context = self._context(settings, child_id)
                    except (OrderConflict, OrderNotFound):
                        continue
                    if context.updating:
                        continue
                    try:
                        self._cycle(settings, client, context)
                    except TrelloError as error:
                        previous_error = context.flow.error
                        context.flow.error = str(error)
                        if error.status_code == 409:
                            context.flow.status = "paused"
                        event = None
                        if context.flow.status == "paused" and previous_error != str(error):
                            event = (
                                "production_paused",
                                f"{context.result['preview']['product_name']}: {error}",
                                None,
                            )
                        self._save(settings, context, event=event)
                self.last_checked_at = utc_now().isoformat()
                self.last_error = ""
            finally:
                client.close()

    def action(self, order_id: str, item_id: str, action: str, step: int | None = None):
        settings = self.settings_provider()
        if not settings.production_automation or not settings.connected:
            raise TrelloError(
                "Conecte o Trello e habilite a automação de produção para continuar.",
                status_code=503,
            )
        with self.lock:
            context = self._context(settings, f"{order_id}__{item_id}")
            if (
                context.order.group_id != order_id
                or context.order.item_id != item_id
                or context.updating
            ):
                raise OrderConflict("Confira a identidade e a atualização pendente deste item.")
            client = self.client_factory(settings)
            try:
                preview, current = self._read(client, context)
                lists = self._lists(client, context)
                if action == "enable":
                    if context.flow is None:
                        db = open_database(settings.database)
                        try:
                            register_production(
                                db, context.order.order_id, context.order.product_code
                            )
                            db.commit()
                            context.flow = read_production(db, context.order.order_id)
                        finally:
                            db.close()
                elif context.flow is None:
                    raise OrderConflict("Ative as etapas deste card primeiro.")
                elif action == "pause":
                    context.flow.status = "paused"
                    context.flow.error = "Pausada por você. As próximas etapas aguardam revisão."
                    self._save(
                        settings,
                        context,
                        event=(
                            "production_paused",
                            f"{preview.product_name}: automação pausada por você.",
                            None,
                        ),
                    )
                elif action == "resume":
                    if context.flow.status != "paused":
                        raise OrderConflict("Este card não está pausado.")
                    allowed = current["idList"] == lists[context.flow.step] or (
                        context.flow.step == 0
                        and preview.status == "draft"
                        and current["idList"] == client.resolve_list(preview)
                    )
                    if not allowed:
                        raise OrderConflict(
                            "Retorne o card à última etapa confirmada antes de retomar a automação."
                        )
                    context.flow.status = "setup" if not context.flow.checklist_id else "active"
                    context.flow.error = ""
                    self._save(
                        settings,
                        context,
                        event=(
                            "production_resumed",
                            f"{preview.product_name}: automação retomada após revisão.",
                            None,
                        ),
                    )
                elif action == "confirm":
                    flow = context.flow
                    if (
                        flow.status not in {"active", "blocked"}
                        or preview.status != "ready"
                        or step != flow.step
                        or step not in range(3)
                        or not flow.item_ids[step]
                    ):
                        raise OrderConflict(
                            "Aguarde o checklist e confirme somente a próxima etapa de um item completo."
                        )
                    if current["idList"] != lists[flow.step]:
                        raise OrderConflict(
                            "O card mudou de etapa no Trello. Aguarde a checagem antes de confirmar."
                        )
                    self._prepare(settings, client, context)
                    if not all(flow.checked[:step]):
                        raise OrderConflict(
                            "Uma confirmação anterior foi removida. Confira o checklist."
                        )
                    client.request(
                        "PUT",
                        f"cards/{context.result['card']['id']}/checkItem/{flow.item_ids[step]}",
                        json={"state": "complete"},
                    )
                else:
                    raise OrderConflict("Ação de produção não reconhecida.")
            finally:
                client.close()
        self.wake()
        return get_batch(order_id, settings)
