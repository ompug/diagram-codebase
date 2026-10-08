"""Reorder forecasting."""

from app.models import Item

SAFETY_STOCK = 5


def reorder_quantity(item: Item, daily_demand: float = 2.0, lead_days: int = 7) -> int:
    """Quantity needed to cover lead-time demand plus safety stock."""
    target = _lead_time_demand(daily_demand, lead_days) + SAFETY_STOCK
    return max(0, int(target) - item.qty)


def _lead_time_demand(daily_demand: float, lead_days: int) -> float:
    return daily_demand * lead_days
