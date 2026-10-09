from __future__ import annotations

import hashlib
import json
from datetime import timezone
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit

from .models import Deal, PreparedPost

MIN_MATERIAL_DROP = Decimal("0.05")


def product_identity(deal: Deal) -> str:
    return product_key_for(deal.retailer, deal.product_url)


def product_key_for(retailer: str, product_url: str) -> str:
    parsed = urlsplit(product_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Product URL must use HTTPS")
    canonical_url = urlunsplit(("https", parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))
    return hashlib.sha256(json.dumps([retailer.strip().casefold(), canonical_url]).encode()).hexdigest()


def offer_identity(deal: Deal) -> str:
    payload = [product_identity(deal), deal.currency.upper(), str(deal.current_price.quantize(Decimal("0.01")))]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def is_material_new_offer(deal: Deal, previous_prices: list[Decimal]) -> bool:
    if not previous_prices:
        return True
    best_previous = min(previous_prices)
    return deal.current_price <= best_previous * (1 - MIN_MATERIAL_DROP)


def preparation_fingerprint(post: PreparedPost) -> str:
    if post.prepared_at.tzinfo is None:
        raise ValueError("Preparation timestamp must include a timezone")
    payload = [
        post.deal.unique_id,
        post.text,
        post.image_url,
        post.prepared_at.astimezone(timezone.utc).isoformat(),
    ]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
