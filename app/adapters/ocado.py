"""
Ocado adapter.

Uses the internal webproductpagews API discovered in the JS bundle.
Requires a seeded session (homepage GET sets required CloudFront cookies).
"""
from __future__ import annotations

import re
import threading
import requests
from .base import BaseAdapter, ProductResult
from .http import REQUEST_TIMEOUT, create_session

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.ocado.com/",
}

_HOME = "https://www.ocado.com/"
_SEARCH_API = "https://www.ocado.com/api/webproductpagews/v6/product-pages/search"
_PRODUCT_URL = "https://www.ocado.com/products/{slug}/{sku}"

_lock = threading.Lock()
_session: requests.Session | None = None
_seeded = False


def _ensure_session() -> requests.Session:
    global _session, _seeded
    with _lock:
        if _session is None:
            _session = create_session(_HEADERS)
        if not _seeded:
            try:
                _session.get(_HOME, headers={"Accept": "text/html"}, timeout=REQUEST_TIMEOUT)
            except Exception:
                pass
            _seeded = True
    return _session


def _slug(name: str) -> str:
    """Convert product name to URL slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _parse_product(item: dict) -> ProductResult | None:
    sku = item.get("retailerProductId", "")
    name = item.get("name", "")
    if not sku or not name:
        return None

    price_obj = item.get("price", {})
    try:
        price = float(price_obj.get("amount", 0))
    except (ValueError, TypeError):
        return None
    if price == 0:
        return None

    unit_obj = item.get("unitPrice", {})
    unit_price_obj = unit_obj.get("price", {}) if isinstance(unit_obj, dict) else {}
    unit_price = float(unit_price_obj.get("amount", 0)) or None
    unit_label = unit_obj.get("unit") if isinstance(unit_obj, dict) else None

    image = item.get("image", {})
    image_url = image.get("src") if isinstance(image, dict) else None

    promo = None
    promos = item.get("promotions", [])
    if promos and isinstance(promos, list):
        promo = promos[0].get("description")

    return ProductResult(
        external_id=sku,
        name=name,
        url=_PRODUCT_URL.format(slug=_slug(name), sku=sku),
        price=price,
        unit_price=unit_price,
        unit=unit_label,
        image_url=image_url,
        in_stock=item.get("available", True),
        brand=item.get("brand"),
        size_text=item.get("packSizeDescription"),
        promo_text=promo,
    )


class OcadoAdapter(BaseAdapter):
    retailer_key = "ocado"

    def search(self, query: str) -> list[ProductResult]:
        try:
            s = _ensure_session()
            resp = s.get(
                _SEARCH_API,
                params={
                    "q": query,
                    "tag": "web",
                    "maxPageSize": 20,
                    "maxProductsToDecorate": 30,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return self._mark_unavailable()

        results = []
        for group in data.get("productGroups", []):
            for item in group.get("decoratedProducts", []):
                r = _parse_product(item)
                if r:
                    results.append(r)
        return results

    def fetch_price(self, external_id: str) -> ProductResult | None:
        # Re-search using the SKU as query — Ocado has no direct SKU lookup
        # that's accessible without authentication.
        results = self.search(external_id)
        return next((r for r in results if r.external_id == external_id), None)
