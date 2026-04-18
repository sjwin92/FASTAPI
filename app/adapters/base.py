from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RetailerInfo:
    key: str
    name: str
    scraping_implemented: bool


class RetailerAdapter(Protocol):
    info: RetailerInfo

    def is_supported_url(self, url: str) -> bool:
        """Return true if URL belongs to this retailer."""
