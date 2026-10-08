from __future__ import annotations

from pathlib import Path

from .models import Deal


def prepare_image(deal: Deal, project_root: Path) -> tuple[str, str | None]:
    """Keep a local test visual for preview; never upload in this phase."""
    if not deal.image_url.startswith("https://"):
        raise ValueError("Image URL must use HTTPS")
    test_path = project_root / "data" / "test-image.svg"
    return deal.image_url, str(test_path) if test_path.exists() else None
