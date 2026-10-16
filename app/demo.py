"""Deterministic deadline examples; this module never accesses Trello."""

from datetime import datetime, time, timedelta

from app.deadlines import SAO_PAULO, calculate_deadline, is_business_day
from app.rules import get_product_rule
from app.settings import Settings
from app.storage import utc_now


def cutoff_example(product_code: str, settings: Settings) -> dict:
    rule = get_product_rule(product_code)
    day = utc_now().astimezone(SAO_PAULO).date()
    while not is_business_day(day, settings.holidays):
        day += timedelta(days=1)
    examples = [
        calculate_deadline(
            datetime.combine(day, clock, SAO_PAULO), rule.lead_time_business_days, settings.holidays
        )
        for clock in (time(15, 59), time(16))
    ]
    return {
        "product": rule.name,
        "date": day.isoformat(),
        "business_days": rule.lead_time_business_days,
        "before": examples[0].model_dump(mode="json"),
        "after": examples[1].model_dump(mode="json"),
    }
