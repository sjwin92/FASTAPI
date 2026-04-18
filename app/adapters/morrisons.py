from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from app.adapters.base import RetailerInfo


@dataclass(frozen=True)
class MorrisonsParsedProduct:
    external_id: str
    name: str
    price_gbp: float
    product_url: str
    brand: str | None = None
    image_url: str | None = None


class MorrisonsAdapter:
    info = RetailerInfo(key="morrisons", name="Morrisons", scraping_implemented=True)
    base_url = "https://groceries.morrisons.com"

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
        return parsed.netloc.endswith("groceries.morrisons.com") or parsed.netloc.endswith("morrisons.com")

    def search_products(self, query: str, limit: int = 20) -> list[MorrisonsParsedProduct]:
        search_url = f"{self.base_url}/search?entry={quote_plus(query)}"
        response = self.session.get(search_url, timeout=self.timeout)
        response.raise_for_status()

        links = set(re.findall(r'href=["\'](/products/[^"\'#?]+)', response.text))
        products: list[MorrisonsParsedProduct] = []
        for path in links:
            url = urljoin(self.base_url, path)
            try:
                products.append(self.fetch_product(url))
            except Exception:
                continue
            if len(products) >= limit:
                break

        return products

    def fetch_product(self, product_url: str) -> MorrisonsParsedProduct:
        response = self.session.get(product_url, timeout=self.timeout)
        response.raise_for_status()
        return self.parse_product_page(response.text, product_url)

    def parse_product_page(self, html: str, url: str) -> MorrisonsParsedProduct:
        product_data = self._extract_ld_json_product(html)
        if product_data is None:
            product_data = self._extract_preloaded_state_product(html)
        if product_data is None:
            raise ValueError("Unable to parse Morrisons product payload from HTML")

        price_raw = product_data.get("price") or product_data.get("currentPrice")
        if price_raw is None:
            raise ValueError("Morrisons parser could not locate product price")

        external_id = str(product_data.get("sku") or product_data.get("productId") or self._external_id_from_url(url))
        return MorrisonsParsedProduct(
            external_id=external_id,
            name=str(product_data.get("name") or "Unknown product").strip(),
            price_gbp=float(price_raw),
            product_url=url,
            image_url=self._normalize_image(product_data.get("image") or product_data.get("imageUrl")),
            brand=self._normalize_brand(product_data.get("brand")),
        )

    @staticmethod
    def _extract_ld_json_product(html: str) -> dict[str, Any] | None:
        scripts = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        for raw in scripts:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue

            product = MorrisonsAdapter._find_product_node(payload)
            if product is not None:
                return product
        return None

    @staticmethod
    def _extract_preloaded_state_product(html: str) -> dict[str, Any] | None:
        match = re.search(
            r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;",
            html,
            flags=re.DOTALL,
        )
        if not match:
            return None

        try:
            state = json.loads(match.group(1))
        except json.JSONDecodeError:
            return None

        if isinstance(state, dict):
            product = state.get("product") or state.get("currentProduct")
            if isinstance(product, dict):
                return product
        return None

    @staticmethod
    def _find_product_node(payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            entry_type = payload.get("@type")
            if entry_type == "Product" or (isinstance(entry_type, list) and "Product" in entry_type):
                return payload

            graph = payload.get("@graph")
            if isinstance(graph, list):
                for node in graph:
                    if not isinstance(node, dict):
                        continue
                    node_type = node.get("@type")
                    if node_type == "Product" or (isinstance(node_type, list) and "Product" in node_type):
                        return node

        if isinstance(payload, list):
            for item in payload:
                product = MorrisonsAdapter._find_product_node(item)
                if product is not None:
                    return product

        return None

    @staticmethod
    def _normalize_image(image: Any) -> str | None:
        if isinstance(image, str):
            return image
        if isinstance(image, list) and image and isinstance(image[0], str):
            return image[0]
        return None

    @staticmethod
    def _normalize_brand(brand: Any) -> str | None:
        if isinstance(brand, str):
            return brand
        if isinstance(brand, dict):
            name = brand.get("name")
            return str(name) if name else None
        return None

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        return parts[-1] if parts else parsed.path
