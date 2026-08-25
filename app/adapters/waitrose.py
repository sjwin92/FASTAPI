"""
Waitrose adapter — public pages only, no auth, no basket/checkout/account flows.

Extraction strategy (in order of preference):
  1. schema.org ld+json   — present on product pages, most stable surface
  2. __NEXT_DATA__ JSON   — embedded in all Waitrose Next.js pages

external_id is stored as the full URL path slug, e.g.
  "essential-semi-skimmed-milk/0-331187-44330-00"
This lets fetch_price reconstruct the exact product URL without a DB lookup.

Known limitation: Waitrose's Akamai CDN TCP-drops server-side requests
(connects then never responds). Timeout is set to 8s so the adapter fails
fast rather than blocking basket/compare for 15s.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import BaseAdapter, ProductResult
from .http import REQUEST_TIMEOUT, create_session

_BASE_URL = "https://www.waitrose.com"
_PRODUCT_BASE = f"{_BASE_URL}/ecom/products"
_SEARCH_URL = f"{_BASE_URL}/ecom/shop/search"

_SESSION = create_session({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
})

# Matches <script type="application/ld+json">…</script>  (DOTALL)
_LD_JSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Matches <script id="__NEXT_DATA__" …>…</script>
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)

# Ordered list of key-paths to try when walking __NEXT_DATA__ for search results.
# Waitrose's Next.js data shape may vary across deployments; we try each in turn.
_SEARCH_PATHS: list[list[str]] = [
    ["props", "pageProps", "initialSearchResults", "products"],
    ["props", "pageProps", "searchResults", "products"],
    ["props", "pageProps", "initialData", "searchResults", "products"],
    ["props", "pageProps", "initialSearch", "products"],
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"[\d]+\.[\d]+|[\d]+", str(raw))
    return float(m.group()) if m else None


def _extract_ld_json(html: str) -> list[dict]:
    blocks: list[dict] = []
    for m in _LD_JSON_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
            # Handle both single objects and @graph arrays
            if isinstance(data, list):
                blocks.extend(data)
            elif isinstance(data, dict):
                if data.get("@graph"):
                    blocks.extend(data["@graph"])
                else:
                    blocks.append(data)
        except (json.JSONDecodeError, ValueError):
            pass
    return blocks


def _extract_next_data(html: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return {}


def _walk(obj: Any, keys: list[str]) -> Any:
    """Safely walk a nested dict/list using a list of string keys."""
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def _products_from_next_data(data: dict) -> list[dict]:
    for path in _SEARCH_PATHS:
        products = _walk(data, path)
        if isinstance(products, list) and products:
            return products
    return []


def _result_from_next_item(item: dict) -> ProductResult | None:
    """Parse a single product dict from Waitrose __NEXT_DATA__ search results."""
    name = item.get("name", "").strip()
    product_id = str(item.get("id", item.get("productId", ""))).strip()
    if not name or not product_id:
        return None

    # Price — Waitrose stores as {price: {amount: 1.65, currency: "GBP"}}
    sale_unit = item.get("currentSaleUnitPrice") or {}
    price_amount = _walk(sale_unit, ["price", "amount"])
    price = _parse_price(price_amount)
    if price is None:
        return None

    # Unit price (retail price per measure)
    retail_unit = item.get("currentSaleUnitRetailPrice") or {}
    up_amount = _walk(retail_unit, ["price", "amount"])
    unit_price = _parse_price(up_amount)
    unit = retail_unit.get("measure") if isinstance(retail_unit, dict) else None
    unit_price_text = f"£{up_amount}/{unit}" if up_amount and unit else None

    # Promo
    promos: list[dict] = item.get("promotions") or []
    promo_text = promos[0].get("promotionDescription") if promos else None

    # Brand / size
    brand_obj = item.get("brand")
    brand = brand_obj.get("name") if isinstance(brand_obj, dict) else brand_obj
    size_text = item.get("size") or item.get("sizeDescription")

    # Image
    thumbnail = item.get("thumbnail")
    if isinstance(thumbnail, dict):
        assets = thumbnail.get("assets") or []
        image_url = assets[0].get("url") if assets else thumbnail.get("url")
    else:
        image_url = thumbnail

    # URL — Waitrose embeds a relative path like "/ecom/products/name/0-id"
    url_path = item.get("url") or ""
    _PRODUCTS_PREFIX = "/ecom/products/"
    if url_path.startswith("/"):
        url = f"{_BASE_URL}{url_path}"
        external_id = url_path[len(_PRODUCTS_PREFIX):] if url_path.startswith(_PRODUCTS_PREFIX) else url_path.lstrip("/")
    else:
        slug = name.lower().replace(" ", "-")
        external_id = f"{slug}/{product_id}"
        url = f"{_PRODUCT_BASE}/{external_id}"

    # Availability
    display_info = item.get("displayInfo") or {}
    availability = display_info.get("availabilityType") or display_info.get("availability", "")
    in_stock = "OutOfStock" not in availability and availability != "UNAVAILABLE"

    return ProductResult(
        external_id=external_id,
        name=name,
        url=url,
        price=price,
        unit_price=unit_price,
        unit=unit,
        image_url=image_url,
        in_stock=in_stock,
        brand=brand,
        size_text=size_text,
        unit_price_text=unit_price_text,
        promo_text=promo_text,
    )


def _result_from_ld_json(blocks: list[dict], page_url: str) -> ProductResult | None:
    """Parse the first schema.org Product block from ld+json."""
    for block in blocks:
        if block.get("@type") != "Product":
            continue

        name = block.get("name", "").strip()
        if not name:
            continue

        offers = block.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        price = _parse_price(offers.get("price"))
        if price is None:
            continue

        # Availability: schema.org uses full URIs like "https://schema.org/InStock"
        avail_uri = offers.get("availability", "")
        in_stock = "OutOfStock" not in avail_uri

        brand_obj = block.get("brand") or {}
        brand = brand_obj.get("name") if isinstance(brand_obj, dict) else brand_obj

        images = block.get("image") or []
        if isinstance(images, str):
            images = [images]
        image_url = images[0] if images else None

        # external_id = the path after /ecom/products/ in the page URL
        external_id = re.sub(r"^https?://[^/]+/ecom/products/", "", page_url).rstrip("/")
        if not external_id:
            external_id = page_url

        # Unit price text from description or offers
        unit_price_text: str | None = None
        price_spec = offers.get("priceSpecification")
        if isinstance(price_spec, list) and price_spec:
            price_spec = price_spec[0]
        if isinstance(price_spec, dict):
            up = _parse_price(price_spec.get("price"))
            ref = price_spec.get("referenceQuantity", {})
            ref_unit = ref.get("unitText") if isinstance(ref, dict) else None
            if up and ref_unit:
                unit_price_text = f"£{up}/{ref_unit}"

        return ProductResult(
            external_id=external_id,
            name=name,
            url=page_url,
            price=price,
            unit_price=None,
            unit=None,
            image_url=image_url,
            in_stock=in_stock,
            brand=brand,
            unit_price_text=unit_price_text,
        )
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class WaitroseAdapter(BaseAdapter):
    retailer_key = "waitrose"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _SESSION.get(
                _SEARCH_URL,
                params={"searchTerm": query},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return self._mark_unavailable()

        # Try __NEXT_DATA__ first — richer data including promo/brand/size
        next_data = _extract_next_data(html)
        raw_products = _products_from_next_data(next_data)
        if raw_products:
            results = []
            for item in raw_products:
                r = _result_from_next_item(item)
                if r is not None:
                    results.append(r)
            return results

        # Fall back: collect any ld+json Product blocks embedded in the page
        blocks = _extract_ld_json(html)
        product_blocks = [b for b in blocks if b.get("@type") == "Product"]
        if product_blocks:
            page_url = resp.url
            result = _result_from_ld_json(product_blocks, page_url)
            return [result] if result else []

        return []

    def fetch_price(self, external_id: str) -> ProductResult | None:
        url = f"{_PRODUCT_BASE}/{external_id}"
        try:
            resp = _SESSION.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return None

        # ld+json is the most reliable surface on individual product pages
        blocks = _extract_ld_json(html)
        result = _result_from_ld_json(blocks, url)
        if result:
            return result

        # Fall back to __NEXT_DATA__ for product detail pages
        next_data = _extract_next_data(html)
        # Product detail page typically stores data at props.pageProps.product
        item = _walk(next_data, ["props", "pageProps", "product"])
        if isinstance(item, dict):
            return _result_from_next_item(item)

        return None
