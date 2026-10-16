import re
from datetime import date

from pydantic import BaseModel, Field

from app.deadlines import Deadline, calculate_deadline
from app.models import Order
from app.rules import get_product_rule

REQUIRED_DETAILS = {
    "customer_name": "Cliente",
    "quantity": "Quantidade",
    "dimensions": "Medidas / especificações",
    "customer_contact": "Contato / protocolo",
    "artwork_path": "Arquivo da arte",
    "reference_at": "Aprovação da arte",
}


class CardPreview(BaseModel):
    order_id: str
    board_key: str
    board_name: str
    list_name: str
    template_name: str
    title: str
    description: str
    deadline: Deadline | None
    product_code: str = ""
    product_name: str = ""
    item_id: str = ""
    group_id: str = ""
    status: str = "ready"
    missing_fields: list[str] = Field(default_factory=list)
    missing_keys: list[str] = Field(default_factory=list)


def build_card(order: Order, holidays: frozenset[date] = frozenset()) -> CardPreview:
    rule = get_product_rule(order.product_code)
    missing_keys = [field for field in REQUIRED_DETAILS if getattr(order, field) in (None, "")]
    missing = [REQUIRED_DETAILS[field] for field in missing_keys]
    deadline = None
    if not missing:
        reference = (
            max(order.reference_at, order.ready_at) if order.ready_at else order.reference_at
        )
        label = (
            "Informações completas"
            if order.ready_at and reference == order.ready_at
            else "Arte aprovada"
        )
        deadline = calculate_deadline(
            reference, rule.lead_time_business_days, holidays, event_label=label
        )
    quantity = (
        f"{order.quantity:g} {rule.unit}" if order.quantity is not None else "QTD A CONFIRMAR"
    )
    title = f"{order.group_id or order.order_id} - {order.customer_name or 'CLIENTE A CONFIRMAR'} - {quantity}"
    if order.group_id:
        title += f" - {rule.name}"
    title = title.upper()
    # Markdown otherwise consumes a backslash from Windows UNC paths in Trello.
    ticks = "`" * (1 + max((len(run) for run in re.findall(r"`+", order.artwork_path)), default=0))
    artwork = f"{ticks} {order.artwork_path} {ticks}" if order.artwork_path else "Não informado"
    if deadline:
        production = (
            f"### Prazo de produção\n{deadline.explanation}\n"
            f"Entrega prevista: {deadline.due_at:%d/%m/%Y às %H:%M} (America/Sao_Paulo).\n"
            f"Referência: {rule.source}\n\n"
        )
    else:
        production = (
            "### Informações pendentes\n"
            + "\n".join(f"- [ ] {field}" for field in missing)
            + "\n\nAguardando em ENGATILHADOS. Prazo ainda não definido.\n\n"
        )
    description = (
        f"## Pedido fictício — POC Estampi\n\n"
        f"- Pedido: {order.group_id or order.order_id}\n- Cliente: {order.customer_name or 'Não informado'}\n"
        f"- Produto: {rule.name}\n- Quantidade: {quantity}\n"
        f"- Medidas / especificações: {order.dimensions or 'Não informado'}\n"
        f"- Servidor >> {artwork}\n"
        f"- Nome/protocolo MZ >> {order.customer_contact or 'Não informado'}\n\n"
        + production
        + f"### Observações\n{order.notes or 'Sem observações.'}\n\n"
        f"ESTAMPI_ORDER_ID={order.order_id}"
        + (
            f"\nESTAMPI_GROUP_ID={order.group_id}\nESTAMPI_ITEM_ID={order.item_id}"
            if order.group_id
            else ""
        )
    )
    return CardPreview(
        order_id=order.order_id,
        board_key=rule.board_key,
        board_name=rule.board_name,
        list_name="ENGATILHADOS" if missing else rule.list_name,
        template_name=rule.template_name,
        title=title,
        description=description,
        deadline=deadline,
        product_code=order.product_code,
        product_name=rule.name,
        item_id=order.item_id,
        group_id=order.group_id,
        status="draft" if missing else "ready",
        missing_fields=missing,
        missing_keys=missing_keys,
    )
