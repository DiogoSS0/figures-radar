"""Dormant Buffer write path. The CLI never calls this module in this phase."""
from __future__ import annotations

from dataclasses import dataclass

from .buffer_client import BufferAmbiguousWriteError, BufferQueueReader, BufferReadError
from .config import Config, TARGET_QUEUE_SIZE
from .models import PreparedPost
from .persistence import DealStore


@dataclass(frozen=True)
class PublishResult:
    status: str
    buffer_id: str | None


class LivePublisher:
    def __init__(self, config: Config, buffer: BufferQueueReader, store: DealStore):
        self.config = config
        self.buffer = buffer
        self.store = store

    def enqueue_once(self, post: PreparedPost) -> PublishResult:
        self.config.assert_writes_allowed()
        if not self.buffer.allow_writes or self.store.readonly:
            raise RuntimeError("Live client and writable ledger are required")
        if post.deal.source == "fixture" or ".invalid" in post.deal.product_url or ".invalid" in post.image_url:
            raise RuntimeError("Fixture offers and placeholder media cannot be sent to Buffer")
        self.buffer.validate_target()
        if self.store.has_offer(post.deal):
            return PublishResult("already_recorded", None)

        state = self.store.record_prepared(post)
        snapshot = self.buffer.snapshot()
        existing = self.buffer.find_equivalent(snapshot, post.text, post.image_url)
        if existing:
            self.store.record_queued(post.deal, str(existing["id"]))
            return PublishResult("reconciled", str(existing["id"]))
        if state == "ambiguous":
            raise BufferAmbiguousWriteError("Earlier write remains unresolved; no automatic retry")
        if snapshot.pending_count >= TARGET_QUEUE_SIZE:
            return PublishResult("queue_full", None)

        try:
            created = self.buffer.create_queued_post(post.text, post.image_url)
        except BufferAmbiguousWriteError as original:
            try:
                after = self.buffer.snapshot()
                existing = self.buffer.find_equivalent(after, post.text, post.image_url)
            except BufferReadError:
                existing = None
            if existing:
                self.store.record_queued(post.deal, str(existing["id"]))
                return PublishResult("reconciled", str(existing["id"]))
            self.store.mark_ambiguous(post.deal)
            raise original

        buffer_id = str(created["id"])
        self.store.record_queued(post.deal, buffer_id)
        return PublishResult("created", buffer_id)
