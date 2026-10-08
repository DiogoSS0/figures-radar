from __future__ import annotations

import tempfile
import unittest
import io
import json
import sqlite3
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest import mock

from figuresradar.buffer_client import BufferQueueReader, BufferQueueSnapshot, BufferReadError, add_to_queue_payload
from figuresradar.config import Config, needed_posts
from figuresradar.content_generator import generate_text
from figuresradar.deal_pipeline import load_fixture, prepare_posts
from figuresradar.persistence import DealStore
from figuresradar.__main__ import main

ROOT = Path(__file__).resolve().parents[1]


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "deals.sqlite3"
        self.deals = load_fixture(ROOT / "data" / "sample-deals.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_with_count(self, count: int):
        store = DealStore(self.db, readonly=True)
        try:
            return prepare_posts(self.deals, BufferQueueSnapshot((), count), store, ROOT)
        finally:
            store.close()

    def test_exact_queue_capacity_cases(self):
        for pending, expected in [(10, 0), (9, 1), (7, 3), (5, 5), (0, 10)]:
            with self.subTest(pending=pending):
                result = self.run_with_count(pending)
                self.assertEqual(needed_posts(pending), expected)
                self.assertEqual(result.needed, expected)
                self.assertEqual(len(result.prepared), expected)
                self.assertLessEqual(pending + len(result.prepared), 10)

    def test_overfull_queue_produces_zero(self):
        self.assertEqual(len(self.run_with_count(11).prepared), 0)

    def test_duplicate_rejected_across_runs(self):
        writer = DealStore(self.db, readonly=False)
        writer.record_queued(self.deals[0], "buffer-1")
        writer.close()
        result = self.run_with_count(0)
        self.assertNotIn("fixture-01", [post.deal.unique_id for post in result.prepared])
        self.assertIn(("fixture-01", "duplicate_offer"), result.skipped)

    def test_material_price_drop_can_create_new_offer(self):
        old = replace(self.deals[0], unique_id="old", current_price=Decimal("129.99"))
        new = replace(self.deals[0], unique_id="new", current_price=Decimal("99.99"))
        writer = DealStore(self.db, readonly=False)
        writer.record_queued(old, "buffer-old")
        writer.close()
        reader = DealStore(self.db, readonly=True)
        try:
            result = prepare_posts([new], BufferQueueSnapshot((), 9), reader, ROOT)
        finally:
            reader.close()
        self.assertEqual([post.deal.unique_id for post in result.prepared], ["new"])

    def test_small_price_change_is_not_material(self):
        old = replace(self.deals[0], unique_id="old", current_price=Decimal("129.99"))
        new = replace(self.deals[0], unique_id="new", current_price=Decimal("127.99"))
        writer = DealStore(self.db, readonly=False)
        writer.record_queued(old, "buffer-old")
        writer.close()
        reader = DealStore(self.db, readonly=True)
        try:
            result = prepare_posts([new], BufferQueueSnapshot((), 9), reader, ROOT)
        finally:
            reader.close()
        self.assertEqual(len(result.prepared), 0)
        self.assertIn(("new", "no_material_price_drop"), result.skipped)

    def test_dry_run_does_not_call_buffer_or_write_ledger(self):
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"DRY_RUN": "true", "FIGURESRADAR_DB": str(self.db)}), \
             mock.patch.object(sys, "argv", ["figuresradar", "--pending", "8"]), \
             mock.patch("figuresradar.__main__.BufferQueueReader") as reader, \
             redirect_stdout(output):
            self.assertEqual(main(), 0)
        reader.assert_not_called()
        result = json.loads(output.getvalue())
        self.assertEqual(result["prepared_count"], 2)
        self.assertEqual(result["buffer_writes"], 0)
        self.assertFalse(self.db.exists())
        self.assertLessEqual(result["projected_queue_count"], 10)

    def test_one_candidate_failure_does_not_corrupt_others_or_ledger(self):
        writer = DealStore(self.db, readonly=False)
        writer.record_queued(self.deals[-1], "existing")
        writer.close()
        before = self.db.read_bytes()

        def sometimes_fails(deal):
            if deal.unique_id == "fixture-02":
                raise ValueError("fixture failure")
            return generate_text(deal)

        store = DealStore(self.db, readonly=True)
        try:
            result = prepare_posts(self.deals[:3], BufferQueueSnapshot((), 7), store, ROOT, content_generator=sometimes_fails)
        finally:
            store.close()
        self.assertEqual([p.deal.unique_id for p in result.prepared], ["fixture-01", "fixture-03"])
        self.assertEqual(result.errors, (("fixture-02", "ValueError"),))
        self.assertEqual(self.db.read_bytes(), before)

    def test_failed_ledger_insert_rolls_back_only_that_deal(self):
        store = DealStore(self.db, readonly=False)
        try:
            store.record_queued(self.deals[0], "buffer-1")
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_queued(self.deals[1], "buffer-1")
            self.assertTrue(store.has_offer(self.deals[0]))
            self.assertFalse(store.has_offer(self.deals[1]))
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM known_deals").fetchone()[0], 1)
            store.record_queued(self.deals[2], "buffer-3")
            self.assertTrue(store.has_offer(self.deals[2]))
        finally:
            store.close()

    def test_buffer_payload_uses_buffer_schedule_and_image(self):
        post = self.run_with_count(9).prepared[0]
        payload = add_to_queue_payload("channel", post.text, post.image_url)
        self.assertEqual(payload["mode"], "addToQueue")
        self.assertEqual(payload["schedulingType"], "automatic")
        self.assertNotIn("dueAt", payload)
        self.assertEqual(payload["assets"][0]["image"]["url"], post.image_url)
        self.assertTrue(Path(post.test_image_path).exists())

    def test_write_requires_both_explicit_flags(self):
        for dry_run, enabled, allowed in [(True, False, False), (True, True, False),
                                          (False, False, False), (False, True, True)]:
            cfg = Config(dry_run, enabled, self.db, None, None, None)
            if allowed:
                cfg.assert_writes_allowed()
            else:
                with self.assertRaises(RuntimeError):
                    cfg.assert_writes_allowed()

    def test_cli_stays_dormant_even_if_both_write_flags_are_set(self):
        with mock.patch.dict("os.environ", {"DRY_RUN": "false", "BUFFER_WRITE_ENABLED": "true"}), \
             mock.patch.object(sys, "argv", ["figuresradar", "--pending", "0"]), \
             mock.patch("figuresradar.__main__.BufferQueueReader") as reader:
            with self.assertRaises(RuntimeError):
                main()
        reader.assert_not_called()

    def test_buffer_reader_only_queries_target_channel(self):
        class Response:
            status_code = 200
            headers = {}

            def json(self):
                return {"data": {"posts": {"edges": [{"node": {
                    "id": "p1", "text": "existing", "channelId": "figures-channel",
                }}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response()

        session = Session()
        result = BufferQueueReader("secret", "org", "figures-channel", session).snapshot()
        self.assertEqual(result.pending_count, 1)
        self.assertEqual(len(session.calls), 1)
        sent = session.calls[0][1]["json"]
        self.assertIn("GetScheduledPosts", sent["query"])
        self.assertEqual(sent["variables"]["input"]["filter"]["channelIds"], ["figures-channel"])
        self.assertNotIn("createPost", sent["query"])

    def test_buffer_error_does_not_echo_token(self):
        class Response:
            status_code = 401
            headers = {}

        class Session:
            def post(self, *args, **kwargs):
                return Response()

        with self.assertRaises(BufferReadError) as raised:
            BufferQueueReader("private-token", "org", "channel", Session()).snapshot()
        self.assertNotIn("private-token", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
