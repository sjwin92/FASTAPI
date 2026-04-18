from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from app.adapters.base import RetailerInfo


@dataclass(frozen=True)
class OcadoParsedProduct:
    external_id: str
    name: str
    brand: str | None
    size_text: str | None
    price_gbp: float
    unit_price_text: str | None
    promo_text: str | None
    image_url: str | None
    product_url: str
    in_stock: bool | None


class OcadoAdapter:
    info = RetailerInfo(key="ocado", name="Ocado", scraping_implemented=True)
    base_url = "https://www.ocado.com"

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
        return parsed.netloc.endswith("ocado.com")

    def search_products(self, query: str, limit: int = 20) -> list[OcadoParsedProduct]:
        search_url = f"{self.base_url}/search?entry={quote_plus(query)}"
        response = self.session.get(search_url, timeout=self.timeout)
        response.raise_for_status()
        return self.parse_search_page(response.text, search_url, limit=limit)

    def fetch_product(self, product_url: str) -> OcadoParsedProduct:
        response = self.session.get(product_url, timeout=self.timeout)
        response.raise_for_status()
        return self.parse_product_page(response.text, product_url)

    def parse_search_page(self, html: str, page_url: str, limit: int = 20) -> list[OcadoParsedProduct]:
        products: list[OcadoParsedProduct] = []
        for node in self._extract_ld_json_products(html):
            parsed = self._parsed_from_node(node, fallback_url=page_url)
            if parsed:
                products.append(parsed)
            if len(products) >= limit:
                break
        return products

    def parse_product_page(self, html: str, page_url: str) -> OcadoParsedProduct:
        products = self._extract_ld_json_products(html)
        if not products:
            raise ValueError("Ocado page does not contain parsable product structured data")

        parsed = self._parsed_from_node(products[0], fallback_url=page_url)
        if parsed is None:
            raise ValueError("Ocado parser failed to map product data")
        return parsed

    def _parsed_from_node(self, node: dict[str, Any], fallback_url: str) -> OcadoParsedProduct | None:
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
        external_id = str(
            node.get("sku")
            or node.get("productID")
            or node.get("productId")
            or self._external_id_from_url(url)
        )

        size_text = self._normalize_size_text(
            node.get("size") or node.get("quantity") or self._extract_size_from_name(str(node.get("name") or ""))
        )

        availability = str(offers.get("availability") or "")
        in_stock = None
        if availability:
            in_stock = availability.endswith("InStock")

        return OcadoParsedProduct(
            external_id=external_id,
            name=str(node.get("name") or "Unknown Product").strip(),
            brand=self._normalize_brand(node.get("brand")),
            size_text=size_text,
            price_gbp=float(raw_price),
            unit_price_text=self._normalize_text(offers.get("priceSpecification") or node.get("unitText")),
            promo_text=self._normalize_text(node.get("description")),
            image_url=self._normalize_image(node.get("image")),
            product_url=url,
            in_stock=in_stock,
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

            products.extend(OcadoAdapter._find_product_nodes(payload))

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
                    found.extend(OcadoAdapter._find_product_nodes(item))

            item_list = payload.get("itemListElement")
            if isinstance(item_list, list):
                for item in item_list:
                    if isinstance(item, dict):
                        candidate = item.get("item")
                        found.extend(OcadoAdapter._find_product_nodes(candidate))

        elif isinstance(payload, list):
            for item in payload:
                found.extend(OcadoAdapter._find_product_nodes(item))

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
    def _normalize_text(value: Any) -> str | None:
        if isinstance(value, str):
            text = value.strip()
            return text or None
        if isinstance(value, dict):
            text = value.get("name") or value.get("description") or value.get("value")
            if isinstance(text, str):
                text = text.strip()
                return text or None
        return None

    @staticmethod
    def _normalize_size_text(size: Any) -> str | None:
        if not isinstance(size, str):
            return None
        normalized = size.strip().lower()
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = normalized.replace("millilitres", "ml").replace("millilitre", "ml")
        normalized = normalized.replace("litres", "l").replace("litre", "l")
        normalized = normalized.replace("grams", "g").replace("gram", "g")
        normalized = normalized.replace("kilograms", "kg").replace("kilogram", "kg")
        normalized = normalized.replace(" x ", "x")
        return normalized or None

    @staticmethod
    def _extract_size_from_name(name: str) -> str | None:
        match = re.search(r"(\d+(?:\.\d+)?\s?(?:g|kg|ml|l|cl|x\d+))\b", name.lower())
        if not match:
            return None
        return OcadoAdapter._normalize_size_text(match.group(1))

    @staticmethod
    def _external_id_from_url(url: str) -> str:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if not parts:
            return parsed.path
        return parts[-1]
