from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def history_status(root: Path) -> dict:
    path = root / "data" / "runtime" / "price-history.sqlite3"
    if not path.exists():
        return {"database": "MISSING"}
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            return {"database": f"ERROR: {integrity}"}
        first, last, observations = connection.execute(
            "SELECT MIN(observed_at),MAX(observed_at),COUNT(*) FROM price_observations").fetchone()
        products = connection.execute("SELECT COUNT(*) FROM discovery_products").fetchone()[0]
        retailers = dict(connection.execute(
            "SELECT retailer,COUNT(*) FROM price_observations GROUP BY retailer ORDER BY retailer"))
        runs = connection.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0]
        successful = connection.execute(
            "SELECT COUNT(*) FROM discovery_runs WHERE status='SUCCESS'").fetchone()[0]
        high = 0
        for count, earliest, latest, days in connection.execute(
            "SELECT COUNT(*),MIN(observed_at),MAX(observed_at),COUNT(DISTINCT SUBSTR(observed_at,1,10)) "
            "FROM price_observations GROUP BY product_identity,retailer,currency"):
            if count >= 6 and days >= 4 and (datetime.fromisoformat(latest) - datetime.fromisoformat(earliest)).total_seconds() >= 7 * 86400:
                high += 1
        period = (datetime.fromisoformat(last) - datetime.fromisoformat(first)).total_seconds() / 86400 if first else 0.0
        return {"database": "OK", "observation_period_days": round(period, 2),
                "runs": runs, "successful_runs": successful, "products_tracked": products,
                "price_observations": observations, "retailers": retailers,
                "high_historical_confidence": high, "next_milestone": "7 days" if period < 7 else "reached",
                "buffer_writes": 0, "x_posts": 0}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Show local FiguresRadar history health")
    parser.add_argument("--changes", action="store_true", help="Show changes recorded by the latest completed discovery")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    status = history_status(root)
    print("FiguresRadar history\n")
    print(f"Database: {status['database']}")
    if status["database"] != "OK":
        return 1
    print(f"Observation period: {status['observation_period_days']:.2f} days")
    print(f"Runs: {status['runs']}")
    print(f"Successful runs: {status['successful_runs']}")
    print(f"Products tracked: {status['products_tracked']}")
    print(f"Price observations: {status['price_observations']}")
    print("Retailers:")
    for retailer, count in status["retailers"].items():
        print(f"  {retailer}: {count}")
    print(f"Products with HIGH historical confidence: {status['high_historical_confidence']}")
    print(f"Next milestone: {status['next_milestone']}")
    print("Buffer writes: 0")
    print("X posts: 0")
    if args.changes:
        changes = root / "data" / "runtime" / "latest-deals.json"
        if changes.exists():
            print("Changes since previous observation:")
            print(json.dumps(json.loads(changes.read_text(encoding="utf-8")).get("changes", []), ensure_ascii=False, indent=2))
    return 0
