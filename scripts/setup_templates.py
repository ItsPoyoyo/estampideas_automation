"""Create only missing POC model cards on the two configured test boards.

Run from the project root: python -m scripts.setup_templates
This script does not modify existing model cards or company boards.
"""

from app.rules import PRODUCT_RULES
from app.settings import get_settings
from app.trello import TrelloClient


def main():
    settings = get_settings()
    if not settings.connected:
        raise SystemExit("Configure a chave e o token no .env primeiro.")
    trello = TrelloClient(settings)
    try:
        for board_key in ("digital", "sublimacao"):
            board_id = trello.test_board(board_key)
            lists = trello.request("GET", f"boards/{board_id}/lists", params={"filter": "open"})
            models = trello.find_one(lists, "CARTÕES MODELO", "lista de modelos")
            cards = trello.request("GET", f"lists/{models['id']}/cards", params={"fields": "name"})
            labels = trello.request("GET", f"boards/{board_id}/labels", params={"limit": "1000"})
            for rule in PRODUCT_RULES.values():
                if rule.board_key != board_key or any(
                    card["name"] == rule.template_name
                    or card["id"] == settings.template_ids.get(rule.template_name)
                    for card in cards
                ):
                    continue
                label = next(
                    (label for label in labels if label["name"] == rule.template_name), None
                )
                if not label:
                    label = trello.request(
                        "POST",
                        "labels",
                        data={"idBoard": board_id, "name": rule.template_name, "color": "green"},
                    )
                    labels.append(label)
                card = trello.request(
                    "POST",
                    "cards",
                    data={
                        "idList": models["id"],
                        "name": rule.template_name,
                        "idLabels": label["id"],
                        "desc": f"Modelo de teste — {rule.name}\n\n- Conferir medidas e quantidade.\n- Servidor >> caminho da arte\n- Nome/protocolo MZ >> contato\n- Usar o vencimento calculado após aprovação da arte.\n\nEstrutura recriada a partir do vídeo; sem dados de clientes reais.",
                    },
                )
                checklist = trello.request(
                    "POST", f"cards/{card['id']}/checklists", data={"name": "Conferência do pedido"}
                )
                for item in (
                    "Dados e medidas conferidos",
                    "Arte aprovada",
                    "Prazo comunicado ao cliente",
                ):
                    trello.request(
                        "POST", f"checklists/{checklist['id']}/checkItems", data={"name": item}
                    )
                print(f"Modelo criado: {rule.template_name}")
    finally:
        trello.close()


if __name__ == "__main__":
    main()
