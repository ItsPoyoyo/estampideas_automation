from pydantic import BaseModel, Field


class ProductRule(BaseModel):
    name: str
    board_key: str
    board_name: str
    list_name: str
    template_name: str
    unit: str
    lead_time_business_days: int = Field(gt=0)
    source: str


def digital(name: str, template: str, days: int, unit: str) -> ProductRule:
    return ProductRule(
        name=name,
        board_key="digital",
        board_name="STAMP — DIGITAL (Templates POC)",
        list_name="LANÇAMENTO PEDIDOS",
        template_name=template,
        unit=unit,
        lead_time_business_days=days,
        source="Stamp_Ideias_Prazos.pdf, páginas 2 e 4: prazo de produção após aprovação da arte.",
    )


def sublimacao(name: str, template: str, days: int, unit: str) -> ProductRule:
    return ProductRule(
        name=name,
        board_key="sublimacao",
        board_name="STAMP — SUBLIMAÇÃO (Templates POC)",
        list_name="LANÇAMENTO DO PEDIDO",
        template_name=template,
        unit=unit,
        lead_time_business_days=days,
        source="Stamp_Ideias_Prazos.pdf, páginas 2 e 3: limite superior do prazo de produção após aprovação da arte.",
    )


PRODUCT_RULES = {
    "dtf_uv": digital("DTF UV", "DTF UV", 1, "UND"),
    "dtf_textil": digital("DTF Têxtil / Metragem", "DTF METRAGEM", 2, "M"),
    "sublimacao_metragem": sublimacao("Sublimação por metragem", "METRAGEM TECIDO STAMP", 5, "M"),
    "windflag": sublimacao("Windflag", "WINDFLAG", 7, "UND"),
    "bandeira": sublimacao("Bandeira", "BANDEIRA", 7, "UND"),
    "camiseta_sublimada": sublimacao("Camiseta sublimada", "CAMISETA SUBLIMADA", 10, "UND"),
}


def get_product_rule(product_code: str) -> ProductRule:
    try:
        return PRODUCT_RULES[product_code]
    except KeyError:
        raise ValueError(f"Produto não configurado: {product_code}") from None
