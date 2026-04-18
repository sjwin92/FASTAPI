"""
Tests for the Waitrose adapter.

All HTTP calls are mocked — no live network requests. Fixtures are built from
the known Waitrose Next.js page structure and schema.org ld+json conventions.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.waitrose import (
    WaitroseAdapter,
    _extract_ld_json,
    _extract_next_data,
    _products_from_next_data,
    _result_from_ld_json,
    _result_from_next_item,
)

# ---------------------------------------------------------------------------
# HTML fixture helpers
# ---------------------------------------------------------------------------

def _html_with_next_data(payload: dict) -> str:
    blob = json.dumps(payload)
    return f'<html><head><script id="__NEXT_DATA__" type="application/json">{blob}</script></head><body></body></html>'


def _html_with_ld_json(*blocks: dict) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


# ---------------------------------------------------------------------------
# Shared fixture data
# ---------------------------------------------------------------------------

_PRODUCT_ITEM = {
    "id": "331187",
    "name": "Essential Semi-Skimmed Milk",
    "size": "6 pints/3.41L",
    "brand": {"name": "Waitrose"},
    "currentSaleUnitPrice": {"price": {"amount": 1.65, "currency": "GBP"}},
    "currentSaleUnitRetailPrice": {"price": {"amount": 0.48}, "measure": "per litre"},
    "promotions": [{"promotionDescription": "Was £1.85"}],
    "thumbnail": {"assets": [{"url": "https://ecom.waitrose.com/img/p/milk.jpg"}]},
    "url": "/ecom/products/essential-semi-skimmed-milk/0-331187-44330-00",
    "displayInfo": {"availabilityType": "InStock"},
}

_NEXT_DATA_SEARCH = {
    "props": {
        "pageProps": {
            "initialSearchResults": {
                "products": [_PRODUCT_ITEM]
            }
        }
    }
}

_LD_JSON_PRODUCT = {
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "Essential Semi-Skimmed Milk 6 Pints/3.41L",
    "image": ["https://ecom.waitrose.com/img/p/milk.jpg"],
    "brand": {"@type": "Brand", "name": "Waitrose"},
    "offers": {
        "@type": "Offer",
        "price": "1.65",
        "priceCurrency": "GBP",
        "availability": "https://schema.org/InStock",
    },
}

_PRODUCT_URL = "https://www.waitrose.com/ecom/products/essential-semi-skimmed-milk/0-331187-44330-00"


# ---------------------------------------------------------------------------
# Unit tests: extraction helpers
# ---------------------------------------------------------------------------

class TestExtractNextData:
    def test_parses_embedded_json(self):
        data = {"props": {"pageProps": {"foo": "bar"}}}
        html = _html_with_next_data(data)
        assert _extract_next_data(html) == data

    def test_returns_empty_dict_when_absent(self):
        assert _extract_next_data("<html></html>") == {}

    def test_returns_empty_dict_on_malformed_json(self):
        html = '<script id="__NEXT_DATA__" type="application/json">{bad json}</script>'
        assert _extract_next_data(html) == {}


class TestExtractLdJson:
    def test_parses_single_block(self):
        blocks = _extract_ld_json(_html_with_ld_json(_LD_JSON_PRODUCT))
        assert len(blocks) == 1
        assert blocks[0]["@type"] == "Product"

    def test_parses_multiple_blocks(self):
        website = {"@type": "WebSite", "name": "Waitrose"}
        blocks = _extract_ld_json(_html_with_ld_json(website, _LD_JSON_PRODUCT))
        types = {b["@type"] for b in blocks}
        assert "Product" in types
        assert "WebSite" in types

    def test_returns_empty_when_absent(self):
        assert _extract_ld_json("<html></html>") == []

    def test_skips_malformed_blocks(self):
        html = (
            '<script type="application/ld+json">{bad}</script>'
            f'<script type="application/ld+json">{json.dumps(_LD_JSON_PRODUCT)}</script>'
        )
        blocks = _extract_ld_json(html)
        assert len(blocks) == 1


class TestProductsFromNextData:
    def test_finds_products_at_known_path(self):
        products = _products_from_next_data(_NEXT_DATA_SEARCH)
        assert len(products) == 1
        assert products[0]["name"] == "Essential Semi-Skimmed Milk"

    def test_returns_empty_for_unknown_shape(self):
        assert _products_from_next_data({"props": {"pageProps": {}}}) == []

    def test_tries_fallback_paths(self):
        data = {
            "props": {
                "pageProps": {
                    "searchResults": {"products": [_PRODUCT_ITEM]}
                }
            }
        }
        products = _products_from_next_data(data)
        assert len(products) == 1


# ---------------------------------------------------------------------------
# Unit tests: result parsers
# ---------------------------------------------------------------------------

class TestResultFromNextItem:
    def test_parses_full_item(self):
        result = _result_from_next_item(_PRODUCT_ITEM)
        assert result is not None
        assert result.name == "Essential Semi-Skimmed Milk"
        assert result.price == 1.65
        assert result.unit_price == 0.48
        assert result.unit == "per litre"
        assert result.brand == "Waitrose"
        assert result.size_text == "6 pints/3.41L"
        assert result.promo_text == "Was £1.85"
        assert result.image_url == "https://ecom.waitrose.com/img/p/milk.jpg"
        assert result.in_stock is True
        assert "essential-semi-skimmed-milk/0-331187-44330-00" in result.external_id

    def test_returns_none_when_name_missing(self):
        item = {**_PRODUCT_ITEM, "name": ""}
        assert _result_from_next_item(item) is None

    def test_returns_none_when_id_missing(self):
        item = {k: v for k, v in _PRODUCT_ITEM.items() if k != "id"}
        assert _result_from_next_item(item) is None

    def test_returns_none_when_price_missing(self):
        item = {**_PRODUCT_ITEM, "currentSaleUnitPrice": {}}
        assert _result_from_next_item(item) is None

    def test_out_of_stock(self):
        item = {**_PRODUCT_ITEM, "displayInfo": {"availabilityType": "OutOfStock"}}
        result = _result_from_next_item(item)
        assert result is not None
        assert result.in_stock is False

    def test_no_promotions(self):
        item = {**_PRODUCT_ITEM, "promotions": []}
        result = _result_from_next_item(item)
        assert result is not None
        assert result.promo_text is None


class TestResultFromLdJson:
    def test_parses_product_block(self):
        result = _result_from_ld_json([_LD_JSON_PRODUCT], _PRODUCT_URL)
        assert result is not None
        assert result.name == "Essential Semi-Skimmed Milk 6 Pints/3.41L"
        assert result.price == 1.65
        assert result.brand == "Waitrose"
        assert result.in_stock is True
        assert result.image_url == "https://ecom.waitrose.com/img/p/milk.jpg"

    def test_external_id_extracted_from_url(self):
        result = _result_from_ld_json([_LD_JSON_PRODUCT], _PRODUCT_URL)
        assert result is not None
        assert result.external_id == "essential-semi-skimmed-milk/0-331187-44330-00"

    def test_returns_none_when_no_product_block(self):
        blocks = [{"@type": "WebSite", "name": "Waitrose"}]
        assert _result_from_ld_json(blocks, _PRODUCT_URL) is None

    def test_returns_none_when_price_missing(self):
        block = {**_LD_JSON_PRODUCT, "offers": {"@type": "Offer", "priceCurrency": "GBP"}}
        assert _result_from_ld_json([block], _PRODUCT_URL) is None

    def test_out_of_stock_from_schema(self):
        block = {
            **_LD_JSON_PRODUCT,
            "offers": {**_LD_JSON_PRODUCT["offers"], "availability": "https://schema.org/OutOfStock"},
        }
        result = _result_from_ld_json([block], _PRODUCT_URL)
        assert result is not None
        assert result.in_stock is False

    def test_skips_non_product_blocks(self):
        blocks = [
            {"@type": "WebSite", "name": "Waitrose"},
            {"@type": "BreadcrumbList"},
            _LD_JSON_PRODUCT,
        ]
        result = _result_from_ld_json(blocks, _PRODUCT_URL)
        assert result is not None
        assert result.name == "Essential Semi-Skimmed Milk 6 Pints/3.41L"


# ---------------------------------------------------------------------------
# Integration tests: adapter methods with mocked HTTP
# ---------------------------------------------------------------------------

def _mock_response(html: str, status: int = 200, url: str = "") -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.text = html
    mock.url = url or _PRODUCT_URL
    mock.raise_for_status = MagicMock()
    if status >= 400:
        from requests import HTTPError
        mock.raise_for_status.side_effect = HTTPError(response=mock)
    return mock


@pytest.fixture
def adapter():
    return WaitroseAdapter()


class TestWaitroseAdapterSearch:
    def test_returns_results_from_next_data(self, adapter):
        html = _html_with_next_data(_NEXT_DATA_SEARCH)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html)
            results = adapter.search("milk")

        assert len(results) == 1
        assert results[0].name == "Essential Semi-Skimmed Milk"
        assert results[0].price == 1.65
        assert results[0].brand == "Waitrose"

    def test_falls_back_to_ld_json_when_no_next_data_products(self, adapter):
        # Page has __NEXT_DATA__ but no products in any known path
        empty_next = {"props": {"pageProps": {}}}
        html = _html_with_next_data(empty_next).replace(
            "</head>",
            f'<script type="application/ld+json">{json.dumps(_LD_JSON_PRODUCT)}</script></head>',
        )
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html)
            results = adapter.search("milk")

        assert len(results) == 1
        assert results[0].price == 1.65

    def test_returns_empty_when_no_structured_data(self, adapter):
        html = "<html><body><p>No products found</p></body></html>"
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html)
            results = adapter.search("xyznonexistent")

        assert results == []

    def test_returns_empty_on_http_error(self, adapter):
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.side_effect = Exception("connection refused")
            results = adapter.search("milk")

        assert results == []

    def test_returns_empty_on_timeout(self, adapter):
        from requests.exceptions import ReadTimeout
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.side_effect = ReadTimeout("timed out")
            results = adapter.search("milk")

        assert results == []

    def test_filters_items_with_missing_price(self, adapter):
        no_price = {**_PRODUCT_ITEM, "currentSaleUnitPrice": {}}
        data = {"props": {"pageProps": {"initialSearchResults": {"products": [
            _PRODUCT_ITEM, no_price
        ]}}}}
        html = _html_with_next_data(data)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html)
            results = adapter.search("milk")

        assert len(results) == 1


class TestWaitroseAdapterFetchPrice:
    def test_returns_result_from_ld_json(self, adapter):
        html = _html_with_ld_json(_LD_JSON_PRODUCT)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html, url=_PRODUCT_URL)
            result = adapter.fetch_price("essential-semi-skimmed-milk/0-331187-44330-00")

        assert result is not None
        assert result.price == 1.65
        assert result.in_stock is True

    def test_falls_back_to_next_data_when_no_ld_json(self, adapter):
        data = {"props": {"pageProps": {"product": _PRODUCT_ITEM}}}
        html = _html_with_next_data(data)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html, url=_PRODUCT_URL)
            result = adapter.fetch_price("essential-semi-skimmed-milk/0-331187-44330-00")

        assert result is not None
        assert result.price == 1.65

    def test_returns_none_when_price_missing_in_ld_json(self, adapter):
        block = {**_LD_JSON_PRODUCT, "offers": {"@type": "Offer", "priceCurrency": "GBP"}}
        html = _html_with_ld_json(block)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html, url=_PRODUCT_URL)
            result = adapter.fetch_price("essential-semi-skimmed-milk/0-331187-44330-00")

        assert result is None

    def test_returns_none_on_http_error(self, adapter):
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.side_effect = Exception("timeout")
            result = adapter.fetch_price("essential-semi-skimmed-milk/0-331187-44330-00")

        assert result is None

    def test_constructs_correct_url_from_external_id(self, adapter):
        html = _html_with_ld_json(_LD_JSON_PRODUCT)
        with patch("app.adapters.waitrose._SESSION") as mock_session:
            mock_session.get.return_value = _mock_response(html, url=_PRODUCT_URL)
            adapter.fetch_price("essential-semi-skimmed-milk/0-331187-44330-00")

        call_url = mock_session.get.call_args[0][0]
        assert call_url == "https://www.waitrose.com/ecom/products/essential-semi-skimmed-milk/0-331187-44330-00"
