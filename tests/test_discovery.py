from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import requests

from figuresradar.collectors.base import CollectorError, HttpClient, RawProduct
from figuresradar.collectors.ninnin import parse_listing, parse_price
from figuresradar.discovery import PriceHistory, normalize, product_identity, run_discovery, score_deal

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def product(**changes) -> RawProduct:
    base = dict(retailer="Shop", retailer_product_id="123", product_url="https://shop.example/figure/123",
                figure_name="Rem Nendoroid Wedding Ver.", regular_price=Decimal("100"),
                current_price=Decimal("50"), currency="EUR", stock_status="IN_STOCK",
                image_url="https://shop.example/image/123.jpg", source="https://shop.example/sale",
                source_confidence="HIGH", manufacturer="Good Smile Company")
    base.update(changes)
    return RawProduct(**base)


class DiscoveryTests(unittest.TestCase):
    def test_price_normalization_and_currencies(self):
        self.assertEqual(parse_price("1.234,50 €"), (Decimal("1234.50"), "EUR"))
        self.assertEqual(parse_price("US$1,234.50"), (Decimal("1234.50"), "USD"))
        self.assertEqual(parse_price("¥5,000"), (Decimal("5000"), "JPY"))

    def test_missing_regular_price_and_cold_start(self):
        item = normalize(product(regular_price=None), NOW)
        self.assertIsNotNone(item)
        self.assertIsNone(item.discount_percent)
        scored = score_deal(item, NOW)
        self.assertEqual(scored.historical_confidence, "LOW")
        self.assertLess(scored.deal_score, 70)

    def test_out_of_stock_and_bad_price_and_bad_url(self):
        unavailable = normalize(product(stock_status="SOLD_OUT"), NOW)
        self.assertEqual(unavailable.stock_status, "OUT_OF_STOCK")
        self.assertEqual(score_deal(unavailable, NOW).deal_score, 0)
        self.assertIsNone(normalize(product(current_price=Decimal("0")), NOW))
        self.assertIsNone(normalize(product(product_url="http://shop.example/figure"), NOW))
        self.assertIsNone(normalize(product(regular_price=Decimal("40")), NOW))

    def test_preorder_is_accepted(self):
        item = normalize(product(stock_status="PREORDER"), NOW)
        self.assertEqual(item.availability_type, "PREORDER")

    def test_merchandise_is_rejected(self):
        self.assertIsNone(normalize(product(figure_name="Nendoroid Doll Customizable Face Plate"), NOW))
        self.assertIsNone(normalize(product(figure_name="Rem 1/7 Plastic Model"), NOW))

    def test_identity_strong_and_variants(self):
        a = product(retailer_product_id=None, jan_code="4581234567890")
        b = product(retailer_product_id="another", jan_code="4581234567890")
        self.assertEqual(product_identity(a), product_identity(b))
        self.assertNotEqual(product_identity(product(retailer_product_id=None, figure_name="Rem Nendoroid")),
                            product_identity(product(retailer_product_id=None, figure_name="Rem Bunny 1/4")))

    def test_fallback_identity_uses_variant_fields(self):
        a = product(retailer_product_id=None, figure_name="Rem", version="Wedding Ver.")
        b = product(retailer_product_id=None, figure_name="Rem", version="Bunny Ver.")
        self.assertNotEqual(product_identity(a), product_identity(b))
        maker_a = product(retailer_product_id=None, manufacturer_product_id="A-01")
        maker_b = product(retailer_product_id=None, manufacturer_product_id="B-01")
        self.assertNotEqual(product_identity(maker_a), product_identity(maker_b))

    def test_history_empty_existing_min_and_30_day_median(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceHistory(Path(tmp) / "prices.sqlite3")
            item = normalize(product(), NOW)
            first = store.enrich_and_record(item, NOW - timedelta(days=10))
            self.assertIsNone(first.median_30d)
            store.enrich_and_record(replace(item, current_price=Decimal("80")), NOW - timedelta(days=8))
            result = store.enrich_and_record(replace(item, current_price=Decimal("40")), NOW)
            self.assertEqual(result.first_observed_price, Decimal("50"))
            self.assertEqual(result.historical_minimum, Decimal("40"))
            self.assertEqual(result.median_30d, Decimal("65"))
            self.assertEqual(result.median_7d, None)
            store.close()

    def test_score_bounds_and_fake_msrp(self):
        item = normalize(product(regular_price=Decimal("1000"), current_price=Decimal("50")), NOW)
        item = replace(item, median_30d=Decimal("50"), historical_minimum=Decimal("50"), historical_confidence="HIGH")
        scored = score_deal(item, NOW)
        self.assertLess(scored.deal_score, 70)
        self.assertGreaterEqual(scored.deal_score, 0)
        self.assertLessEqual(scored.deal_score, 100)

    def test_historical_drop_outweighs_advertised_discount(self):
        item = normalize(product(regular_price=Decimal("100"), current_price=Decimal("50")), NOW)
        high = score_deal(replace(item, median_30d=Decimal("100"), historical_minimum=Decimal("60"), historical_confidence="HIGH"), NOW)
        low = score_deal(replace(item, median_30d=Decimal("50"), historical_minimum=Decimal("50"), historical_confidence="HIGH"), NOW)
        self.assertGreater(high.deal_score, low.deal_score)

    def test_single_observation_is_still_cold_start(self):
        item = normalize(product(), NOW)
        item = replace(item, median_30d=Decimal("50"), historical_minimum=Decimal("50"), historical_confidence="LOW")
        self.assertIn("cold start", score_deal(item, NOW).score_reason)

    def test_duplicate_and_unavailable_collector(self):
        class Good:
            retailer = "Shop"
            def discover(self):
                return [product(), product()]
        class Bad:
            retailer = "Offline"
            def discover(self):
                raise CollectorError("unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            result = run_discovery(Path(tmp), [Bad(), Good()], now=NOW, verify_images=False)
            self.assertEqual(result["products_discovered"], 2)
            self.assertEqual(result["normalized"], 1)
            self.assertEqual(result["sources"][0]["status"], "FAILED")
            self.assertEqual(result["buffer_writes"], 0)
            self.assertEqual(result["x_posts"], 0)
            self.assertTrue((Path(tmp) / "data/runtime/latest-deals.json").exists())

    def test_broken_parser_and_incomplete_data(self):
        with self.assertRaises(CollectorError):
            parse_listing("<html><body>broken</body></html>")
        self.assertIsNone(normalize(product(current_price=None), NOW))

    def test_timeout_and_http_429(self):
        def response(status: int, text: str, content_type: str = "text/html"):
            r = requests.Response()
            r.status_code = status
            r._content = text.encode()
            r.headers["Content-Type"] = content_type
            return r
        session = Mock()
        session.headers = {}
        session.request.side_effect = [response(200, "User-agent: *\nAllow: /"),
                                       requests.Timeout(), requests.Timeout()]
        client = HttpClient(session=session, min_interval=0)
        with self.assertRaises(CollectorError):
            client.get("https://shop.example/sale")
        self.assertEqual(session.request.call_count, 3)
        session2 = Mock()
        session2.headers = {}
        session2.request.side_effect = [response(200, "User-agent: *\nAllow: /"), response(429, "blocked")]
        client2 = HttpClient(session=session2, min_interval=0)
        with self.assertRaisesRegex(CollectorError, "429"):
            client2.get("https://shop.example/sale")
        self.assertEqual(session2.request.call_count, 2)

        for code in (403, 503):
            blocked = Mock()
            blocked.headers = {}
            blocked.request.side_effect = ([response(200, "User-agent: *\nAllow: /"), response(code, "error")]
                                           + ([response(code, "error")] if code == 503 else []))
            with self.assertRaises(CollectorError):
                HttpClient(session=blocked, min_interval=0).get("https://shop.example/sale")
            self.assertEqual(blocked.request.call_count, 3 if code == 503 else 2)

    def test_robots_disallow_prevents_page_request(self):
        robots = requests.Response()
        robots.status_code = 200
        robots._content = b"User-agent: *\nDisallow: /sale"
        session = Mock()
        session.headers = {}
        session.request.return_value = robots
        with self.assertRaisesRegex(CollectorError, "robots.txt disallows"):
            HttpClient(session=session, min_interval=0).get("https://shop.example/sale")
        self.assertEqual(session.request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
