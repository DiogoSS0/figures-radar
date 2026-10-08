from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

LOG = logging.getLogger(__name__)
USER_AGENT = "FiguresRadar/0.1 (+https://github.com/DiogoSS0/figures-radar; contact via GitHub)"


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class RawProduct:
    retailer: str
    retailer_product_id: str | None
    product_url: str
    figure_name: str
    regular_price: Decimal | None
    current_price: Decimal | None
    currency: str | None
    stock_status: str | None
    image_url: str | None
    source: str
    source_confidence: str = "MEDIUM"
    character: str | None = None
    series: str | None = None
    manufacturer: str | None = None
    scale: str | None = None
    figure_type: str | None = None
    sku: str | None = None
    jan_code: str | None = None
    manufacturer_product_id: str | None = None
    version: str | None = None


class HttpClient:
    def __init__(self, *, min_interval: float = 1.5, timeout: float = 12.0,
                 session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.min_interval = min_interval
        self.timeout = timeout
        self.last_request = 0.0
        self.robots: dict[str, RobotFileParser] = {}

    def _request(self, url: str, *, method: str = "GET") -> requests.Response:
        delay = self.min_interval - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        return self.session.request(method, url, timeout=self.timeout, allow_redirects=False)

    def _allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self.robots:
            robots_url = f"https://{host}/robots.txt"
            try:
                response = self._request(robots_url)
                if response.status_code != 200:
                    raise CollectorError(f"robots.txt HTTP {response.status_code} for {host}")
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                self.robots[host] = parser
            except requests.RequestException as exc:
                raise CollectorError(f"robots.txt unavailable for {host}: {type(exc).__name__}") from exc
        return self.robots[host].can_fetch(USER_AGENT, url)

    def get(self, url: str) -> str:
        if not self._allowed(url):
            raise CollectorError(f"robots.txt disallows {url}")
        for attempt in range(2):
            try:
                response = self._request(url)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 0:
                    continue
                raise CollectorError(f"Network error at {url}: {type(exc).__name__}") from exc
            if response.status_code in (403, 429):
                raise CollectorError(f"HTTP {response.status_code} at {url}; stopped")
            if response.status_code in (500, 502, 503, 504) and attempt == 0:
                continue
            if response.status_code != 200:
                raise CollectorError(f"HTTP {response.status_code} at {url}")
            if "html" not in response.headers.get("Content-Type", "").lower():
                raise CollectorError(f"Unexpected content type at {url}")
            return response.text
        raise CollectorError(f"HTTP 5xx at {url}")


class DealCollector:
    retailer: str

    def discover(self) -> list[RawProduct]:
        raise NotImplementedError
