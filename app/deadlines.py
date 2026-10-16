from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

SAO_PAULO = ZoneInfo("America/Sao_Paulo")
CUTOFF = time(16)


class Deadline(BaseModel):
    reference_local: datetime
    effective_date: date
    due_at: datetime
    business_days: int
    cutoff_applied: bool
    explanation: str


def is_business_day(day: date, holidays: frozenset[date]) -> bool:
    return day.weekday() < 5 and day not in holidays


def calculate_deadline(
    reference_at: datetime,
    days: int,
    holidays: frozenset[date] = frozenset(),
    *,
    event_label: str = "Arte aprovada",
) -> Deadline:
    """Pure calculation from artwork approval or the time missing details become complete."""
    if reference_at.tzinfo is None or reference_at.utcoffset() is None:
        raise ValueError("A data precisa incluir o fuso horário.")
    if days < 1:
        raise ValueError("O prazo deve ter pelo menos um dia útil.")
    local = reference_at.astimezone(SAO_PAULO)
    cutoff_applied = local.time() >= CUTOFF
    effective = local.date() + timedelta(days=int(cutoff_applied))
    while not is_business_day(effective, holidays):
        effective += timedelta(days=1)
    due = effective
    remaining = days
    while remaining:
        due += timedelta(days=1)
        if is_business_day(due, holidays):
            remaining -= 1
    explanation = (
        f"{event_label} em {local:%d/%m/%Y às %H:%M}. "
        + ("Corte das 16h aplicado. " if cutoff_applied else "Antes do corte das 16h. ")
        + f"Data-base: {effective:%d/%m/%Y}; +{days} dia(s) útil(eis). "
        "Vencimento às 18h (convenção da POC)."
    )
    return Deadline(
        reference_local=local,
        effective_date=effective,
        due_at=datetime.combine(due, time(18), SAO_PAULO),
        business_days=days,
        cutoff_applied=cutoff_applied,
        explanation=explanation,
    )
