from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from app.adapters.base import RetailerInfo


@dataclass(frozen=True)
class TescoParsedProduct:
    external_id: str
    name: str
    price_gbp: float
    product_url: str
    brand: str | None = None
    image_url: str | None = None


class TescoAdapter:
    info = RetailerInfo(key="tesco", name="Tesco", scraping_implemented=True)
    base_url = "https://www.tesco.com"

    def __init__(self, timeout: int = 15) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (compatible; PriceTrackerBot/1.0)",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )

    def is_supported_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("tesco.com")

    def search_products(self, query: str, limit: int = 20) -> list[TescoParsedProduct]:
        search_url = f"{self.base_url}/groceries/en-GB/search?query={quote_plus(query)}"
        response = self.session.get(search_url, timeout=self.timeout)
        response.raise_for_status()

        products: list[TescoParsedProduct] = []
        for node in self._extract_ld_json_products(response.text):
            parsed = self._parsed_from_node(node, fallback_url=search_url)
            if parsed is not None:
                products.append(parsed)
            if len(products) >= limit:
                break
        return products

    def fetch_product(self, product_url: str) -> TescoParsedProduct:
        response = self.session.get(product_url, timeout=self.timeout)
        response.raise_for_status()
        products = self._extract_ld_json_products(response.text)
        if not products:
            raise ValueError("Tesco page does not contain Product structured data")

        parsed = self._parsed_from_node(products[0], fallback_url=product_url)
        if parsed is None:
            raise ValueError("Tesco parser could not map product data")
        return parsed

    def _parsed_from_node(self, node: dict[str, Any], fallback_url: str) -> TescoParsedProduct | None:
        offers = node.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        if not isinstance(offers, dict):
            offers = {}

        raw_price = offers.get("price") or node.get("price")
        if raw_price is None:
            return None

        url = str(node.get("url") or fallback_url)
        url = urljoin(self.base_url, url)
        external_id = str(node.get("sku") or node.get("productID") or self._external_id_from_url(url))

        return TescoParsedProduct(
            external_id=external_id,
            name=str(node.get("name") or "Unknown Product").strip(),
            price_gbp=float(raw_price),
            product_url=url,
            brand=self._normalize_brand(node.get("brand")),
            image_url=self._normalize_image(node.get("image")),
        )

    @staticmethod
    def _extract_ld_json_products(html: str) -> list[dict[str, Any]]:
        scripts = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

        products: list[dict[str, Any]] = []
        for raw in scripts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            products.extend(TescoAdapter._find_product_nodes(payload))
        return products

    @staticmethod
    def _find_product_nodes(payload: Any) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            entry_type = payload.get("@type")
            if entry_type == "Product" or (isinstance(entry_type, list) and "Product" in entry_type):
                found.append(payload)

            graph = payload.get("@graph")
            if isinstance(graph, list):
                for item in graph:
                    found.extend(TescoAdapter._find_product_nodes(item))

            item_list = payload.get("itemListElement")
            if isinstance(item_list, list):
                for item in item_list:
                    if isinstance(item, dict):
                        found.extend(TescoAdapter._find_product_nodes(item.get("item")))

        elif isinstance(payload, list):
            for item in payload:
                found.extend(TescoAdapter._find_product_nodes(item))

        return found

    @staticmethod
    def _normalize_brand(brand: Any) -> str | None:
        if isinstance(brand, str):
            return brand.strip() or None
        if isinstance(brand, dict):
            name = brand.get("name")
            if isinstance(name, str):
                return name.strip() or None
        return None

    @staticmethod
    def _normalize_image(image: Any) -> str | None:
        if isinstance(image, str):
            return image
        if isinstance(image, list) and image and isinstance(image[0], str):
            return image[0]
        return None

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else parsed.path
