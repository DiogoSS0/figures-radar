from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import CollectorError, DealCollector, HttpClient, RawProduct
from .ninnin import parse_price

SEARCH_URL = "https://www.hlj.com/search/?GenreCode2=Action+Figures&MacroType2=Action+Figures&Page=1&Sort=std+desc&itemGroup=AUTUMNSALE2026"


def parse_listing(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(".search-widget-block .product-item-name a[href]")
    if not cards:
        raise CollectorError("HLJ product cards missing; parser may be stale")
    result = [(a.get_text(" ", strip=True), urljoin("https://www.hlj.com", a["href"])) for a in cards]
    return [(name, url) for name, url in result if urlparse(url).hostname == "www.hlj.com"]


def parse_product(html: str, url: str) -> RawProduct:
    soup = BeautifulSoup(html, "html.parser")
    name = soup.select_one("h2.page-title")
    price_node = soup.select_one("p.price.product-margin")
    if not name or not price_node:
        raise CollectorError(f"HLJ product fields missing at {url}")
    price, currency = parse_price(price_node.get_text(" ", strip=True))
    status_node = soup.select_one("p.product-stock")
    status_text = " ".join(status_node.get_text(" ", strip=True).lower().split()) if status_node else ""
    status = "IN_STOCK" if "in stock" in status_text else "PREORDER" if "preorder" in status_text else "OUT_OF_STOCK" if "out of stock" in status_text else "UNKNOWN"
    page_text = soup.get_text(" ", strip=True)
    jan = re.search(r"JAN Code:\s*(\d{8,14})", page_text)
    code = re.search(r"Code:\s*([A-Za-z0-9-]+)", page_text)
    maker = re.search(r"Manufacturer:\s*(.+?)\s+(?:Item Size|Storage Fee|Release Date|Series:)", page_text)
    image = soup.select_one('meta[property="og:image"]')
    image_url = image.get("content") if image else None
    if image_url and urlparse(image_url).hostname != "www.hlj.com":
        image_url = None
    scale = re.search(r"\b1/(?:[1-9]|1[02])\b", name.get_text(" ", strip=True))
    return RawProduct(
        retailer="HobbyLink Japan", retailer_product_id=code.group(1) if code else None,
        product_url=url, figure_name=name.get_text(" ", strip=True), regular_price=None,
        current_price=price, currency=currency, stock_status=status,
        image_url=image_url, source=SEARCH_URL,
        source_confidence="MEDIUM", jan_code=jan.group(1) if jan else None,
        sku=code.group(1) if code else None, manufacturer=maker.group(1).strip() if maker else None,
        scale=scale.group(0) if scale else None,
    )


class HLJCollector(DealCollector):
    retailer = "HobbyLink Japan"

    def __init__(self, http: HttpClient | None = None, *, max_details: int = 8) -> None:
        self.http = http or HttpClient()
        self.max_details = max_details

    def discover(self) -> list[RawProduct]:
        products = []
        for name, url in parse_listing(self.http.get(SEARCH_URL)):
            if not any(word in name.lower() for word in ("figure", "figuarts", "figma", "mafex", "nendoroid", "statue")):
                continue
            try:
                products.append(parse_product(self.http.get(url), url))
            except CollectorError as exc:
                if "HTTP 403" in str(exc) or "HTTP 429" in str(exc):
                    raise
                continue
            if len(products) >= self.max_details:
                break
        return products
