import re
import requests
from .base import BaseAdapter, ProductResult

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PriceTracker/1.0)",
    "Accept": "application/json",
})

_PRODUCT_URL = "https://groceries.morrisons.com/products/{id}"


def _parse_price(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"[\d.]+", str(raw))
    return float(match.group()) if match else None


class MorrisonsAdapter(BaseAdapter):
    retailer_key = "morrisons"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _SESSION.get(
                "https://groceries.morrisons.com/api/products/search",
                params={"q": query, "size": 10, "page": 1},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("products", data.get("hits", data.get("results", [])))
        except Exception:
            return []

        results = []
        for item in items:
            external_id = str(item.get("id", item.get("productId", "")))
            if not external_id:
                continue
            price = _parse_price(
                item.get("price", {}).get("current")
                if isinstance(item.get("price"), dict)
                else item.get("price", item.get("currentPrice"))
            )
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
                in_stock=item.get("inStock", item.get("isAvailable", True)),
            ))
        return results

    def fetch_price(self, external_id: str) -> ProductResult | None:
        try:
            resp = _SESSION.get(
                f"https://groceries.morrisons.com/api/products/{external_id}",
                timeout=10,
            )
            resp.raise_for_status()
            item = resp.json()
        except Exception:
            return None

        price_obj = item.get("price", {})
        price = _parse_price(
            price_obj.get("current") if isinstance(price_obj, dict) else price_obj
        )
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
            in_stock=item.get("inStock", item.get("isAvailable", True)),
        )
