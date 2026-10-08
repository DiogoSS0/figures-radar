from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

import requests

from .collectors import HLJCollector, NinNinCollector
from .collectors.base import CollectorError, DealCollector, RawProduct, USER_AGENT

LOG = logging.getLogger(__name__)
FIGURE_WORDS = ("figure", "figuarts", "figma", "mafex", "nendoroid", "statue", "pop up parade", "revoltech", "prize")
EXCLUDE_WORDS = ("plush", "keychain", "key ring", "acrylic stand", "trading card", "poster", "t-shirt", "shirt", "jacket", "pillow", "model kit", "plastic model", "outfit", "accessory", "face plate", "clothing set", "oyoufuku set", "doll clothes", "replacement part", "body parts")
SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_products (
  product_identity TEXT NOT NULL, retailer TEXT NOT NULL, currency TEXT NOT NULL,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  PRIMARY KEY(product_identity, retailer, currency)
);
CREATE TABLE IF NOT EXISTS price_observations (
  product_identity TEXT NOT NULL, retailer TEXT NOT NULL, currency TEXT NOT NULL,
  price TEXT NOT NULL, stock_status TEXT, observed_at TEXT NOT NULL,
  PRIMARY KEY(product_identity, retailer, currency, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_observations_lookup ON price_observations(product_identity, retailer, currency, observed_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def slug(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").casefold()).strip("-")


def product_identity(product: RawProduct) -> str:
    if product.jan_code:
        return "jan:" + slug(product.jan_code)
    if product.sku:
        return "sku:" + slug(product.manufacturer) + ":" + slug(product.sku)
    if product.manufacturer_product_id:
        return "maker:" + slug(product.manufacturer) + ":" + slug(product.manufacturer_product_id)
    if product.retailer_product_id:
        return "retailer:" + slug(product.retailer) + ":" + slug(product.retailer_product_id)
    # A full title preserves variant words when a retailer supplies no strong ID.
    return "title:" + ":".join(slug(x) for x in (
        product.manufacturer, product.character, product.series, product.scale,
        product.figure_type, product.version, product.figure_name))


@dataclass(frozen=True)
class NormalizedProduct:
    retailer: str
    retailer_product_id: str | None
    product_identity: str
    product_url: str
    figure_name: str
    character: str | None
    series: str | None
    manufacturer: str | None
    scale: str | None
    figure_type: str | None
    sku: str | None
    jan_code: str | None
    manufacturer_product_id: str | None
    version: str | None
    currency: str
    regular_price: Decimal | None
    current_price: Decimal
    discount_percent: Decimal | None
    stock_status: str
    availability_type: str
    image_url: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    last_verified_at: datetime
    source: str
    source_confidence: str
    first_observed_price: Decimal | None = None
    historical_minimum: Decimal | None = None
    median_7d: Decimal | None = None
    median_30d: Decimal | None = None
    median_90d: Decimal | None = None
    historical_confidence: str = "LOW"
    deal_score: int = 0
    deal_quality: str = "IGNORE"
    score_reason: str = ""
    image_status: str = "UNVERIFIED"


def valid_https(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def normalize(raw: RawProduct, now: datetime | None = None) -> NormalizedProduct | None:
    now = now or utc_now()
    title = raw.figure_name.strip()
    lower = title.casefold()
    has_scale = bool(re.search(r"\b1/(?:[1-9]|1[02])\b", lower))
    if not valid_https(raw.product_url) or not title or not (any(w in lower for w in FIGURE_WORDS) or has_scale) or any(w in lower for w in EXCLUDE_WORDS):
        return None
    price = raw.current_price
    if price is None or price <= 0 or not raw.currency or not re.fullmatch(r"[A-Z]{3}", raw.currency):
        return None
    regular = raw.regular_price
    if regular is not None and (regular <= 0 or regular < price):
        return None
    status = (raw.stock_status or "UNKNOWN").upper()
    if status in {"OUT_OF_STOCK", "SOLD_OUT", "DISCONTINUED"}:
        return None
    availability = status if status in {"IN_STOCK", "PREORDER", "BACKORDER"} else "UNKNOWN"
    discount = ((regular - price) / regular * 100).quantize(Decimal("0.01")) if regular else None
    image = raw.image_url if valid_https(raw.image_url) else None
    return NormalizedProduct(
        retailer=raw.retailer, retailer_product_id=raw.retailer_product_id,
        product_identity=product_identity(raw), product_url=raw.product_url, figure_name=title,
        character=raw.character, series=raw.series, manufacturer=raw.manufacturer,
        scale=raw.scale, figure_type=raw.figure_type, sku=raw.sku, jan_code=raw.jan_code,
        manufacturer_product_id=raw.manufacturer_product_id, version=raw.version,
        currency=raw.currency, regular_price=regular, current_price=price,
        discount_percent=discount, stock_status=status, availability_type=availability,
        image_url=image, first_seen_at=now, last_seen_at=now, last_verified_at=now,
        source=raw.source, source_confidence=raw.source_confidence,
    )


class PriceHistory:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def enrich_and_record(self, product: NormalizedProduct, now: datetime) -> NormalizedProduct:
        key = (product.product_identity, product.retailer, product.currency)
        first = self.connection.execute(
            "SELECT first_seen_at FROM discovery_products WHERE product_identity=? AND retailer=? AND currency=?", key).fetchone()
        rows = self.connection.execute(
            "SELECT price, observed_at FROM price_observations WHERE product_identity=? AND retailer=? AND currency=? ORDER BY observed_at", key).fetchall()
        observations = [(Decimal(p), datetime.fromisoformat(t)) for p, t in rows]
        def window(days: int) -> Decimal | None:
            values = [p for p, t in observations if t >= now - timedelta(days=days)]
            return Decimal(str(median(values))) if values else None
        m7, m30, m90 = window(7), window(30), window(90)
        historical_min = min([product.current_price] + [p for p, _ in observations])
        enriched = replace(product,
            first_seen_at=datetime.fromisoformat(first[0]) if first else now,
            first_observed_price=observations[0][0] if observations else product.current_price,
            historical_minimum=historical_min,
            median_7d=m7, median_30d=m30, median_90d=m90,
            historical_confidence="HIGH" if len(observations) >= 5 and observations[-1][1] - observations[0][1] >= timedelta(days=7) else "LOW")
        with self.connection:
            self.connection.execute(
                "INSERT INTO discovery_products VALUES (?,?,?,?,?) ON CONFLICT(product_identity,retailer,currency) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (*key, first[0] if first else now.isoformat(), now.isoformat()))
            self.connection.execute("INSERT INTO price_observations VALUES (?,?,?,?,?,?)",
                                    (*key, str(product.current_price), product.stock_status, now.isoformat()))
        return enriched


def quality(score: int) -> str:
    return "EXCEPTIONAL" if score >= 90 else "HOT" if score >= 80 else "GOOD" if score >= 70 else "FAIR" if score >= 60 else "IGNORE"


def score_deal(product: NormalizedProduct, now: datetime | None = None) -> NormalizedProduct:
    now = now or utc_now()
    age_days = max(0.0, (now - product.first_seen_at).total_seconds() / 86400)
    freshness = max(0, round(10 * (1 - age_days / 30)))
    confidence = (4 if product.jan_code or product.sku or product.manufacturer_product_id or product.retailer_product_id else 0) + (
        2 if product.availability_type != "UNKNOWN" else 0) + (2 if product.image_url else 0) + (
        2 if product.source_confidence == "HIGH" else 1)
    advertised = float(product.discount_percent or 0)
    baseline = product.median_30d
    if baseline is not None and product.historical_confidence == "HIGH":
        historical_drop = max(0.0, float((baseline - product.current_price) / baseline * 100))
        historical_points = min(45, round(historical_drop * 1.5))
        advertised_points = min(20, round(advertised * 0.4))
        low_bonus = 0
        if product.historical_minimum and product.current_price <= product.historical_minimum:
            low_bonus = 15 if product.historical_confidence == "HIGH" else 5
        elif product.historical_minimum and product.current_price <= product.historical_minimum * Decimal("1.05"):
            low_bonus = 8 if product.historical_confidence == "HIGH" else 3
        score = min(100, historical_points + advertised_points + low_bonus + freshness + confidence)
        reason = f"historical drop {historical_drop:.1f}% vs 30d median; advertised {advertised:.1f}%; history {product.historical_confidence}"
    else:
        # Cold start is eligible only for an unusually large, explicitly priced reduction.
        score = min(79, min(60, round(advertised * 1.2)) + freshness + confidence)
        reason = f"cold start: advertised {advertised:.1f}%; insufficient history; history LOW"
    return replace(product, deal_score=score, deal_quality=quality(score), score_reason=reason)


def verify_image(product: NormalizedProduct, *, session: requests.Session | None = None) -> NormalizedProduct:
    if not product.image_url or not valid_https(product.image_url):
        return replace(product, image_status="MISSING")
    # URL is taken from the retailer's own product card or official page. Check the response only.
    try:
        response = (session or requests.Session()).head(product.image_url, timeout=8, allow_redirects=False,
                                                          headers={"User-Agent": USER_AGENT})
        status = "VERIFIED" if response.status_code == 200 and response.headers.get("Content-Type", "").lower().startswith("image/") else "UNVERIFIED"
    except requests.RequestException:
        status = "UNVERIFIED"
    return replace(product, image_status=status)


def serialize(product: NormalizedProduct) -> dict:
    result = asdict(product)
    for key, value in result.items():
        if isinstance(value, (datetime, Decimal)):
            result[key] = str(value) if isinstance(value, Decimal) else value.isoformat()
    return result


def run_discovery(root: Path, collectors: list[DealCollector] | None = None,
                  *, now: datetime | None = None, verify_images: bool = True) -> dict:
    now = now or utc_now()
    collectors = collectors if collectors is not None else [NinNinCollector(), HLJCollector()]
    raw_products: list[RawProduct] = []
    sources = []
    for collector in collectors:
        try:
            found = collector.discover()
            raw_products.extend(found)
            sources.append({"retailer": collector.retailer, "status": "OK", "products": len(found)})
        except Exception as exc:
            code = re.search(r"HTTP (403|429|5\d\d)", str(exc))
            reason = code.group(0) if code else type(exc).__name__
            LOG.warning("Collector %s failed: %s", collector.retailer, reason)
            sources.append({"retailer": collector.retailer, "status": "FAILED", "error": reason, "products": 0})
    normalized = []
    seen = set()
    for raw in raw_products:
        item = normalize(raw, now)
        if item is None:
            continue
        key = (item.product_identity, item.retailer, item.currency)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(item)
    history = PriceHistory(root / "data" / "runtime" / "price-history.sqlite3")
    try:
        scored = [score_deal(history.enrich_and_record(item, now), now) for item in normalized]
    finally:
        history.close()
    scored.sort(key=lambda p: (-p.deal_score, p.retailer, p.figure_name))
    if verify_images:
        scored[:10] = [verify_image(item) for item in scored[:10]]
    result = {
        "generated_at": now.isoformat(), "sources_queried": len(collectors),
        "sources": sources, "products_discovered": len(raw_products),
        "normalized": len(normalized),
        "in_stock_or_preorder": sum(p.availability_type in {"IN_STOCK", "PREORDER", "BACKORDER"} for p in normalized),
        "deals_at_least_70": sum(p.deal_score >= 70 for p in scored),
        "deals": [serialize(p) for p in scored], "buffer_writes": 0, "x_posts": 0,
    }
    output = root / "data" / "runtime" / "latest-deals.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Discover figure deals without publishing")
    parser.add_argument("--no-image-check", action="store_true", help="Skip image HEAD checks")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    result = run_discovery(root, verify_images=not args.no_image_check)
    print(f"Sources queried: {result['sources_queried']}")
    for source in result["sources"]:
        print(f"  {source['retailer']}: {source['status']} ({source['products']})")
    print(f"Products discovered: {result['products_discovered']}")
    print(f"Normalized: {result['normalized']}")
    print(f"In stock/preorder: {result['in_stock_or_preorder']}")
    print(f"Deals >= 70: {result['deals_at_least_70']}")
    print("TOP DEALS")
    for i, deal in enumerate(result["deals"][:10], 1):
        print(f"{i}. {deal['figure_name']} | {deal['retailer']} | {deal['current_price']} {deal['currency']} | "
              f"{deal['discount_percent']}% | {deal['deal_score']} {deal['deal_quality']} | {deal['stock_status']} | "
              f"{deal['product_url']} | image {deal['image_status']} | {deal['score_reason']}")
    print("Buffer writes: 0 | X posts: 0")
    return 0
