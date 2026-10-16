from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from app.cards import CardPreview
from app.settings import Settings


class TrelloError(Exception):
    def __init__(self, message: str, uncertain: bool = False, status_code: int = 502):
        super().__init__(message)
        self.uncertain = uncertain
        self.status_code = status_code


class TrelloClient:
    """Only talks to Trello's public REST API. Does not use browser session cookies."""

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self._lists = {}
        self._templates = {}
        auth = (
            {"headers": {"Authorization": f"Bearer {settings.access_token}"}}
            if settings.oauth_ready
            else {"params": {"key": settings.key, "token": settings.token}}
        )
        self.client = httpx.Client(
            base_url="https://api.trello.com/1/",
            timeout=20,
            **auth,
            transport=transport,
        )

    def close(self):
        self.client.close()

    def request(self, method: str, path: str, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise TrelloError(
                "Não foi possível confirmar a resposta do Trello. Confira a conexão.",
                uncertain=method in {"POST", "PUT"},
            ) from None
        if response.is_error:
            message = {
                401: "A conexão com o Trello expirou ou foi recusada. Clique em Conectar Trello novamente.",
                403: "O token não tem permissão para esse quadro.",
                404: "Quadro, lista ou modelo não encontrado no Trello.",
                429: "Limite de requisições do Trello. Aguarde e tente novamente.",
            }.get(response.status_code, f"Trello recusou a operação (HTTP {response.status_code}).")
            raise TrelloError(
                message, uncertain=method in {"POST", "PUT"} and response.status_code >= 500
            )
        try:
            return response.json()
        except ValueError:
            raise TrelloError(
                "Resposta inválida do Trello.", uncertain=method in {"POST", "PUT"}
            ) from None

    @staticmethod
    def find_one(items: list[dict], name: str, kind: str) -> dict:
        matches = [item for item in items if item["name"] == name and not item.get("closed", False)]
        if len(matches) != 1:
            raise TrelloError(
                f"Esperado um(a) {kind} com nome exato '{name}'; encontrados {len(matches)}."
            )
        return matches[0]

    def test_board(self, board_key: str) -> str:
        board_id = self.settings.boards[board_key]
        if board_id in self.settings.blocked_board_ids:
            raise TrelloError("Os quadros de produção não podem ser usados nesta POC.")
        return board_id

    def board_lists(self, board_key: str) -> list[dict]:
        board_id = self.test_board(board_key)
        if board_id not in self._lists:
            self._lists[board_id] = self.request(
                "GET", f"boards/{board_id}/lists", params={"filter": "open"}
            )
        return self._lists[board_id]

    def resolve_list(self, card: CardPreview) -> str:
        return self.find_one(self.board_lists(card.board_key), card.list_name, "lista de entrada")[
            "id"
        ]

    def resolve_target(self, card: CardPreview) -> tuple[str, dict]:
        lists = self.board_lists(card.board_key)
        target_id = self.resolve_list(card)
        models = self.find_one(lists, "CARTÕES MODELO", "lista de modelos")
        if models["id"] not in self._templates:
            self._templates[models["id"]] = self.request(
                "GET", f"lists/{models['id']}/cards", params={"fields": "name,desc,closed,idBoard"}
            )
        templates = self._templates[models["id"]]
        template_id = self.settings.template_ids.get(card.template_name)
        if template_id:
            matches = [
                item
                for item in templates
                if item["id"] == template_id and not item.get("closed", False)
            ]
            if len(matches) != 1:
                raise TrelloError(
                    "O modelo configurado não está na lista de modelos deste quadro de teste."
                )
            template = matches[0]
        else:
            template = self.find_one(templates, card.template_name, "modelo")
        return target_id, template

    def create_card(self, card: CardPreview, list_id: str, template: dict) -> dict:
        self.test_board(card.board_key)
        description = card.description
        if template.get("desc"):
            description += "\n\n---\n\n### Instruções do modelo\n\n" + template["desc"]
        payload = {
            "idList": list_id,
            "idCardSource": template["id"],
            "keepFromSource": "labels,checklists,attachments,customFields",
            "name": card.title,
            "desc": description,
            "dueComplete": "false",
            "pos": "top",
        }
        if card.deadline:
            payload["due"] = card.deadline.due_at.astimezone(UTC).isoformat()
        result = self.request("POST", "cards", data=payload)
        if not isinstance(result, dict) or not result.get("id") or not result.get("url"):
            raise TrelloError(
                "Trello respondeu sem confirmar o card. Confira o quadro antes de reenviar.",
                uncertain=True,
            )
        return {"id": result["id"], "url": result["url"]}

    def link_order_items(self, results: list[dict], group_id: str):
        """Update only cards created by this app in the configured personal boards."""
        if len(results) < 2:
            return
        lines = []
        for result in results:
            url = result["card"]["url"]
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc != "trello.com":
                raise TrelloError("Link de item inválido.")
            preview = result["preview"]
            lines.append(f"- [{preview['product_name']} — item {preview['item_id']}]({url})")
        block = (
            "\n\n<!-- ESTAMPI_RELATED_START -->\n### Itens deste pedido\n"
            + "\n".join(lines)
            + "\n<!-- ESTAMPI_RELATED_END -->"
        )
        for result in results:
            card_id = result["card"]["id"]
            board_id = self.test_board(result["preview"]["board_key"])
            current = self.request("GET", f"cards/{card_id}", params={"fields": "idBoard,desc"})
            if (
                current.get("idBoard") != board_id
                or f"ESTAMPI_ORDER_ID={result['preview']['order_id']}"
                not in current.get("desc", "").splitlines()
                or f"ESTAMPI_GROUP_ID={group_id}" not in current.get("desc", "").splitlines()
            ):
                raise TrelloError("O card não pertence a este pedido de teste.")
            if block not in current["desc"]:
                self.request("PUT", f"cards/{card_id}", data={"desc": current["desc"] + block})

    def read_owned_card(self, card_id: str, preview: CardPreview) -> dict:
        board_id = self.test_board(preview.board_key)
        current = self.request(
            "GET",
            f"cards/{card_id}",
            params={
                "fields": "idBoard,idList,name,desc,due,dueComplete,closed,url,isTemplate",
            },
        )
        if not isinstance(current, dict) or not {"idList", "name", "desc"}.issubset(current):
            raise TrelloError("O Trello não retornou dados suficientes para confirmar o card.")
        lines = current.get("desc", "").splitlines()
        markers = [f"ESTAMPI_ORDER_ID={preview.order_id}"]
        if preview.group_id:
            markers.append(f"ESTAMPI_GROUP_ID={preview.group_id}")
        if current.get("idBoard") != board_id or not all(marker in lines for marker in markers):
            raise TrelloError(
                "O card não pertence a este pedido e quadro de teste.", status_code=409
            )
        return current

    @staticmethod
    def same_due(actual: str | None, expected: str | None) -> bool:
        if not actual or not expected:
            return not actual and not expected
        try:
            return datetime.fromisoformat(actual) == datetime.fromisoformat(expected)
        except ValueError:
            return False

    def verify_card(self, created: dict, preview: CardPreview) -> dict:
        current = self.read_owned_card(created["id"], preview)
        return self.describe_card(current, created, preview)

    def describe_card(self, current: dict, created: dict, preview: CardPreview) -> dict:
        """Describe a card already checked by read_owned_card, without another network read."""
        lists = self.board_lists(preview.board_key)
        listing = next((item for item in lists if item["id"] == current["idList"]), None)
        if listing is None:
            listing = self.request(
                "GET", f"lists/{current['idList']}", params={"fields": "name,idBoard"}
            )
        if listing.get("idBoard", current["idBoard"]) != current["idBoard"]:
            raise TrelloError("A lista retornada não pertence ao quadro de teste.")
        expected_due = preview.deadline.due_at.isoformat() if preview.deadline else None
        return {
            "confirmed": current["idList"] == self.resolve_list(preview)
            and current.get("name") == preview.title
            and not current.get("closed")
            and self.same_due(current.get("due"), expected_due),
            "board_key": preview.board_key,
            "board_id": current["idBoard"],
            "list_id": current["idList"],
            "list_name": listing["name"],
            "due": current.get("due"),
            "expected_list": preview.list_name,
            "url": created["url"],
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def update_card(self, created: dict, previous: CardPreview, updated: CardPreview) -> dict:
        """Complete only this app's pending card, preserving templates and sibling links."""
        target_id = self.resolve_list(updated)
        pending_id = self.resolve_list(previous)
        current = self.read_owned_card(created["id"], previous)
        if current.get("closed") or current.get("dueComplete"):
            raise TrelloError(
                "O card foi arquivado ou concluído no Trello. Confira antes de alterar.",
                status_code=409,
            )
        if current["idList"] not in {pending_id, target_id}:
            raise TrelloError(
                "O card mudou de etapa no Trello. A atualização foi interrompida para preservar essa mudança.",
                status_code=409,
            )
        description = current["desc"]
        if description.startswith(updated.description):
            desired_description = description
        elif description.startswith(previous.description):
            desired_description = updated.description + description[len(previous.description) :]
        else:
            raise TrelloError(
                "A descrição foi editada no Trello. Confira o card antes de atualizar.",
                status_code=409,
            )
        if current.get("name") not in {previous.title, updated.title}:
            raise TrelloError(
                "O título foi editado no Trello. Confira o card antes de atualizar.",
                status_code=409,
            )
        due = updated.deadline.due_at.astimezone(UTC).isoformat() if updated.deadline else None
        previous_due = previous.deadline.due_at.isoformat() if previous.deadline else None
        if not self.same_due(current.get("due"), previous_due) and not self.same_due(
            current.get("due"), due
        ):
            raise TrelloError(
                "O prazo foi editado no Trello. Confira o card antes de atualizar.", status_code=409
            )
        already_applied = (
            current["idList"] == target_id
            and current["name"] == updated.title
            and description == desired_description
            and self.same_due(current.get("due"), due)
        )
        if not already_applied:
            self.request(
                "PUT",
                f"cards/{created['id']}",
                json={
                    "name": updated.title,
                    "desc": desired_description,
                    "idList": target_id,
                    "due": due,
                    "dueComplete": False,
                },
            )
        try:
            verification = self.verify_card(created, updated)
            verification["status"] = "confirmed" if verification["confirmed"] else "different"
            return verification
        except TrelloError as error:
            return {"confirmed": False, "status": "unavailable", "error": str(error)}
