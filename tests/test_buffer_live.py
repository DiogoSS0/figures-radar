from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from figuresradar.buffer_client import (
    BufferAmbiguousWriteError, BufferQueueReader, BufferQueueSnapshot,
    BufferReadError, BufferWriteError,
)
from figuresradar.config import Config, needed_posts
from figuresradar.content_generator import generate_text
from figuresradar.deal_pipeline import load_fixture
from figuresradar.live_publisher import LivePublisher
from figuresradar.models import PreparedPost
from figuresradar.persistence import DealStore

ROOT = Path(__file__).resolve().parents[1]


class Response:
    status_code = 200
    headers = {}

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(kwargs["json"])
        return self.responses.pop(0)


class BufferIntegrationTests(unittest.TestCase):
    def test_channel_selection_checks_name_service_and_id(self):
        channels = [
            {"id": "figure-id", "name": "FiguresRadar", "displayName": "FiguresRadar", "service": "twitter"},
            {"id": "zah-id", "name": "ZahZah_cun", "displayName": "ZahZah_cun", "service": "twitter"},
        ]
        body = {"data": {"channels": channels}}
        client = BufferQueueReader("token", "org", "figure-id", Session([Response(body)]))
        self.assertEqual(client.validate_target()["id"], "figure-id")
        wrong = BufferQueueReader("token", "org", "zah-id", Session([Response(body)]))
        with self.assertRaises(BufferReadError):
            wrong.validate_target()

    def test_overfull_api_page_cannot_create_capacity(self):
        posts = [{"node": {"id": str(i), "channelId": "figure-id", "text": "", "assets": []}} for i in range(100)]
        body = {"data": {"posts": {"edges": posts, "pageInfo": {"hasNextPage": True, "endCursor": "next"}}}}
        result = BufferQueueReader("token", "org", "figure-id", Session([Response(body)])).snapshot()
        self.assertEqual(result.pending_count, 100)
        self.assertEqual(needed_posts(result.pending_count), 0)

    def test_create_mutation_is_gated_and_uses_add_to_queue(self):
        session = Session([Response({"data": {"createPost": {"post": {
            "id": "buffer-1", "channelId": "figure-id", "text": "deal",
        }}}})])
        disabled = BufferQueueReader("token", "org", "figure-id", session)
        with self.assertRaises(BufferWriteError):
            disabled.create_queued_post("deal", "https://cdn.example/image.png")
        self.assertEqual(len(session.calls), 0)
        enabled = BufferQueueReader("token", "org", "figure-id", session, allow_writes=True)
        self.assertEqual(enabled.create_queued_post("deal", "https://cdn.example/image.png")["id"], "buffer-1")
        payload = session.calls[0]["variables"]["input"]
        self.assertEqual(payload["mode"], "addToQueue")
        self.assertNotIn("dueAt", payload)
        self.assertEqual(len(session.calls), 1)


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DealStore(Path(self.tmp.name) / "ledger.sqlite3", readonly=False)
        fixture = load_fixture(ROOT / "data" / "sample-deals.json")[0]
        deal = replace(
            fixture, unique_id="real-offer-1", source="manual-verified",
            product_url="https://retailer.example/products/figure-1",
            image_url="https://cdn.example/figure-1.png",
        )
        self.post = PreparedPost(deal, generate_text(deal), deal.image_url, datetime.now(timezone.utc))
        self.config = Config(False, True, self.store.path, "token", "org", "figure-id")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fake_buffer(self, after_has_post):
        post = {"id": "buffer-confirmed", "channelId": "figure-id", "text": self.post.text,
                "assets": [{"source": self.post.image_url}]}

        class FakeBuffer:
            allow_writes = True

            def __init__(self):
                self.reads = 0
                self.writes = 0

            def validate_target(self):
                return {"id": "figure-id", "name": "FiguresRadar", "service": "twitter"}

            def snapshot(self):
                self.reads += 1
                posts = (post,) if after_has_post and self.reads >= 2 else ()
                return BufferQueueSnapshot(posts, len(posts))

            def find_equivalent(self, snapshot, text, image_url):
                return next((item for item in snapshot.posts if item["text"] == text and
                             item["assets"][0]["source"] == image_url), None)

            def create_queued_post(self, text, image_url):
                self.writes += 1
                raise BufferAmbiguousWriteError("timeout")

        return FakeBuffer()

    def test_timeout_reconciles_existing_post_without_retry(self):
        buffer = self.fake_buffer(after_has_post=True)
        publisher = LivePublisher(self.config, buffer, self.store)
        result = publisher.enqueue_once(self.post)
        self.assertEqual(result.status, "reconciled")
        self.assertEqual(result.buffer_id, "buffer-confirmed")
        self.assertEqual(buffer.writes, 1)
        self.assertEqual(publisher.enqueue_once(self.post).status, "already_recorded")
        self.assertEqual(buffer.writes, 1)
        self.assertTrue(self.store.has_offer(self.post.deal))

    def test_unresolved_timeout_blocks_second_write(self):
        buffer = self.fake_buffer(after_has_post=False)
        publisher = LivePublisher(self.config, buffer, self.store)
        with self.assertRaises(BufferAmbiguousWriteError):
            publisher.enqueue_once(self.post)
        first_fingerprint = self.store.conn.execute("SELECT fingerprint FROM prepared_posts").fetchone()[0]
        with self.assertRaises(BufferAmbiguousWriteError):
            publisher.enqueue_once(self.post)
        self.assertEqual(buffer.writes, 1)
        self.assertEqual(self.store.conn.execute("SELECT fingerprint FROM prepared_posts").fetchone()[0], first_fingerprint)
        self.assertFalse(self.store.has_offer(self.post.deal))

    def test_fixture_cannot_be_sent_even_with_both_flags(self):
        fixture = load_fixture(ROOT / "data" / "sample-deals.json")[0]
        post = PreparedPost(fixture, generate_text(fixture), fixture.image_url, datetime.now(timezone.utc))
        buffer = self.fake_buffer(after_has_post=False)
        with self.assertRaises(RuntimeError):
            LivePublisher(self.config, buffer, self.store).enqueue_once(post)
        self.assertEqual(buffer.writes, 0)


if __name__ == "__main__":
    unittest.main()
