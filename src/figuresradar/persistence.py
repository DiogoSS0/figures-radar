from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .deduplication import offer_identity, preparation_fingerprint, product_identity
from .models import Deal, PreparedPost

SCHEMA = """
CREATE TABLE IF NOT EXISTS known_deals (
    offer_key TEXT PRIMARY KEY,
    product_key TEXT NOT NULL,
    currency TEXT NOT NULL,
    current_price TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS posted_deals (
    offer_key TEXT PRIMARY KEY,
    product_key TEXT NOT NULL,
    currency TEXT NOT NULL,
    current_price TEXT NOT NULL,
    unique_id TEXT NOT NULL,
    buffer_id TEXT NOT NULL UNIQUE,
    queued_at TEXT NOT NULL,
    posted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_posted_product ON posted_deals(product_key, currency);
CREATE TABLE IF NOT EXISTS prepared_posts (
    offer_key TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL UNIQUE,
    unique_id TEXT NOT NULL,
    text TEXT NOT NULL,
    image_url TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    state TEXT NOT NULL,
    buffer_id TEXT
);
"""


class DealStore:
    def __init__(self, path: Path, *, readonly: bool = True):
        self.path = path
        self.readonly = readonly
        if readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True) if path.exists() else sqlite3.connect(":memory:")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
        if not readonly or not path.exists():
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def prior_prices(self, deal: Deal) -> list[Decimal]:
        rows = self.conn.execute(
            "SELECT current_price FROM posted_deals WHERE product_key=? AND currency=?",
            (product_identity(deal), deal.currency.upper()),
        ).fetchall()
        return [Decimal(row[0]) for row in rows]

    def has_offer(self, deal: Deal) -> bool:
        return self.conn.execute("SELECT 1 FROM posted_deals WHERE offer_key=?", (offer_identity(deal),)).fetchone() is not None

    def record_prepared(self, post: PreparedPost) -> str:
        if self.readonly:
            raise RuntimeError("Dry-run store is read only")
        key = offer_identity(post.deal)
        row = self.conn.execute(
            "SELECT fingerprint, text, image_url, state FROM prepared_posts WHERE offer_key=?", (key,)
        ).fetchone()
        if row:
            if row[1:3] != (post.text, post.image_url):
                raise RuntimeError("Prepared offer changed; reconcile before replacing it")
            return str(row[3])
        with self.conn:
            self.conn.execute(
                "INSERT INTO prepared_posts VALUES (?,?,?,?,?,?,?,?)",
                (key, preparation_fingerprint(post), post.deal.unique_id, post.text, post.image_url,
                 post.prepared_at.isoformat(), "prepared", None),
            )
        return "prepared"

    def mark_ambiguous(self, deal: Deal) -> None:
        if self.readonly:
            raise RuntimeError("Dry-run store is read only")
        with self.conn:
            self.conn.execute(
                "UPDATE prepared_posts SET state='ambiguous' WHERE offer_key=?", (offer_identity(deal),)
            )

    def record_queued(self, deal: Deal, buffer_id: str) -> None:
        """A future live runner may call this only after Buffer returns a confirmed ID."""
        if self.readonly:
            raise RuntimeError("Dry-run store is read only")
        if not buffer_id:
            raise ValueError("Confirmed buffer_id required")
        now = datetime.now(timezone.utc).isoformat()
        snapshot = json.dumps(asdict(deal), default=str, ensure_ascii=False)
        with self.conn:
            self.conn.execute(
                "INSERT INTO known_deals VALUES (?,?,?,?,?,?,?) ON CONFLICT(offer_key) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                (offer_identity(deal), product_identity(deal), deal.currency.upper(), str(deal.current_price), snapshot, now, now),
            )
            self.conn.execute(
                "INSERT INTO posted_deals VALUES (?,?,?,?,?,?,?,?)",
                (offer_identity(deal), product_identity(deal), deal.currency.upper(), str(deal.current_price), deal.unique_id, buffer_id, now, None),
            )
            self.conn.execute(
                "UPDATE prepared_posts SET state='confirmed', buffer_id=? WHERE offer_key=?",
                (buffer_id, offer_identity(deal)),
            )
