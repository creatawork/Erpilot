"""Shared per-SKU pricing; decimal arithmetic with rounded settlement units."""

from decimal import ROUND_HALF_UP, Decimal

# Descending quantity thresholds; applied after merging lines of the same SKU.
QUOTE_TIERS: tuple[tuple[int, float], ...] = ((200, 0.90), (50, 0.95), (10, 0.98))
_CENT = Decimal("0.01")


class PricingError(ValueError):
    """The current product price cannot produce a positive settlement unit."""


def price_for_quantity(price: float, quantity: int) -> tuple[float, float, float]:
    """Return discount, rounded unit price and total for a positive quantity."""
    if quantity < 1:
        raise ValueError("quantity must be positive")
    discount = next((d for threshold, d in QUOTE_TIERS if quantity >= threshold), 1.0)
    unit = (Decimal(str(price)) * Decimal(str(discount))).quantize(
        _CENT, rounding=ROUND_HALF_UP,
    )
    if unit <= 0:
        raise PricingError("折后单价舍入后须至少为 0.01 元")
    return discount, float(unit), float(unit * quantity)
