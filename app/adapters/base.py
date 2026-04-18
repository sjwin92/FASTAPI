from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProductResult:
    external_id: str
    name: str
    url: str
    price: float
    unit_price: float | None = None
    unit: str | None = None
    image_url: str | None = None
    in_stock: bool = True
    # Extended fields — populated by adapters that can source them; None otherwise
    brand: str | None = None
    size_text: str | None = None
    unit_price_text: str | None = None
    promo_text: str | None = None


class BaseAdapter(ABC):
    retailer_key: str

    @abstractmethod
    def search(self, query: str) -> list[ProductResult]:
        """Search for products matching the query string."""

    @abstractmethod
    def fetch_price(self, external_id: str) -> ProductResult | None:
        """Fetch the current price for a single product by its external ID."""
