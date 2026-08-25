"""
Morrisons adapter.

The Morrisons search page embeds full product state in a large JS object.
Product names + prices appear in adjacent catalog entries.
Each rendered product card contains its name and URL together.

Approach:
  1. Extract (name, price, unit) tuples from catalog entries in order
  2. Extract (name, slug, numeric_id) from each rendered product card
  3. Join on a normalised name, failing closed when no exact association exists
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from html import unescape

from .base import BaseAdapter, ProductResult
from .http import REQUEST_TIMEOUT, create_session

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

# The accessible product name is nested directly inside its product-card anchor.
# Keeping these fields in one regex prevents the historical positional zip bug.
_CARD_LINK_RE = re.compile(
    r'href="/products/([a-z0-9-]+)/(\d+)"[^>]*>'
    r'\s*<span class="salt-vc">([^<]+)</span>',
    re.IGNORECASE,
)

_session = create_session(_HEADERS)


def _unit_label_to_unit(label: str) -> str | None:
    if not label:
        return None
    parts = label.split(".")
    return parts[-1] if parts else None


def _name_key(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", unescape(value).lower()))


def _parse_products(html: str) -> list[ProductResult]:
    catalog = _CATALOG_RE.findall(html)
    links_by_name: dict[str, deque[tuple[str, str]]] = defaultdict(deque)
    seen_links: set[tuple[str, str]] = set()
    for slug, product_id, card_name in _CARD_LINK_RE.findall(html):
        link = (slug, product_id)
        if link not in seen_links:
            links_by_name[_name_key(card_name)].append(link)
            seen_links.add(link)

    results = []
    for name, price_str, unit_label, unit_amount_str in catalog:
        try:
            price = float(price_str)
        except ValueError:
            continue

        matching_links = links_by_name.get(_name_key(name))
        if not matching_links:
            continue
        slug, pid = matching_links.popleft()

        unit_price = None
        if unit_amount_str:
            try:
                # Morrisons stores unit price in GBX (pence) — convert to GBP
                unit_price = float(unit_amount_str) / 100
            except ValueError:
                pass

        unit = _unit_label_to_unit(unit_label)
        results.append(ProductResult(
            external_id=pid,
            name=name,
            url=f"{_PRODUCT_BASE}/products/{slug}/{pid}",
            price=price,
            unit_price=unit_price,
            unit=unit,
            # Omit an image rather than risk the same positional-association bug.
            image_url=None,
            in_stock=True,  # Non-available products don't appear in search results
        ))
    return results


class MorrisonsAdapter(BaseAdapter):
    retailer_key = "morrisons"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _session.get(_SEARCH_URL, params={"entry": query}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return _parse_products(resp.text)
        except Exception:
            return self._mark_unavailable()

    def fetch_price(self, external_id: str) -> ProductResult | None:
        try:
            resp = _session.get(
                f"{_PRODUCT_BASE}/products/product/{external_id}",
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            results = _parse_products(resp.text)
            return next((r for r in results if r.external_id == external_id), None)
        except Exception:
            return None
