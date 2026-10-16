"""Explicit POC routes. A checklist confirmation is the evidence for each move."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class ProductionRoute:
    board_key: str
    lists: tuple[str, str, str, str]


CONFIRMATIONS = (
    "Arquivo liberado para impressão",
    "Impressão iniciada",
    "Impressão concluída",
)
CHECKLIST_NAME = "Etapas de produção — Estampi POC"
ROUTES = {
    "dtf_uv": ProductionRoute(
        "digital",
        (
            "LANÇAMENTO PEDIDOS",
            "PRONTO PARA IMPRESSÃO UV",
            "EM PRODUÇÃO UV",
            "CONFERENCIA UV",
        ),
    ),
    "windflag": ProductionRoute(
        "sublimacao",
        (
            "LANÇAMENTO DO PEDIDO",
            "PRONTO PARA IMPRESSÃO",
            "SENDO IMPRESSO",
            "CALANDRANDO",
        ),
    ),
}


class ProductionFlow(BaseModel):
    product_code: Literal["dtf_uv", "windflag"]
    status: Literal["setup", "active", "blocked", "paused", "finished"] = "setup"
    step: int = Field(default=0, ge=0, le=3)
    checklist_id: str = ""
    item_ids: list[str] = Field(default_factory=lambda: ["", "", ""], min_length=3, max_length=3)
    checked: list[bool] = Field(
        default_factory=lambda: [False, False, False], min_length=3, max_length=3
    )
    pending_create: str = ""
    pending_step: int | None = Field(default=None, ge=1, le=3)
    error: str = ""
    checked_at: str | None = None
    current_list: str = ""

    def public(self) -> dict:
        route = ROUTES[self.product_code]
        return {
            **self.model_dump(exclude={"item_ids", "pending_create"}),
            "steps": [
                {"label": label, "target": route.lists[index + 1], "checked": self.checked[index]}
                for index, label in enumerate(CONFIRMATIONS)
            ],
        }
