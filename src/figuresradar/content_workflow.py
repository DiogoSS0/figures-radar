"""Read-only Buffer queue + real discovery snapshot -> local post previews."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from PIL import Image, ImageOps

from .buffer_client import BufferQueueReader, BufferQueueSnapshot, add_to_queue_payload
from .card_renderer import download_product_image, render_card
from .character_enrichment import CharacterEnricher
from .config import Config, TARGET_QUEUE_SIZE, needed_posts
from .deduplication import product_key_for
from .persistence import DealStore
from .reservations import ReservationStore

AVAILABLE = {"IN_STOCK", "PREORDER", "BACKORDER"}
MAX_SNAPSHOT_AGE = timedelta(hours=24)
HEADINGS = ("🔥 FIGURE DEAL", "👀 DEAL FOUND", "⚡ DEAL ALERT", "💸 DISCOUNT FOUND")


def revalidate(deal: dict, latest: dict | None = None) -> str:
    """Comparison interface for a future live retailer check; no network call here."""
    if latest is None:
        return "UNKNOWN"
    if latest.get("stock_status") not in AVAILABLE:
        return "OUT_OF_STOCK" if latest.get("stock_status") in {"OUT_OF_STOCK", "SOLD_OUT"} else "UNAVAILABLE"
    if latest.get("currency") != deal.get("currency"):
        return "UNKNOWN"
    try:
        if Decimal(latest["current_price"]) != Decimal(deal["current_price"]):
            return "PRICE_CHANGED"
    except (KeyError, ValueError):
        return "UNKNOWN"
    return "VALID"


def _money(value: Decimal, currency: str) -> str:
    return ("€" if currency == "EUR" else "$" if currency == "USD" else "¥" if currency == "JPY" else currency + " ") + (
        f"{value:,.0f}" if currency == "JPY" else f"{value:,.2f}")


def x_weighted_length(text: str) -> int:
    """Conservative count: URLs consume 23 characters and non-BMP emoji two."""
    without_urls = re.sub(r"https://\S+", "X" * 23, text)
    return sum(2 if ord(char) > 0xFFFF else 1 for char in without_urls)


def _short_title(title: str, limit: int = 72) -> str:
    if len(title) <= limit:
        return title
    return title[: limit - 1].rsplit(" ", 1)[0] + "…"


def generate_copy(deal: dict, *, enrichment: CharacterEnricher | None = None,
                  cta_url: str | None = None) -> tuple[str, dict]:
    enricher = enrichment or CharacterEnricher()
    enriched = enricher.enrich(deal.get("character"), deal.get("series"))
    if cta_url is not None:
        parsed = urlsplit(cta_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("NEKOPRICE_CTA_URL must be an explicit HTTPS URL")
    heading = HEADINGS[int(hashlib.sha256(deal["product_identity"].encode()).hexdigest(), 16) % len(HEADINGS)]
    regular = deal.get("regular_price")
    current = _money(Decimal(deal["current_price"]), deal["currency"])
    if regular is not None and deal.get("discount_percent") is not None:
        price_line = f"{_money(Decimal(regular), deal['currency'])} → {current} · {Decimal(deal['discount_percent']):.0f}% OFF"
    else:
        price_line = f"Current price: {current} · Deal detected"
    destination = cta_url or deal["product_url"]
    title = _short_title(deal["figure_name"])
    parts = [heading, title]
    if enriched.short_description:
        parts.append(enriched.short_description)
    retailer_line = ("Found at " if heading.startswith(("🔥", "👀")) else "At ") + deal["retailer"] + "."
    parts.extend([price_line, retailer_line, destination, "Powered by NekoPrice."])
    copy = "\n\n".join(parts[:2]) + "\n\n" + "\n".join(parts[2:])
    if x_weighted_length(copy) > 250:
        parts = [heading, _short_title(deal["figure_name"], 52), price_line,
                 retailer_line, destination, "Powered by NekoPrice."]
        copy = "\n\n".join(parts[:2]) + "\n\n" + "\n".join(parts[2:])
    if x_weighted_length(copy) > 280:
        raise ValueError("Copy exceeds X character limit")
    return copy, vars(enriched)


def _diversity_key(deal: dict) -> tuple[str, ...]:
    name = deal["figure_name"].casefold()
    kind = next((word for word in ("nendoroid", "figma", "figuarts", "mafex", "statue", "figure") if word in name), "other")
    return (deal["retailer"].casefold(), (deal.get("series") or "").casefold(),
            (deal.get("character") or "").casefold(), kind,
            (deal.get("manufacturer") or "").casefold())


def rank_diverse(deals: list[dict]) -> list[dict]:
    remaining = list(deals)
    selected: list[dict] = []
    counts = [Counter() for _ in range(5)]
    while remaining:
        best = max(int(d["deal_score"]) for d in remaining)
        near = [d for d in remaining if int(d["deal_score"]) >= best - 3]
        def key(deal: dict) -> tuple:
            dimensions = _diversity_key(deal)
            penalty = sum(counts[i][value] for i, value in enumerate(dimensions) if value)
            baseline = Decimal(deal["median_30d"]) if deal.get("median_30d") else Decimal(0)
            historical_drop = (baseline - Decimal(deal["current_price"])) / baseline * 100 if baseline > 0 else Decimal(0)
            return (-penalty, int(deal["deal_score"]), deal.get("last_verified_at") or "",
                    historical_drop, Decimal(deal.get("discount_percent") or 0),
                    1 if deal.get("source_confidence") == "HIGH" else 0)
        chosen = max(near, key=key)
        selected.append(chosen)
        for index, value in enumerate(_diversity_key(chosen)):
            if value:
                counts[index][value] += 1
        remaining.remove(chosen)
    return selected


def _reject_reason(deal: dict, now: datetime) -> str | None:
    if int(deal.get("deal_score") or 0) < 70:
        return "score_below_70"
    if deal.get("stock_status") not in AVAILABLE:
        return "not_in_stock"
    try:
        verified = datetime.fromisoformat(deal["last_verified_at"])
        if verified.tzinfo is None or now - verified > MAX_SNAPSHOT_AGE or verified > now + timedelta(minutes=5):
            return "stale_or_invalid_snapshot"
        if Decimal(deal["current_price"]) <= 0:
            return "invalid_price"
    except (KeyError, TypeError, ValueError):
        return "invalid_price_or_timestamp"
    for field in ("image_url", "product_url"):
        parsed = urlsplit(deal.get(field) or "")
        if parsed.scheme != "https" or not parsed.hostname:
            return f"invalid_{field}"
    return None


def _buffer_draft(channel_id: str, copy: str, card: Path, public_card_url: str | None) -> tuple[dict | None, dict]:
    if public_card_url:
        payload = add_to_queue_payload(channel_id, copy, public_card_url)
        return payload, {"status": "READY", "payload": payload}
    return None, {"status": "AWAITING_PUBLIC_CARD_URL", "mode": "addToQueue",
                  "schedulingType": "automatic", "channelId": channel_id,
                  "text": copy, "local_card_path": str(card)}


@dataclass(frozen=True)
class PreparationResult:
    pending: int
    slots: int
    eligible: int
    prepared: tuple[dict, ...]
    rejected: tuple[dict, ...]
    run_dir: Path | None


def prepare_real_posts(deals: list[dict], snapshot: BufferQueueSnapshot, root: Path, *,
                       now: datetime | None = None, channel_id: str = "test-channel",
                       downloader: Callable = download_product_image,
                       renderer: Callable = render_card,
                       enrichment: CharacterEnricher | None = None,
                       cta_url: str | None = None) -> PreparationResult:
    now = now or datetime.now(timezone.utc)
    slots = needed_posts(snapshot.pending_count)
    rejected: list[dict] = []
    eligible: list[dict] = []
    for deal in deals:
        reason = _reject_reason(deal, now)
        if reason:
            rejected.append({"product_identity": deal.get("product_identity"), "title": deal.get("figure_name"), "reason": reason})
        else:
            eligible.append(deal)
    if slots == 0 or not eligible:
        return PreparationResult(snapshot.pending_count, slots, len(eligible), (), tuple(rejected), None)
    run_root = root / "data/runtime/prepared-posts"
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=run_root))
    assets = root / "data/runtime/assets"
    reservations = ReservationStore(root / "data/runtime/preparation.sqlite3")
    ledger = DealStore(root / "data/figuresradar.sqlite3", readonly=True)
    prepared = []
    pending_texts = {str(post.get("text") or "") for post in snapshot.posts}
    try:
        for deal in rank_diverse(eligible):
            if len(prepared) >= slots:
                break
            identity = deal["product_identity"]
            posted = ledger.prior_prices_for_key(product_key_for(deal["retailer"], deal["product_url"]), deal["currency"])
            accepted, key_or_reason = reservations.reserve(deal, posted)
            if not accepted:
                rejected.append({"product_identity": identity, "title": deal["figure_name"], "reason": key_or_reason})
                continue
            key = key_or_reason
            stage: Path | None = None
            try:
                copy, character = generate_copy(deal, enrichment=enrichment, cta_url=cta_url)
                if copy in pending_texts or any(deal["product_url"] in text for text in pending_texts):
                    reservations.set_state(key, "FAILED")
                    rejected.append({"product_identity": identity, "title": deal["figure_name"], "reason": "already_in_buffer_queue"})
                    continue
                asset = downloader(deal["image_url"], assets)
                post_dir = run_dir / f"post-{len(prepared)+1:03d}"
                stage = Path(tempfile.mkdtemp(prefix="building-", dir=run_dir))
                card = post_dir / "card.jpg"
                renderer(deal, asset, stage / "card.jpg")
                payload, draft = _buffer_draft(channel_id, copy, card, None)
                metadata = {
                    "deal_identity": identity, "offer_key": key, "figure_name": deal["figure_name"],
                    "retailer": deal["retailer"],
                    "product_url": deal["product_url"], "price": deal["current_price"],
                    "regular_price": deal.get("regular_price"), "currency": deal["currency"],
                    "discount": deal.get("discount_percent"), "score": deal["deal_score"],
                    "historical_confidence": deal.get("historical_confidence", "LOW"),
                    "character_enrichment": character, "generated_copy": copy,
                    "source_image_url": deal["image_url"], "local_image_path": str(asset),
                    "card_path": str(card), "created_at": now.isoformat(),
                    "buffer_payload": payload, "buffer_payload_draft": draft,
                    "status": "RESERVED", "revalidation": "PENDING_LIVE_CHECK",
                    "buffer_writes": 0, "x_posts": 0,
                }
                (stage / "copy.txt").write_text(copy + "\n", encoding="utf-8")
                (stage / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                stage.rename(post_dir)
                stage = None
                reservations.set_state(key, "RESERVED", post_dir)
                prepared.append(metadata)
            except Exception as exc:
                reservations.set_state(key, "FAILED")
                rejected.append({"product_identity": identity, "title": deal["figure_name"], "reason": type(exc).__name__})
            finally:
                if stage is not None and stage.exists():
                    shutil.rmtree(stage)
        manifest = {"created_at": now.isoformat(), "buffer_pending": snapshot.pending_count,
                    "queue_target": TARGET_QUEUE_SIZE, "slots_available": slots,
                    "eligible_deals": len(eligible), "prepared_posts": len(prepared),
                    "prepared": [str(Path(item["card_path"]).parent) for item in prepared],
                    "rejected": rejected, "buffer_writes": 0, "x_posts": 0}
        (run_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_review_html(run_dir, prepared)
        write_contact_sheet(run_dir, prepared)
    finally:
        ledger.close()
        reservations.close()
    return PreparationResult(snapshot.pending_count, slots, len(eligible), tuple(prepared), tuple(rejected), run_dir)


def write_review_html(run_dir: Path, posts: list[dict]) -> Path:
    cards = []
    for post in posts:
        card = Path(post["card_path"])
        cards.append(f'<article><img src="{html.escape(card.parent.name + "/card.jpg", quote=True)}" alt="Figure deal card">'
                     f'<div><h2>{html.escape(post["retailer"])}</h2><pre>{html.escape(post["generated_copy"])}</pre>'
                     f'<p>Score {post["score"]} · History {html.escape(post["historical_confidence"])}</p>'
                     f'<a href="{html.escape(post["product_url"], quote=True)}">Product page</a></div></article>')
    document = ('<!doctype html><html lang="en"><meta charset="utf-8"><title>FiguresRadar review</title>'
                '<style>body{font:16px system-ui;background:#101b28;color:#f4f4f4;margin:2rem}article{display:grid;'
                'grid-template-columns:minmax(360px,600px) 1fr;gap:2rem;margin:0 0 2rem;padding:1rem;background:#1c2b3c;'
                'border-radius:12px}img{width:100%}pre{white-space:pre-wrap;font:inherit}a{color:#ff906e}</style>'
                '<h1>FiguresRadar · local review</h1>' + ''.join(cards) + '</html>')
    output = run_dir / "review.html"
    output.write_text(document, encoding="utf-8")
    return output


def write_contact_sheet(run_dir: Path, posts: list[dict]) -> Path | None:
    if not posts:
        return None
    rows = (len(posts) + 1) // 2
    sheet = Image.new("RGB", (1200, rows * 338), "#111d2a")
    for index, post in enumerate(posts):
        with Image.open(post["card_path"]) as card:
            tile = ImageOps.fit(card.convert("RGB"), (600, 338), method=Image.Resampling.LANCZOS)
            sheet.paste(tile, ((index % 2) * 600, (index // 2) * 338))
    output = run_dir / "contact-sheet.jpg"
    sheet.save(output, format="JPEG", quality=90, optimize=True)
    return output


def main_prepare() -> int:
    parser = argparse.ArgumentParser(description="Prepare real FiguresRadar deals locally; never publish")
    parser.add_argument("--clear-test-reservations", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    config = Config.from_env(root)
    if not config.dry_run or config.buffer_write_enabled:
        raise RuntimeError("prepare-posts requires DRY_RUN=true and BUFFER_WRITE_ENABLED=false")
    if args.clear_test_reservations:
        store = ReservationStore(root / "data/runtime/preparation.sqlite3")
        try:
            print(f"Cleared dry-run reservations: {store.clear_test_reservations()}")
        finally:
            store.close()
        return 0
    reader = BufferQueueReader(config.buffer_api_key or "", config.buffer_org_id or "", config.buffer_channel_id or "")
    reader.validate_target()
    snapshot = reader.snapshot()
    path = root / "data/runtime/latest-deals.json"
    discovery = json.loads(path.read_text(encoding="utf-8"))
    result = prepare_real_posts(discovery["deals"], snapshot, root, channel_id=config.buffer_channel_id or "",
                                cta_url=os.getenv("NEKOPRICE_CTA_URL") or None)
    print(f"Buffer pending: {result.pending}\nQueue target: {TARGET_QUEUE_SIZE}\nSlots available: {result.slots}\n")
    print(f"Eligible deals: {result.eligible}\nPrepared posts: {len(result.prepared)}\n")
    for number, post in enumerate(result.prepared, 1):
        print(f"{number}. {post['figure_name']} | Score: {post['score']} | Card: {post['card_path']} | Copy: OK")
    print(f"Rejected: {len(result.rejected)}")
    for reason, count in Counter(item["reason"] for item in result.rejected).most_common():
        print(f"  {reason}: {count}")
    print(f"Review: {result.run_dir / 'review.html' if result.run_dir else 'none'}")
    print("Buffer writes: 0\nX posts: 0")
    return 0


def main_review() -> int:
    root = Path(__file__).resolve().parents[2]
    base = root / "data/runtime/prepared-posts"
    runs = [
        (json.loads(path.read_text(encoding="utf-8")), path)
        for path in base.glob("run-*/manifest.json")
    ] if base.exists() else []
    if not runs:
        print("No prepared posts")
        return 0
    runs.sort(key=lambda pair: (pair[0]["created_at"], pair[1].stat().st_mtime))
    manifest, chosen = next((pair for pair in reversed(runs) if pair[0]["prepared_posts"]), runs[-1])
    run = chosen.parent
    print(f"Review: {run / 'review.html'}")
    print(f"Prepared posts: {manifest['prepared_posts']}")
    for post_dir in manifest["prepared"]:
        metadata = json.loads((Path(post_dir) / "metadata.json").read_text(encoding="utf-8"))
        print(f"\n{Path(post_dir).name}: {metadata.get('figure_name', metadata['deal_identity'])}")
        print(f"{metadata['retailer']} | score {metadata['score']} | history {metadata['historical_confidence']}")
        print(f"Price: {metadata['price']} {metadata['currency']} | Card: {metadata['card_path']}\nURL: {metadata['product_url']}")
        print(metadata["generated_copy"])
    return 0
