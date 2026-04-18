import re
import requests
from .base import BaseAdapter, ProductResult

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; PriceTracker/1.0)",
    "Accept": "application/json",
})

_SEARCH_URL = "https://www.tesco.com/groceries/en-GB/search?query={query}&count=24&page=1"
_PRODUCT_URL = "https://www.tesco.com/groceries/en-GB/products/{id}"
_API_URL = "https://www.tesco.com/groceries/en-GB/resources?resources=productsByQuery%3AproductsByQuery%3Aquery%3D{query}%26count%3D10%26page%3D1"


def _parse_price(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    match = re.search(r"[\d.]+", str(raw))
    return float(match.group()) if match else None


class TescoAdapter(BaseAdapter):
    retailer_key = "tesco"

    def search(self, query: str) -> list[ProductResult]:
        try:
            resp = _SESSION.get(
                "https://www.tesco.com/groceries/en-GB/resources",
                params={"resources": f"productsByQuery:productsByQuery:query={query}&count=10&page=1"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            items = (
                data.get("productsByQuery", {})
                    .get("data", {})
                    .get("productsByQuery", {})
                    .get("products", [])
            )
        except Exception:
            return []

        results = []
        for item in items:
            product = item.get("product", item)
            external_id = str(product.get("id", ""))
            if not external_id:
                continue
            price_raw = product.get("price", {})
            price = _parse_price(price_raw.get("actual") if isinstance(price_raw, dict) else price_raw)
            if price is None:
                continue
            unit_price = _parse_price(
                price_raw.get("unitPrice") if isinstance(price_raw, dict) else None
            )
            unit = price_raw.get("unitOfMeasure") if isinstance(price_raw, dict) else None
            results.append(ProductResult(
                external_id=external_id,
                name=product.get("title", ""),
                url=_PRODUCT_URL.format(id=external_id),
                price=price,
                unit_price=unit_price,
                unit=unit,
                image_url=product.get("defaultImageUrl"),
                in_stock=product.get("status", "Available") == "Available",
            ))
        return results

    def fetch_price(self, external_id: str) -> ProductResult | None:
        try:
            resp = _SESSION.get(
                "https://www.tesco.com/groceries/en-GB/resources",
                params={"resources": f"product:product:id={external_id}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            product = data.get("product", {}).get("data", {}).get("product", {})
        except Exception:
            return None

        if not product:
            return None

        price_raw = product.get("price", {})
        price = _parse_price(price_raw.get("actual") if isinstance(price_raw, dict) else price_raw)
        if price is None:
            return None

        return ProductResult(
            external_id=external_id,
            name=product.get("title", ""),
            url=_PRODUCT_URL.format(id=external_id),
            price=price,
            unit_price=_parse_price(price_raw.get("unitPrice") if isinstance(price_raw, dict) else None),
            unit=price_raw.get("unitOfMeasure") if isinstance(price_raw, dict) else None,
            image_url=product.get("defaultImageUrl"),
            in_stock=product.get("status", "Available") == "Available",
        )
