"""
Morrisons adapter.

The Morrisons search page embeds full product state in a large JS object.
Product names + prices appear in adjacent catalog entries.
Product numeric IDs appear in the embedded URL list (same order).

Approach:
  1. Extract (name, price, unit) tuples from catalog entries in order
  2. Extract (slug, numeric_id) from embedded /products/{slug}/{id} URLs in order
  3. Zip them by position (both lists appear in search-result order)
"""
from __future__ import annotations

import re
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

_SEARCH_URL = "https://groceries.morrisons.com/search"
_PRODUCT_BASE = "https://groceries.morrisons.com"

# Catalog entry: name immediately followed by price
_CATALOG_RE = re.compile(
    r'"name":"([^"]+)","price":\{"current":\{"amount":"([\d.]+)","currency":"GBP"\}'
    r'(?:,"unit":\{"label":"([^"]*)"[^}]*"amount":"([\d.]+)")?'
)

# Product URLs embedded in the page
_URL_RE = re.compile(r'href="/products/([a-z0-9-]+)/(\d+)"')

# Image URLs for products — 300x300 versions from Morrisons CDN
_IMG_RE = re.compile(r'"src":"(https://groceries\.morrisons\.com/images[^"]+300x300\.jpg)"')

_session = requests.Session()
_session.headers.update(_HEADERS)


def _unit_label_to_unit(label: str) -> str | None:
    if not label:
        return None
    parts = label.split(".")
    return parts[-1] if parts else None


def _parse_products(html: str) -> list[ProductResult]:
    catalog = _CATALOG_RE.findall(html)
    urls = list(dict.fromkeys(_URL_RE.findall(html)))  # preserve order, deduplicate
    images = _IMG_RE.findall(html)

    results = []
    for i, (name, price_str, unit_label, unit_amount_str) in enumerate(catalog):
        try:
            price = float(price_str)
        except ValueError:
            continue

        slug, pid = urls[i] if i < len(urls) else ("", "")
        if not pid:
            continue

        unit_price = None
        if unit_amount_str:
            try:
                # Morrisons stores unit price in GBX (pence) — convert to GBP
                unit_price = float(unit_amount_str) / 100
            except ValueError:
                pass

        unit = _unit_label_to_unit(unit_label)
        image_url = images[i] if i < len(images) else None

        results.append(ProductResult(
            external_id=pid,
            name=name,
            url=f"{_PRODUCT_BASE}/products/{slug}/{pid}",
            price=price,
            unit_price=unit_price,
            unit=unit,
            image_url=image_url,
            in_stock=True,  # Non-available products don't appear in search results
        ))
    return results


class MorrisonsAdapter(BaseAdapter):
    retailer_key = "morrisons"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _session.get(_SEARCH_URL, params={"entry": query}, timeout=15)
            resp.raise_for_status()
            return _parse_products(resp.text)
        except Exception:
            return []

    def fetch_price(self, external_id: str) -> ProductResult | None:
        try:
            resp = _session.get(
                f"{_PRODUCT_BASE}/products/product/{external_id}",
                timeout=15,
            )
            resp.raise_for_status()
            results = _parse_products(resp.text)
            return next((r for r in results if r.external_id == external_id), None)
        except Exception:
            return None
