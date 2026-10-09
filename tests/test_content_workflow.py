from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import requests
from PIL import Image

from figuresradar.buffer_client import BufferQueueSnapshot
from figuresradar.card_renderer import download_product_image, render_card
from figuresradar.character_enrichment import CharacterEnricher
from figuresradar.content_workflow import (_buffer_draft, generate_copy, prepare_real_posts,
                                           rank_diverse, revalidate, x_weighted_length)
from figuresradar.models import Deal
from figuresradar.persistence import DealStore
from figuresradar.reservations import ReservationStore

NOW = datetime(2026, 10, 9, 18, tzinfo=timezone.utc)


def deal(number: int, **updates) -> dict:
    value = {
        "product_identity": f"id:{number}", "figure_name": f"Figure {number} Nendoroid",
        "retailer": "Nin-Nin-Game", "product_url": f"https://www.nin-nin-game.com/en/figure/{number}",
        "image_url": "https://media1.nin-nin-game.com/product.jpg", "current_price": "20.00",
        "regular_price": "40.00", "discount_percent": "50.00", "currency": "EUR",
        "stock_status": "IN_STOCK", "deal_score": 79, "historical_confidence": "LOW",
        "last_verified_at": NOW.isoformat(), "source_confidence": "HIGH", "median_30d": None,
        "character": None, "series": None, "manufacturer": None,
    }
    value.update(updates)
    return value


def fake_download(url: str, assets: Path) -> Path:
    assets.mkdir(parents=True, exist_ok=True)
    path = assets / "figure.jpg"
    Image.new("RGB", (600, 600), "#dddddd").save(path)
    return path


class FakeResponse:
    def __init__(self, data: bytes, content_type: str = "image/jpeg", status: int = 200,
                 length: str | None = None):
        self.data = data
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        if length:
            self.headers["Content-Length"] = length

    def iter_content(self, size):
        yield self.data

    def close(self):
        pass


class ContentWorkflowTests(unittest.TestCase):
    def test_ten_slots_four_deals_produces_four_local_posts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = prepare_real_posts([deal(i) for i in range(4)], BufferQueueSnapshot((), 0), root,
                                        now=NOW, downloader=fake_download)
            self.assertEqual((result.slots, result.eligible, len(result.prepared)), (10, 4, 4))
            self.assertFalse((root / "data/figuresradar.sqlite3").exists())
            for post in result.prepared:
                self.assertEqual(post["status"], "RESERVED")
                self.assertEqual(post["buffer_payload_draft"]["status"], "AWAITING_PUBLIC_CARD_URL")
                self.assertIsNone(post["buffer_payload"])
                self.assertEqual(post["buffer_writes"], 0)
                self.assertEqual(post["x_posts"], 0)
                self.assertTrue(Path(post["card_path"]).exists())
                self.assertTrue(Path(post["card_path"]).with_name("copy.txt").exists())
            self.assertTrue((result.run_dir / "review.html").exists())
            self.assertTrue((result.run_dir / "contact-sheet.jpg").exists())
            with Image.open(result.prepared[0]["card_path"]) as card:
                self.assertEqual(card.size, (1200, 675))
            again = prepare_real_posts([deal(i) for i in range(4)], BufferQueueSnapshot((), 0), root,
                                       now=NOW, downloader=fake_download)
            self.assertEqual(len(again.prepared), 0)
            self.assertEqual({r["reason"] for r in again.rejected}, {"already_reserved"})

    def test_score_threshold_capacity_and_diversity(self):
        candidates = [deal(1, deal_score=90), deal(2, deal_score=90),
                      deal(3, deal_score=89, retailer="HobbyLink Japan", series="Other"),
                      deal(4, deal_score=69)]
        ranked = rank_diverse(candidates[:3])
        self.assertEqual(ranked[0]["product_identity"], "id:1")
        self.assertEqual(ranked[1]["retailer"], "HobbyLink Japan")
        self.assertEqual(rank_diverse([deal(5, deal_score=90),
                                       deal(6, deal_score=86, retailer="HobbyLink Japan")])[0]["product_identity"], "id:5")
        with tempfile.TemporaryDirectory() as temp:
            result = prepare_real_posts(candidates, BufferQueueSnapshot((), 9), Path(temp),
                                        now=NOW, downloader=fake_download)
            self.assertEqual(result.slots, 1)
            self.assertEqual(result.eligible, 3)
            self.assertEqual(len(result.prepared), 1)
            self.assertIn("score_below_70", {r["reason"] for r in result.rejected})

    def test_existing_buffer_link_blocks_different_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            item = deal(1)
            snapshot = BufferQueueSnapshot(({"text": "Older copy " + item["product_url"]},), 1)
            result = prepare_real_posts([item], snapshot, Path(temp), now=NOW, downloader=fake_download)
            self.assertEqual(len(result.prepared), 0)
            self.assertEqual(result.rejected[-1]["reason"], "already_in_buffer_queue")

    def test_material_drop_and_concurrent_reservation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reservations.sqlite3"
            first = ReservationStore(path)
            second = ReservationStore(path)
            try:
                item = deal(1)
                self.assertTrue(first.reserve(item)[0])
                self.assertEqual(second.reserve(item), (False, "already_reserved"))
                self.assertEqual(second.reserve(deal(1, current_price="19.50")),
                                 (False, "no_material_price_drop"))
                self.assertTrue(second.reserve(deal(1, current_price="18.00"))[0])
                self.assertEqual(first.clear_test_reservations(), 2)
                self.assertTrue(first.reserve(item)[0])
            finally:
                first.close()
                second.close()

    def test_existing_publication_ledger_blocks_repeat_but_allows_material_drop(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = Deal(unique_id="old", figure_name="Figure 1 Nendoroid", character="", series="",
                       manufacturer="", retailer="Nin-Nin-Game",
                       product_url="https://www.nin-nin-game.com/en/figure/1",
                       image_url="https://media1.nin-nin-game.com/product.jpg",
                       normal_price=Decimal("40"), current_price=Decimal("20"), currency="EUR",
                       discount_percent=Decimal("50"), deal_score=Decimal("79"), detected_at=NOW,
                       verified_at=NOW, stock_status="in_stock", source="retailer")
            ledger = DealStore(root / "data/figuresradar.sqlite3", readonly=False)
            ledger.record_queued(old, "buffer-old")
            ledger.close()
            repeated = prepare_real_posts([deal(1)], BufferQueueSnapshot((), 0), root,
                                          now=NOW, downloader=fake_download)
            self.assertEqual(len(repeated.prepared), 0)
            self.assertEqual(repeated.rejected[-1]["reason"], "no_material_price_drop")
            cheaper = prepare_real_posts([deal(1, current_price="18.00")], BufferQueueSnapshot((), 0), root,
                                         now=NOW, downloader=fake_download)
            self.assertEqual(len(cheaper.prepared), 1)

    def test_copy_low_confidence_no_lore_and_x_limit(self):
        item = deal(1, figure_name="Nendoroid Very Long Name " * 8)
        text, enrichment = generate_copy(item, enrichment=CharacterEnricher())
        self.assertLessEqual(x_weighted_length(text), 280)
        self.assertNotIn("lowest price", text.lower())
        self.assertNotIn("historical low", text.lower())
        self.assertIsNone(enrichment["short_description"])
        self.assertIn("50% OFF", text)
        self.assertIn("Powered by NekoPrice", text)
        self.assertNotIn("example.invalid", text)
        no_regular, _ = generate_copy(deal(2, regular_price=None, discount_percent=None))
        self.assertIn("Deal detected", no_regular)
        self.assertNotIn("% OFF", no_regular)
        self.assertNotIn("→", no_regular)

    def test_image_validation_size_type_payload_timeout(self):
        valid = io.BytesIO()
        Image.new("RGB", (400, 400), "white").save(valid, format="JPEG")
        url = "https://media1.nin-nin-game.com/product.jpg"
        with tempfile.TemporaryDirectory() as temp:
            assets = Path(temp)
            session = mock.Mock()
            session.get.return_value = FakeResponse(valid.getvalue())
            self.assertTrue(download_product_image(url, assets, session=session).exists())
            for response in [FakeResponse(valid.getvalue(), "text/html"),
                             FakeResponse(valid.getvalue(), length=str(9 * 1024 * 1024)),
                             FakeResponse(b"not an image"), FakeResponse(valid.getvalue(), status=403)]:
                session.get.return_value = response
                with self.assertRaises(ValueError):
                    download_product_image(url, assets, session=session)
            session.get.side_effect = requests.Timeout()
            with self.assertRaises(requests.Timeout):
                download_product_image(url, assets, session=session)
            session.get.side_effect = None
            session.get.return_value = FakeResponse(valid.getvalue() + b"padding")
            with mock.patch("figuresradar.card_renderer.MAX_IMAGE_BYTES", 10):
                with self.assertRaisesRegex(ValueError, "too large"):
                    download_product_image(url, assets, session=session)
            with self.assertRaises(ValueError):
                download_product_image("http://media1.nin-nin-game.com/a.jpg", assets)
            with self.assertRaises(ValueError):
                download_product_image("https://127.0.0.1/a.jpg", assets)

    def test_card_never_invents_regular_price_or_discount(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            image = fake_download("https://media1.nin-nin-game.com/a.jpg", root)
            with_discount = root / "discount.jpg"
            without_discount = root / "no-discount.jpg"
            render_card(deal(1), image, with_discount)
            render_card(deal(1, regular_price=None, discount_percent=None), image, without_discount)
            with Image.open(with_discount) as a, Image.open(without_discount) as b:
                self.assertEqual(a.size, (1200, 675))
                self.assertEqual(b.size, (1200, 675))
                self.assertNotEqual(a.crop((650, 445, 850, 485)).tobytes(),
                                    b.crop((650, 445, 850, 485)).tobytes())
                self.assertNotEqual(a.crop((965, 506, 1135, 564)).tobytes(),
                                    b.crop((965, 506, 1135, 564)).tobytes())

    def test_buffer_payload_and_revalidation_interface(self):
        item = deal(1)
        with tempfile.TemporaryDirectory() as temp:
            card = Path(temp) / "card.jpg"
            pending, draft = _buffer_draft("channel", "text", card, None)
            self.assertIsNone(pending)
            self.assertEqual(draft["mode"], "addToQueue")
            ready, _ = _buffer_draft("channel", "text", card, "https://cdn.example/card.jpg")
            self.assertEqual(ready["mode"], "addToQueue")
            self.assertNotIn("dueAt", ready)
        self.assertEqual(revalidate(item), "UNKNOWN")
        self.assertEqual(revalidate(item, item), "VALID")
        self.assertEqual(revalidate(item, deal(1, current_price="10.00")), "PRICE_CHANGED")
        self.assertEqual(revalidate(item, deal(1, stock_status="OUT_OF_STOCK")), "OUT_OF_STOCK")

    def test_failed_card_marks_reservation_failed_and_other_deal_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            def renderer(item, image, output):
                if item["product_identity"] == "id:1":
                    raise RuntimeError("render failed")
                render_card(item, image, output)
            result = prepare_real_posts([deal(1), deal(2)], BufferQueueSnapshot((), 0), root,
                                        now=NOW, downloader=fake_download, renderer=renderer)
            self.assertEqual(len(result.prepared), 1)
            self.assertIn("RuntimeError", {item["reason"] for item in result.rejected})
            store = ReservationStore(root / "data/runtime/preparation.sqlite3")
            try:
                self.assertEqual(dict(store.connection.execute(
                    "SELECT product_identity,state FROM content_reservations")),
                    {"id:1": "FAILED", "id:2": "RESERVED"})
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
