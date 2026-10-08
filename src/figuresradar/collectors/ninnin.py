from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .base import CollectorError, DealCollector, HttpClient, RawProduct

SALE_URL = "https://www.nin-nin-game.com/en/prices-drop"
FIGURE_SALE_URLS = (
    "https://www.nin-nin-game.com/en/scale-figure-sales",
    "https://www.nin-nin-game.com/en/nendoroid-sales",
    "https://www.nin-nin-game.com/en/figma-sales",
)


def parse_price(text: str) -> tuple[Decimal | None, str | None]:
    currency = "EUR" if "€" in text else "USD" if "$" in text else "JPY" if "¥" in text else None
    if not currency:
        return None, None
    number = re.sub(r"[^0-9,.]", "", text)
    if not number:
        return None, currency
    if "," in number and "." in number:
        number = number.replace(",", "") if number.rfind(".") > number.rfind(",") else number.replace(".", "").replace(",", ".")
    elif "," in number:
        number = number.replace(",", ".") if len(number.rsplit(",", 1)[1]) <= 2 else number.replace(",", "")
    try:
        return Decimal(number), currency
    except InvalidOperation:
        return None, currency


def parse_listing(html: str, source: str = SALE_URL) -> list[RawProduct]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select("li.general_block_card[data-id]")
    if not cards:
        raise CollectorError("Nin-Nin product cards missing; parser may be stale")
    products = []
    for card in cards:
        link = card.select_one("a.product-name[href]")
        current = card.select_one(".price_container > .price")
        if not link or not current:
            continue
        if urlparse(link["href"]).hostname != "www.nin-nin-game.com":
            continue
        price, currency = parse_price(current.get_text(" ", strip=True))
        old = card.select_one(".old_price .stroke")
        regular, regular_currency = parse_price(old.get_text(" ", strip=True)) if old else (None, None)
        if regular_currency and regular_currency != currency:
            continue
        title = link.get_text(" ", strip=True)
        image = card.select_one(".product_image img")
        image_url = image.get("src") if image else None
        if image_url and not (urlparse(image_url).hostname or "").endswith(".nin-nin-game.com"):
            image_url = None
        status = card.select_one(".status-box-title")
        stock = "PREORDER" if status and "PRE-ORDER" in status.get_text(" ", strip=True).upper() else (
            "IN_STOCK" if card.select_one(".ajax_add_to_cart_button") else "OUT_OF_STOCK")
        manufacturer = re.search(r"\[([^\[\]]+)\]\s*$", title)
        scale = re.search(r"\b1/(?:[1-9]|1[02])\b", title)
        products.append(RawProduct(
            retailer="Nin-Nin-Game", retailer_product_id=card.get("data-id"),
            product_url=link["href"], figure_name=title, regular_price=regular,
            current_price=price, currency=currency, stock_status=stock,
            image_url=image_url, source=source,
            source_confidence="HIGH", manufacturer=manufacturer.group(1) if manufacturer else None,
            scale=scale.group(0) if scale else None,
        ))
    return products


class NinNinCollector(DealCollector):
    retailer = "Nin-Nin-Game"

    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient()

    def discover(self) -> list[RawProduct]:
        products = []
        for url in FIGURE_SALE_URLS:
            products.extend(parse_listing(self.http.get(url), url))
        return products
