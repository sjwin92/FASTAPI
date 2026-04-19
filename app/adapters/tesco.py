"""
Tesco adapter.

Search page with ?format=json returns 200 and embeds an Apollo GraphQL cache
in the HTML. Products are stored as "ProductType:{id}":{...} entries.
The regular /resources API is blocked by Akamai.
"""
from __future__ import annotations

import re
import threading
import requests
from .base import BaseAdapter, ProductResult

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

_SEARCH_URL = "https://www.tesco.com/groceries/en-GB/search"
_PRODUCT_URL = "https://www.tesco.com/groceries/en-GB/products/{id}"

# Field order in Tesco Apollo cache: __typename → id → status → price → title → brandName → defaultImageUrl
_PRODUCT_RE = re.compile(
    r'"ProductType:(\d+)":\{"__typename":"ProductType"'
    r',"id":"(?:\d+)"'
    r'.*?"status":"([^"]*)"'
    r'.*?"price":\{"__typename":"PriceType","actual":([\d.]+),"unitPrice":([\d.]+),"unitOfMeasure":"([^"]*)"'
    r'.*?"title":"([^"]+)"'
    r'.*?"brandName":"([^"]*)"'
    r'.*?"defaultImageUrl":"([^"]*)"',
    re.DOTALL,
)

_lock = threading.Lock()
_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    with _lock:
        if _session is None:
            _session = requests.Session()
            _session.headers.update(_HEADERS)
    return _session


def _unescape(s: str) -> str:
    """Decode \u002F style unicode escapes without full JSON parsing."""
    return s.encode("utf-8").decode("unicode_escape", errors="replace")


def _parse_products(html: str) -> list[ProductResult]:
    results = []
    for m in _PRODUCT_RE.finditer(html):
        pid, status, price_str, unit_price_str, unit, title, brand, img_raw = m.groups()
        try:
            price = float(price_str)
            unit_price = float(unit_price_str)
        except ValueError:
            continue
        image_url = _unescape(img_raw) if img_raw else None
        in_stock = status == "AvailableForSale"
        results.append(ProductResult(
            external_id=pid,
            name=title,
            url=_PRODUCT_URL.format(id=pid),
            price=price,
            unit_price=unit_price,
            unit=unit or None,
            image_url=image_url,
            in_stock=in_stock,
            brand=brand or None,
        ))
    return results


class TescoAdapter(BaseAdapter):
    retailer_key = "tesco"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _get_session().get(
                _SEARCH_URL,
                params={"query": query, "format": "json"},
                timeout=15,
            )
            resp.raise_for_status()
            return _parse_products(resp.text)
        except Exception:
            return []

    def fetch_price(self, external_id: str) -> ProductResult | None:
        # Product page also embeds Apollo cache — reuse same parsing
        try:
            resp = _get_session().get(
                _PRODUCT_URL.format(id=external_id),
                params={"format": "json"},
                timeout=15,
            )
            resp.raise_for_status()
            results = _parse_products(resp.text)
            return next((r for r in results if r.external_id == external_id), None)
        except Exception:
            return None
