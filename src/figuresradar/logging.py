from __future__ import annotations

import logging


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # Application logs contain only counts and opaque deal IDs, never credentials or API bodies.
