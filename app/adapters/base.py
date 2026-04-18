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

    def search_products(self, query: str, limit: int = 20):
        """Search retailer public pages and return parsed products."""

    def fetch_product(self, product_url: str):
        """Fetch and parse one public product page."""
