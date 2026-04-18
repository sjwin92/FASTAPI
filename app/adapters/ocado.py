import re
import requests
from .base import BaseAdapter, ProductResult

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PriceTracker/1.0)",
    "Accept": "application/json",
})

_PRODUCT_URL = "https://www.ocado.com/products/{id}"
_SEARCH_API = "https://www.ocado.com/search?entry={query}&maximumNumberOfResults=10"


def _parse_price(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"[\d.]+", str(raw))
    return float(match.group()) if match else None


class OcadoAdapter(BaseAdapter):
    retailer_key = "ocado"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _SESSION.get(
                "https://api.ocado.com/products/search",
                params={"q": query, "size": 10},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("products", data.get("results", []))
        except Exception:
            return []

        results = []
        for item in items:
            external_id = str(item.get("id", item.get("sku", "")))
            if not external_id:
                continue
            price = _parse_price(item.get("price", {}).get("current") if isinstance(item.get("price"), dict) else item.get("price"))
            if price is None:
                continue
            price_obj = item.get("price", {})
            unit_price = _parse_price(price_obj.get("unitPrice") if isinstance(price_obj, dict) else None)
            unit = price_obj.get("unitOfMeasure") if isinstance(price_obj, dict) else None
            results.append(ProductResult(
                external_id=external_id,
                name=item.get("name", item.get("title", "")),
                url=_PRODUCT_URL.format(id=external_id),
                price=price,
                unit_price=unit_price,
                unit=unit,
                image_url=item.get("imageUrl", item.get("image")),
                in_stock=item.get("availability", "IN_STOCK") == "IN_STOCK",
            ))
        return results

    def fetch_price(self, external_id: str) -> ProductResult | None:
        try:
            resp = _SESSION.get(
                f"https://api.ocado.com/products/{external_id}",
                headers={"Accept": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            item = resp.json()
        except Exception:
            return None

        price_obj = item.get("price", {})
        price = _parse_price(price_obj.get("current") if isinstance(price_obj, dict) else price_obj)
        if price is None:
            return None

        return ProductResult(
            external_id=external_id,
            name=item.get("name", item.get("title", "")),
            url=_PRODUCT_URL.format(id=external_id),
            price=price,
            unit_price=_parse_price(price_obj.get("unitPrice") if isinstance(price_obj, dict) else None),
            unit=price_obj.get("unitOfMeasure") if isinstance(price_obj, dict) else None,
            image_url=item.get("imageUrl", item.get("image")),
            in_stock=item.get("availability", "IN_STOCK") == "IN_STOCK",
        )
