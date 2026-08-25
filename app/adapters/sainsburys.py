"""
Sainsbury's adapter — public-facing product API, no auth, no basket/checkout/account flows.

How the session works
---------------------
The gol-ui frontend is a pure React SPA; there is no server-side rendered HTML.
Product data comes from an internal REST API at:

    /groceries-api/gol-services/product/v1/product

Sainsbury's protects this API via Akamai Bot Manager.  A plain request with no
cookies is rejected with 403.  However, a prior GET to the homepage — which also
returns 403 for automated clients — causes Akamai to issue a set of session
cookies in the response headers without requiring JavaScript execution:

    akaas_gol_random, akavpau_vpc_gol_default, bm_ss, _abck, bm_s, bm_so, bm_sz

The API then accepts requests that carry those cookies, returning full product JSON.
If the API returns a 4xx after cookie seeding, the session is re-seeded once.

external_id is product_uid (e.g. "7260789"), a stable numeric string.
Product page URL is taken from the full_url field returned by the API.

Known limitations
-----------------
- The Akamai challenge may change at any time; the re-seed strategy is best-effort.
- IP-level blocks (data-centre egress) will cause all calls to return []/None.
- All failures are caught and return [] / None gracefully.
"""

from __future__ import annotations

import re
import threading
from typing import Any

import requests

from .base import BaseAdapter, ProductResult
from .http import REQUEST_TIMEOUT, create_session

_BASE = "https://www.sainsburys.co.uk"
_HOME = f"{_BASE}/"
_SEARCH_API = f"{_BASE}/groceries-api/gol-services/product/v1/product"
_PRODUCT_API = f"{_BASE}/groceries-api/gol-services/product/v1/product/{{uid}}"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_COMMON_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}
_NAV_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
_API_HEADERS = {
    **_COMMON_HEADERS,
    "Accept": "application/json",
    "Origin": _BASE,
    "Referer": f"{_BASE}/gol-ui/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

# Size units extracted from product names, e.g. "500ml", "1kg", "2 ltr"
_SIZE_RE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:ml|cl|l|ltr|litre|litres|g|kg|oz|pints?|pack(?:\s+of\s+\d+)?))\b",
    re.IGNORECASE,
)


def _parse_price(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = re.search(r"[\d]+\.[\d]+|[\d]+", str(raw))
    return float(m.group()) if m else None


def _extract_size(name: str) -> str | None:
    """Return the last size-like token found in the product name."""
    matches = _SIZE_RE.findall(name)
    return matches[-1].strip() if matches else None


def _best_image(item: dict) -> str | None:
    """Pick the best available image URL from the product dict."""
    # Prefer the assets CDN image (cleaner URLs, known to be stable)
    assets = item.get("assets") or {}
    plp = assets.get("plp_image")
    if plp:
        return plp
    # Fall back to image sizes — pick 640×640 if available
    for img_entry in assets.get("images") or []:
        for size in img_entry.get("sizes") or []:
            if size.get("width") == 640:
                return size["url"]
    # Last resort: the top-level image field
    return item.get("image") or item.get("image_thumbnail")


def _parse_product(item: dict) -> ProductResult | None:
    """Convert a raw API product dict to a ProductResult.  Returns None if
    any required field is absent or the price cannot be parsed."""
    uid = str(item.get("product_uid", "")).strip()
    name = (item.get("name") or "").strip()
    if not uid or not name:
        return None

    retail = item.get("retail_price") or {}
    price = _parse_price(retail.get("price"))
    if price is None:
        return None

    unit_p = item.get("unit_price") or {}
    unit_price = _parse_price(unit_p.get("price"))
    measure = (unit_p.get("measure") or "").strip()
    unit = measure or None

    unit_price_text: str | None = None
    if unit_price is not None and unit:
        amount = unit_p.get("measure_amount")
        amt_str = f"{amount}" if amount and amount != 1 else ""
        unit_price_text = f"£{unit_price:.2f}/{amt_str}{unit}"

    promos: list[dict] = item.get("promotions") or []
    promo_text = promos[0].get("strap_line") if promos else None

    brands: list[str] = (item.get("attributes") or {}).get("brand") or []
    brand = brands[0] if brands else None

    full_url = item.get("full_url") or f"{_BASE}/gol-ui/product/{uid}"

    return ProductResult(
        external_id=uid,
        name=name,
        url=full_url,
        price=price,
        unit_price=unit_price,
        unit=unit,
        image_url=_best_image(item),
        in_stock=bool(item.get("is_available", True)),
        brand=brand,
        size_text=_extract_size(name),
        unit_price_text=unit_price_text,
        promo_text=promo_text,
    )


class SainsburysAdapter(BaseAdapter):
    """Adapter for Sainsbury's groceries-api."""

    retailer_key = "sainsburys"

    def __init__(self) -> None:
        self._session = create_session({})
        self._seeded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _seed_session(self) -> None:
        """Hit the homepage to obtain Akamai session cookies.

        The response will be 403, but the Set-Cookie headers are returned
        regardless and are sufficient for subsequent API calls.
        """
        try:
            self._session.get(_HOME, headers=_NAV_HEADERS, timeout=REQUEST_TIMEOUT)
        except Exception:
            pass
        self._seeded = True

    def _ensure_session(self) -> None:
        with self._lock:
            if not self._seeded:
                self._seed_session()

    def _get(self, url: str, params: dict | None = None) -> requests.Response | None:
        """Make an API GET, re-seeding the session once on 403."""
        self._ensure_session()
        try:
            resp = self._session.get(url, params=params, headers=_API_HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 403:
                # Cookie set may have expired — re-seed once and retry
                with self._lock:
                    self._seeded = False
                    self._seed_session()
                resp = self._session.get(url, params=params, headers=_API_HEADERS, timeout=REQUEST_TIMEOUT)
            return resp
        except Exception:
            return None

    # ------------------------------------------------------------------
    # BaseAdapter interface
    # ------------------------------------------------------------------

    def search(self, query: str) -> list[ProductResult]:
        resp = self._get(_SEARCH_API, params={"filter[keyword]": query, "pageSize": "20"})
        if resp is None or resp.status_code != 200:
            return self._mark_unavailable("http_error")
        try:
            data = resp.json()
        except Exception:
            return self._mark_unavailable("invalid_response")

        results: list[ProductResult] = []
        for item in data.get("products") or []:
            result = _parse_product(item)
            if result is not None:
                results.append(result)
        return results

    def fetch_price(self, external_id: str) -> ProductResult | None:
        resp = self._get(_PRODUCT_API.format(uid=external_id))
        if resp is None or resp.status_code != 200:
            return None
        try:
            item = resp.json()
        except Exception:
            return None
        return _parse_product(item)
