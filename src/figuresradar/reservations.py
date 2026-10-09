"""Local dry-run reservations, isolated from the live publishing ledger."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

STATES = {"RESERVED", "QUEUED", "PUBLISHED", "EXPIRED", "FAILED"}
BLOCKING_STATES = ("RESERVED", "QUEUED", "PUBLISHED")
SCHEMA = """
CREATE TABLE IF NOT EXISTS content_reservations (
  offer_key TEXT PRIMARY KEY, product_identity TEXT NOT NULL, retailer TEXT NOT NULL,
  currency TEXT NOT NULL, price TEXT NOT NULL, state TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, output_path TEXT
);
CREATE INDEX IF NOT EXISTS idx_content_reservation_product
ON content_reservations(product_identity,retailer,currency,state);
"""


def reservation_key(deal: dict) -> str:
    payload = [deal["product_identity"], deal["retailer"], deal["currency"],
               str(Decimal(deal["current_price"]).quantize(Decimal("0.01")))]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


class ReservationStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=20)
        self.connection.executescript(SCHEMA)

    def close(self) -> None:
        self.connection.close()

    def reserve(self, deal: dict, posted_prices: list[Decimal] | None = None) -> tuple[bool, str]:
        """Atomically reserve one offer; another process cannot reserve it concurrently."""
        key = reservation_key(deal)
        identity = (deal["product_identity"], deal["retailer"], deal["currency"])
        price = Decimal(deal["current_price"])
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT state FROM content_reservations WHERE offer_key=?", (key,)).fetchone()
            if existing and existing[0] in BLOCKING_STATES:
                self.connection.rollback()
                return False, "already_reserved"
            rows = self.connection.execute(
                "SELECT price FROM content_reservations WHERE product_identity=? AND retailer=? AND currency=? "
                "AND state IN ('RESERVED','QUEUED','PUBLISHED')", identity).fetchall()
            prior = [Decimal(row[0]) for row in rows] + list(posted_prices or [])
            if not is_material_new_offer_value(price, prior):
                self.connection.rollback()
                return False, "no_material_price_drop"
            self.connection.execute(
                "INSERT INTO content_reservations VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(offer_key) DO UPDATE SET state='RESERVED',updated_at=excluded.updated_at,output_path=NULL",
                (key, *identity, str(price), "RESERVED", now, now, None))
            self.connection.commit()
            return True, key
        except Exception:
            self.connection.rollback()
            raise

    def set_state(self, key: str, state: str, output_path: Path | None = None) -> None:
        if state not in STATES:
            raise ValueError("Invalid reservation state")
        with self.connection:
            self.connection.execute(
                "UPDATE content_reservations SET state=?,updated_at=?,output_path=? WHERE offer_key=?",
                (state, datetime.now(timezone.utc).isoformat(), str(output_path) if output_path else None, key))

    def clear_test_reservations(self) -> int:
        with self.connection:
            cursor = self.connection.execute("DELETE FROM content_reservations WHERE state IN ('RESERVED','FAILED')")
        return cursor.rowcount


def is_material_new_offer_value(current_price: Decimal, previous_prices: list[Decimal]) -> bool:
    if not previous_prices:
        return True
    return current_price <= min(previous_prices) * Decimal("0.95")
