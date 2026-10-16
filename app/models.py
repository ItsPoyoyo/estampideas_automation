from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

ProductCode = Literal[
    "dtf_uv", "dtf_textil", "sublimacao_metragem", "windflag", "bandeira", "camiseta_sublimada"
]


class Order(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    order_id: str = Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9_-]+$")
    customer_name: str = Field(default="", max_length=160)
    product_code: ProductCode
    quantity: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)
    dimensions: str = Field(default="", max_length=200)
    customer_contact: str = Field(default="", max_length=200)
    artwork_path: str = Field(default="", max_length=600)
    reference_at: AwareDatetime | None = None
    notes: str = Field(default="", max_length=4001)
    group_id: str = Field(default="", max_length=50)
    item_id: str = Field(default="", max_length=8)
    ready_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_quantity(self):
        if self.quantity is None:
            return self
        if self.product_code in {"dtf_textil", "sublimacao_metragem"}:
            if self.quantity > 100:
                raise ValueError(
                    "Pedidos acima de 100 m precisam de consulta à produção antes de prometer um prazo."
                )
        elif not self.quantity.is_integer():
            raise ValueError("A quantidade deste produto deve ser um número inteiro de unidades.")
        return self


class OrderItem(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    item_id: str = Field(min_length=1, max_length=8, pattern=r"^[A-Za-z0-9_-]+$")
    product_code: ProductCode
    quantity: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)
    dimensions: str = Field(default="", max_length=200)
    artwork_path: str = Field(default="", max_length=600)
    reference_at: AwareDatetime | None = None
    notes: str = Field(default="", max_length=2000)


class BatchOrder(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    order_id: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    customer_name: str = Field(default="", max_length=160)
    customer_contact: str = Field(default="", max_length=200)
    artwork_path: str = Field(default="", max_length=600)
    reference_at: AwareDatetime | None = None
    notes: str = Field(default="", max_length=2000)
    items: list[OrderItem] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_items(self):
        if len({item.item_id for item in self.items}) != len(self.items):
            raise ValueError("Cada item precisa ter um identificador diferente.")
        self.orders()  # Reuse the product-specific quantity validation.
        return self

    def orders(self) -> list[Order]:
        return [
            Order(
                order_id=f"{self.order_id}__{item.item_id}",
                group_id=self.order_id,
                item_id=item.item_id,
                customer_name=self.customer_name,
                customer_contact=self.customer_contact,
                product_code=item.product_code,
                quantity=item.quantity,
                dimensions=item.dimensions,
                artwork_path=item.artwork_path or self.artwork_path,
                reference_at=item.reference_at or self.reference_at,
                notes="\n".join(filter(None, [self.notes, item.notes])),
            )
            for item in self.items
        ]


class CompletionChanges(BaseModel):
    """Only missing order facts can be completed; routing and identity are immutable."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    customer_name: str | None = Field(default=None, min_length=1, max_length=160)
    customer_contact: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)
    dimensions: str | None = Field(default=None, min_length=1, max_length=200)
    artwork_path: str | None = Field(default=None, min_length=1, max_length=600)
    reference_at: AwareDatetime | None = None


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1)
    changes: CompletionChanges


class FollowupRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=4000)


class ProductionConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checked: Literal[True]
