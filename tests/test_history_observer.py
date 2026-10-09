from __future__ import annotations

import fcntl
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from figuresradar.collectors.base import CollectorError, RawProduct
from figuresradar.discovery import PriceHistory, changes_for, normalize, run_discovery
from figuresradar.history_status import history_status

NOW = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)


def product(**updates):
    fields = dict(retailer="Shop", retailer_product_id="id-1", product_url="https://shop.example/item",
                  figure_name="Rem Nendoroid", regular_price=Decimal("100"),
                  current_price=Decimal("80"), currency="EUR", stock_status="IN_STOCK",
                  image_url=None, source="https://shop.example/sale")
    fields.update(updates)
    return RawProduct(**fields)


class Collector:
    retailer = "Shop"

    def __init__(self, items):
        self.items = items

    def discover(self):
        return self.items


class BrokenCollector:
    retailer = "Offline"

    def discover(self):
        raise CollectorError("HTTP 429 at https://example.invalid; stopped")


class HistoryObserverTests(unittest.TestCase):
    def test_successive_runs_restart_missing_product_and_currencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            one = run_discovery(root, [Collector([product()])], now=NOW, verify_images=False)
            self.assertEqual(one["changes"][0]["change"], "NEW_PRODUCT")
            run_discovery(root, [Collector([])], now=NOW + timedelta(hours=1), verify_images=False)
            two = run_discovery(root, [Collector([product(current_price=Decimal("70"))])],
                                now=NOW + timedelta(hours=2), verify_images=False)
            self.assertIn("PRICE_DOWN", [c["change"] for c in two["changes"]])
            run_discovery(root, [Collector([product(currency="JPY", current_price=Decimal("9000"),
                                                    regular_price=None)])],
                          now=NOW + timedelta(hours=3), verify_images=False)
            db = root / "data/runtime/price-history.sqlite3"
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(dict(connection.execute(
                    "SELECT currency,COUNT(*) FROM price_observations GROUP BY currency")),
                    {"EUR": 2, "JPY": 1})
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM discovery_products").fetchone()[0], 2)
            status = history_status(root)
            self.assertEqual(status["runs"], 4)
            self.assertEqual(status["successful_runs"], 4)
            self.assertEqual(status["price_observations"], 3)
            self.assertEqual(status["buffer_writes"], 0)
            self.assertEqual(status["x_posts"], 0)

    def test_lock_skips_concurrent_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "data/runtime/discover.lock"
            lock_path.parent.mkdir(parents=True)
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = run_discovery(root, [Collector([product()])], now=NOW, verify_images=False)
                self.assertEqual(result["status"], "SKIPPED_ALREADY_RUNNING")
            self.assertEqual(history_status(root)["runs"], 1)
            self.assertEqual(history_status(root)["price_observations"], 0)

    def test_source_failure_preserves_other_source_and_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_discovery(root, [BrokenCollector(), Collector([product()])], now=NOW,
                                   verify_images=False)
            self.assertEqual([s["status"] for s in result["sources"]], ["FAILED", "OK"])
            db = root / "data/runtime/price-history.sqlite3"
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute("SELECT status FROM discovery_runs").fetchone()[0], "PARTIAL_FAILURE")
                self.assertEqual(dict(connection.execute("SELECT retailer,status FROM discovery_run_sources")),
                                 {"Offline": "FAILED", "Shop": "OK"})
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_high_requires_five_prior_across_four_days_and_seven_day_span(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceHistory(Path(tmp) / "history.sqlite3")
            item = normalize(product(), NOW)
            for day in [0, 0, 0, 0, 7]:
                store.enrich_and_record(item, NOW + timedelta(days=day, seconds=day + store.connection.execute(
                    "SELECT COUNT(*) FROM price_observations").fetchone()[0]))
            low = store.enrich_and_record(item, NOW + timedelta(days=7, seconds=100))
            self.assertEqual(low.historical_confidence, "LOW")
            store.close()
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceHistory(Path(tmp) / "history.sqlite3")
            for day in [0, 1, 2, 4, 6]:
                store.enrich_and_record(item, NOW + timedelta(days=day))
            self.assertEqual(store.enrich_and_record(item, NOW + timedelta(days=6, hours=1)).historical_confidence, "LOW")
            self.assertEqual(store.enrich_and_record(item, NOW + timedelta(days=7)).historical_confidence, "HIGH")
            store.close()

    def test_price_stock_and_promotion_changes(self):
        base = normalize(product(), NOW)
        self.assertIn("PRICE_DOWN", changes_for(replace(base, current_price=Decimal("70")),
                                                ("80", "IN_STOCK", "100")))
        self.assertIn("PRICE_UP", changes_for(replace(base, current_price=Decimal("90")),
                                              ("80", "IN_STOCK", "100")))
        self.assertIn("SOLD_OUT", changes_for(replace(base, stock_status="OUT_OF_STOCK"),
                                              ("80", "IN_STOCK", "100")))
        self.assertIn("BACK_IN_STOCK", changes_for(base, ("80", "OUT_OF_STOCK", "100")))
        self.assertIn("PROMOTION_APPEARED", changes_for(base, ("80", "IN_STOCK", None)))
        self.assertIn("PROMOTION_DISAPPEARED", changes_for(replace(base, regular_price=None),
                                                          ("80", "IN_STOCK", "100")))

    def test_migration_creates_backup_and_keeps_observations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE price_observations (product_identity TEXT, retailer TEXT, currency TEXT, price TEXT, stock_status TEXT, observed_at TEXT, PRIMARY KEY(product_identity,retailer,currency,observed_at))")
                connection.execute("INSERT INTO price_observations VALUES (?,?,?,?,?,?)",
                                   ("id", "Shop", "EUR", "80", "IN_STOCK", NOW.isoformat()))
            store = PriceHistory(path)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0], 1)
            store.close()
            backups = list(Path(tmp).glob("history.sqlite3.backup-*"))
            self.assertEqual(len(backups), 1)
            with sqlite3.connect(backups[0]) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_observations").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
