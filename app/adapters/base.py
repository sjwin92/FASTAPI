from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading


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
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class AdapterSearchOutcome:
    """Products plus a safe, client-facing availability status."""

    products: list[ProductResult]
    error_code: str | None = None

    @property
    def is_available(self) -> bool:
        return self.error_code is None


class BaseAdapter(ABC):
    retailer_key: str

    def _state(self) -> threading.local:
        # Adapter instances are shared, so failures must be isolated per request thread.
        state = getattr(self, "_search_state", None)
        if state is None:
            state = threading.local()
            self._search_state = state
        return state

    def _mark_unavailable(self, code: str = "source_unavailable") -> list[ProductResult]:
        self._state().error_code = code
        return []

    def search_with_status(self, query: str) -> AdapterSearchOutcome:
        """Search without conflating an upstream failure with zero matches."""
        state = self._state()
        state.error_code = None
        try:
            products = self.search(query)
        except Exception:
            products = self._mark_unavailable()
        return AdapterSearchOutcome(products=products, error_code=state.error_code)

    @abstractmethod
    def search(self, query: str) -> list[ProductResult]:
        """Search for products matching the query string."""

    @abstractmethod
    def fetch_price(self, external_id: str) -> ProductResult | None:
        """Fetch the current price for a single product by its external ID."""


class DisabledAdapter(BaseAdapter):
    """Fail-closed provider used when automated access is not authorised."""

    def __init__(self, retailer_key: str, code: str = "disabled_by_policy") -> None:
        self.retailer_key = retailer_key
        self.code = code

    def search(self, query: str) -> list[ProductResult]:
        return self._mark_unavailable(self.code)

    def fetch_price(self, external_id: str) -> ProductResult | None:
        return None
