"""
Trolley.co.uk multi-retailer adapter.

Trolley.co.uk is a UK price comparison aggregator. A single product page
shows prices from all major retailers (Asda, Tesco, Morrisons, Sainsbury's,
Waitrose, Ocado, Iceland, etc.).

Strategy:
  1. Search /search/?q={query} — parse product IDs and slugs
  2. Fetch the best-matching product page — parse the "Where To Buy" table
  3. Return results keyed by retailer slug (e.g. "asda", "waitrose")

Results are cached per query so that N parallel retailer adapters
sharing the same query only trigger one HTTP round-trip.
"""
from __future__ import annotations

import html as _html
import re
import threading

import requests

from .base import BaseAdapter, ProductResult

_BASE = "https://www.trolley.co.uk"
_SEARCH_URL = f"{_BASE}/search/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-GB,en;q=0.9",
}

# Trolley SVG title → our retailer key
_STORE_KEY: dict[str, str] = {
    "Asda": "asda",
    "Tesco": "tesco",
    "Morrisons": "morrisons",
    "Sainsbury's": "sainsburys",
    "Waitrose": "waitrose",
    "Ocado": "ocado",
    "Iceland": "iceland",
}

_session = requests.Session()
_session.headers.update(_HEADERS)

# Per-query result cache
_cache: dict[str, dict[str, list[ProductResult]]] = {}
_cache_lock = threading.Lock()

# --- Regex patterns ---

# Search results: href="/product/{slug}/{ID}"
_SEARCH_LINK_RE = re.compile(r'href="/product/([a-z0-9-]+)/([A-Z0-9]+)"')

# Product page: split comparison table into _item blocks
_ITEM_SPLIT_RE = re.compile(r'<div class="_item">')

# Within each _item block
_SVG_TITLE_RE = re.compile(r'<svg[^>]+title="([^"]+)"')
_PRICE_B_RE = re.compile(r'<b>(?:&pound;|£)([\d.]+)</b>')
_PER_ITEM_RE = re.compile(r'<div class="_per-item[^"]*">(.*?)</div>', re.DOTALL)
_OFFER_RE = re.compile(r'<div class="_product-offer">(.*?)</div>', re.DOTALL)

# Image: og:image meta tag
_OG_IMAGE_RE = re.compile(r'property="og:image"[^>]+content="([^"]+)"')

# Product name: twitter:title has format "Name | £X.XX Best Price | ..."
_TW_TITLE_RE = re.compile(r'twitter:title[^>]+content="([^|"]+)')


def _parse_product_page(
    html_text: str,
    trolley_pid: str,
    fallback_title: str,
) -> dict[str, ProductResult]:
    """Parse the Where-To-Buy table; return {retailer_key: ProductResult}."""
    # Product name
    tw_m = _TW_TITLE_RE.search(html_text)
    name = tw_m.group(1).strip() if tw_m else fallback_title

    # Image
    og_m = _OG_IMAGE_RE.search(html_text)
    image_url = og_m.group(1) if og_m else f"{_BASE}/img/product/{trolley_pid}"

    # Locate comparison-table section
    start = html_text.find('class="comparison-table"')
    if start < 0:
        return {}
    section = html_text[start : start + 6000]

    results: dict[str, ProductResult] = {}

    # Split into blocks — first chunk is before any _item div
    blocks = _ITEM_SPLIT_RE.split(section)[1:]
    for block in blocks:
        store_m = _SVG_TITLE_RE.search(block)
        if not store_m:
            continue
        store_name = _html.unescape(store_m.group(1))
        retailer_key = _STORE_KEY.get(store_name)
        if not retailer_key:
            continue

        price_m = _PRICE_B_RE.search(block)
        if not price_m:
            continue
        try:
            price = float(price_m.group(1))
        except ValueError:
            continue

        unit_price_text: str | None = None
        unit_price: float | None = None
        unit: str | None = None
        per_m = _PER_ITEM_RE.search(block)
        if per_m:
            raw = re.sub(r"<[^>]+>", "", per_m.group(1))  # strip any inner tags (e.g. <br>)
            raw = _html.unescape(raw).strip()
            unit_price_text = raw
            up_m = re.match(r"£([\d.]+)\s+per\s+(.+)", raw)
            if up_m:
                try:
                    unit_price = float(up_m.group(1))
                    unit = up_m.group(2).strip()
                except ValueError:
                    pass

        promo_text: str | None = None
        offer_m = _OFFER_RE.search(block)
        if offer_m:
            promo_text = _html.unescape(offer_m.group(1)).strip()

        results[retailer_key] = ProductResult(
            external_id=trolley_pid,
            name=name,
            url=f"{_BASE}/product/{trolley_pid}",
            price=price,
            unit_price=unit_price,
            unit=unit,
            image_url=image_url,
            in_stock=True,
            unit_price_text=unit_price_text,
            promo_text=promo_text,
        )

    return results


_OWN_BRAND_PREFIXES = frozenset([
    "tesco", "sainsburys", "morrisons", "asda", "waitrose",
    "ocado", "iceland", "aldi", "lidl", "coop",
])


def _fetch_all(query: str) -> dict[str, list[ProductResult]]:
    """Search trolley, try up to 5 product pages, return the result with best retailer coverage.

    Branded products (not retailer own-brand) tend to appear at multiple retailers,
    so we try non-own-brand slugs first, then fall back to any product.
    """
    try:
        resp = _session.get(_SEARCH_URL, params={"q": query}, timeout=15)
        resp.raise_for_status()
        search_html = resp.text
    except Exception:
        return {}

    all_links = list(dict.fromkeys(_SEARCH_LINK_RE.findall(search_html)))
    if not all_links:
        return {}

    # Prefer branded products (not retailer own-brand) since they appear at more stores
    branded = [(s, p) for s, p in all_links if s.split("-")[0] not in _OWN_BRAND_PREFIXES]
    own_brand = [(s, p) for s, p in all_links if s.split("-")[0] in _OWN_BRAND_PREFIXES]
    candidates = (branded + own_brand)[:5]

    best: dict[str, list[ProductResult]] = {}
    for slug, pid in candidates:
        try:
            prod_resp = _session.get(f"{_BASE}/product/{slug}/{pid}", timeout=15)
            prod_resp.raise_for_status()
            product_html = prod_resp.text
        except Exception:
            continue

        per_retailer = _parse_product_page(product_html, pid, slug.replace("-", " ").title())
        candidate = {key: [result] for key, result in per_retailer.items()}
        if len(candidate) > len(best):
            best = candidate
        if len(best) >= 4:
            break

    return best


def _cached_lookup(query: str) -> dict[str, list[ProductResult]]:
    with _cache_lock:
        if query not in _cache:
            _cache[query] = _fetch_all(query)
        return _cache[query]


class TrolleyRetailerAdapter(BaseAdapter):
    """Reads prices for one specific retailer from trolley.co.uk."""

    def __init__(self, key: str) -> None:
        self.retailer_key = key

    def search(self, query: str) -> list[ProductResult]:
        return _cached_lookup(query).get(self.retailer_key, [])

    def fetch_price(self, external_id: str) -> ProductResult | None:
        return None
