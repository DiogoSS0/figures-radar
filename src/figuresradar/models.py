from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True)
class Deal:
    unique_id: str
    figure_name: str
    character: str
    series: str
    manufacturer: str
    retailer: str
    product_url: str
    image_url: str
    normal_price: Decimal
    current_price: Decimal
    currency: str
    discount_percent: Decimal
    deal_score: Decimal
    detected_at: datetime
    verified_at: datetime | None
    stock_status: str
    source: str
    posted_at: datetime | None = None
    buffer_id: str | None = None

    def __post_init__(self) -> None:
        if not self.unique_id or not self.figure_name or not self.retailer:
            raise ValueError("Deal needs unique_id, figure_name and retailer")
        if self.normal_price <= 0 or self.current_price <= 0:
            raise ValueError("Prices must be positive")
        if self.current_price >= self.normal_price:
            raise ValueError("A test deal must have a real discount")
        if not 0 <= self.discount_percent <= 100:
            raise ValueError("Invalid discount_percent")


@dataclass(frozen=True)
class PreparedPost:
    deal: Deal
    text: str
    image_url: str
    prepared_at: datetime
    test_image_path: str | None = None
