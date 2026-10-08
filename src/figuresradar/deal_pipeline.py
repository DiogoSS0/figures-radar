from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Iterable

from .buffer_client import BufferQueueSnapshot
from .config import needed_posts
from .content_generator import generate_text
from .deduplication import is_material_new_offer, offer_identity, product_identity
from .image_generator import prepare_image
from .models import Deal, PreparedPost
from .persistence import DealStore


def load_fixture(path: Path) -> list[Deal]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    deals = []
    for item in raw:
        item = dict(item)
        for key in ("normal_price", "current_price", "discount_percent", "deal_score"):
            item[key] = Decimal(str(item[key]))
        for key in ("detected_at", "verified_at", "posted_at"):
            if item.get(key):
                item[key] = datetime.fromisoformat(item[key].replace("Z", "+00:00"))
        deals.append(Deal(**item))
    return deals


def fixture_revalidator(deal: Deal) -> bool:
    """Future retailer adapters can replace this check before queuing."""
    return deal.verified_at is not None and deal.stock_status == "in_stock"


@dataclass(frozen=True)
class PipelineResult:
    pending_before: int
    needed: int
    prepared: tuple[PreparedPost, ...]
    skipped: tuple[tuple[str, str], ...]
    errors: tuple[tuple[str, str], ...]


def prepare_posts(
    deals: Iterable[Deal],
    snapshot: BufferQueueSnapshot,
    store: DealStore,
    project_root: Path,
    *,
    content_generator: Callable[[Deal], str] = generate_text,
    revalidator: Callable[[Deal], bool] = fixture_revalidator,
) -> PipelineResult:
    needed = needed_posts(snapshot.pending_count)
    prepared: list[PreparedPost] = []
    skipped: list[tuple[str, str]] = []
    errors: list[tuple[str, str]] = []
    seen_offers: set[str] = set()
    seen_prices: dict[str, list[Decimal]] = {}
    pending_texts = {str(post.get("text") or "") for post in snapshot.posts}
    ranked = sorted(deals, key=lambda d: (d.deal_score, d.detected_at), reverse=True)
    for deal in ranked:
        if len(prepared) >= needed:
            break
        try:
            if not revalidator(deal):
                skipped.append((deal.unique_id, "not_verified_or_out_of_stock"))
                continue
            offer = offer_identity(deal)
            product = product_identity(deal)
            if offer in seen_offers or store.has_offer(deal):
                skipped.append((deal.unique_id, "duplicate_offer"))
                continue
            prior = store.prior_prices(deal) + seen_prices.get(product, [])
            if not is_material_new_offer(deal, prior):
                skipped.append((deal.unique_id, "no_material_price_drop"))
                continue
            text = content_generator(deal)
            if text in pending_texts:
                skipped.append((deal.unique_id, "already_in_buffer_queue"))
                continue
            image_url, test_image_path = prepare_image(deal, project_root)
            prepared.append(PreparedPost(deal, text, image_url, datetime.now(timezone.utc), test_image_path))
            seen_offers.add(offer)
            seen_prices.setdefault(product, []).append(deal.current_price)
        except (ValueError, OSError, RuntimeError) as exc:
            # Failure is isolated to this candidate. Never write a scheduled marker here.
            errors.append((deal.unique_id, type(exc).__name__))
    return PipelineResult(snapshot.pending_count, needed, tuple(prepared), tuple(skipped), tuple(errors))
