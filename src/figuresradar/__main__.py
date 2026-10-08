from __future__ import annotations

import argparse
import json
from pathlib import Path

from .buffer_client import BufferQueueReader, BufferQueueSnapshot
from .config import Config
from .deal_pipeline import load_fixture, prepare_posts
from .logging import configure_logging
from .persistence import DealStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare FiguresRadar deals without publishing")
    parser.add_argument("--fixture", type=Path, default=None)
    parser.add_argument("--pending", type=int, default=None, help="Simulated Buffer count; test only")
    args = parser.parse_args()
    configure_logging()
    root = Path(__file__).resolve().parents[2]
    config = Config.from_env(root)
    if not config.dry_run:
        raise RuntimeError("Live CLI is not activated in this phase; use DRY_RUN=true")
    fixture = args.fixture or root / "data" / "sample-deals.json"
    deals = load_fixture(fixture)
    if args.pending is not None:
        snapshot = BufferQueueSnapshot((), args.pending)
        queue_source = "simulation"
        channel_name = "simulation"
    else:
        reader = BufferQueueReader(
            config.buffer_api_key or "", config.buffer_org_id or "", config.buffer_channel_id or ""
        )
        channel = reader.validate_target()
        snapshot = reader.snapshot()
        queue_source = "buffer_read_only"
        channel_name = str(channel.get("displayName") or channel.get("name"))
    store = DealStore(config.database, readonly=True)
    try:
        result = prepare_posts(deals, snapshot, store, root)
    finally:
        store.close()
    print(json.dumps({
        "mode": "DRY_RUN",
        "queue_source": queue_source,
        "buffer_channel": channel_name,
        "pending_before": result.pending_before,
        "needed_posts": result.needed,
        "prepared_count": len(result.prepared),
        "projected_queue_count": result.pending_before + len(result.prepared),
        "posts": [{
            "unique_id": post.deal.unique_id,
            "text": post.text,
            "image_url": post.image_url,
            "test_image_path": post.test_image_path,
        } for post in result.prepared],
        "skipped": result.skipped,
        "errors": result.errors,
        "buffer_writes": 0,
    }, ensure_ascii=False, indent=2))
    return 1 if result.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
