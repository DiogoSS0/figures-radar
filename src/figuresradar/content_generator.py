from __future__ import annotations

from .models import Deal


def generate_text(deal: Deal) -> str:
    symbol = "€" if deal.currency.upper() == "EUR" else f"{deal.currency.upper()} "
    text = (
        f"🔥 FIGURE DEAL\n\n{deal.figure_name}\n\n"
        f"{deal.character} from {deal.series}.\n\n"
        f"{symbol}{deal.normal_price:.2f} → {symbol}{deal.current_price:.2f}\n"
        f"{deal.discount_percent:.0f}% OFF\n\n"
        f"{deal.product_url}\n\nPowered by NekoPrice."
    )
    if len(text) > 280:
        raise ValueError(f"Post exceeds 280 characters ({len(text)})")
    return text
