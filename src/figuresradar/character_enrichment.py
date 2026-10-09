"""Optional, sourced character descriptions. No inferred lore is emitted."""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class CharacterEnrichment:
    character: str | None
    series: str | None
    short_description: str | None
    source: str | None
    confidence: str


class CharacterEnricher:
    def __init__(self, entries: dict[tuple[str, str], CharacterEnrichment] | None = None):
        self.entries = entries or {}

    def enrich(self, character: str | None, series: str | None) -> CharacterEnrichment:
        key = ((character or "").strip().casefold(), (series or "").strip().casefold())
        entry = self.entries.get(key)
        if entry and entry.short_description and entry.source and urlsplit(entry.source).scheme == "https" and entry.confidence == "HIGH":
            return entry
        return CharacterEnrichment(character, series, None, None, "UNKNOWN")
