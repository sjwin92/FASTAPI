"""
Tests for the Sainsbury's adapter.

All HTTP calls are mocked.  Fixtures mirror the live API JSON structure
confirmed against the real /groceries-api/gol-services/product/v1/product
endpoint.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from app.adapters.sainsburys import (
    SainsburysAdapter,
    _best_image,
    _extract_size,
    _parse_price,
    _parse_product,
)

# ---------------------------------------------------------------------------
# Fixture data  — mirrors live API responses
# ---------------------------------------------------------------------------

_FULL_PRODUCT = {
    "product_uid": "7260789",
    "name": "Starbucks Caffé Latte Flavoured Milk Iced Coffee 220ml",
    "image": "https://www.sainsburys.co.uk/wcsstore/ExtendedSitesCatalogAssetStore/images/57/img_L.jpeg",
    "image_thumbnail": "https://www.sainsburys.co.uk/wcsstore/ExtendedSitesCatalogAssetStore/images/57/img_M.jpeg",
    "full_url": "https://www.sainsburys.co.uk/gol-ui/product/starbucks-caff-latte-flavoured-milk-iced-coffee-220ml",
    "unit_price": {"price": 10.0, "measure": "ltr", "measure_amount": 1},
    "retail_price": {"price": 2.2, "measure": "unit"},
    "is_available": True,
    "promotions": [
        {
            "promotion_uid": "10734698",
            "strap_line": "Buy any 3 for £3.50 with Nectar",
            "is_nectar": True,
        }
    ],
    "attributes": {"brand": ["Starbucks"]},
    "assets": {
        "plp_image": "https://assets.sainsburys-groceries.co.uk/gol/7260789/image.jpg",
        "images": [
            {
                "id": "1",
                "sizes": [
                    {"width": 300, "height": 300, "url": "https://assets.sainsburys-groceries.co.uk/gol/7260789/1/300x300.jpg"},
                    {"width": 640, "height": 640, "url": "https://assets.sainsburys-groceries.co.uk/gol/7260789/1/640x640.jpg"},
                ],
            }
        ],
        "video": [],
    },
}

_MINIMAL_PRODUCT = {
    "product_uid": "1234567",
    "name": "Sainsbury's Semi Skimmed Milk 2.27L",
    "full_url": "https://www.sainsburys.co.uk/gol-ui/product/sainsburys-semi-skimmed-milk-227l",
    "retail_price": {"price": 1.35, "measure": "unit"},
    "unit_price": {"price": 0.59, "measure": "ltr", "measure_amount": 1},
    "is_available": True,
    "promotions": [],
    "attributes": {},
    "assets": {},
}

_SEARCH_RESPONSE = {
    "products": [_FULL_PRODUCT, _MINIMAL_PRODUCT],
    "controls": {},
}

_EMPTY_SEARCH = {"products": [], "controls": {}}


def _mock_response(json_data: dict, status: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


def _mock_403() -> MagicMock:
    mock = MagicMock()
    mock.status_code = 403
    mock.text = "<HTML><TITLE>Access Denied</TITLE></HTML>"
    return mock


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

class TestParsePrice:
    def test_float_passthrough(self):
        assert _parse_price(2.20) == 2.2

    def test_int_passthrough(self):
        assert _parse_price(10) == 10.0

    def test_string_extraction(self):
        assert _parse_price("£1.99") == 1.99

    def test_none_returns_none(self):
        assert _parse_price(None) is None

    def test_non_numeric_string_returns_none(self):
        assert _parse_price("price unavailable") is None


class TestExtractSize:
    def test_ml(self):
        assert _extract_size("Sainsbury's Semi Skimmed Milk 568ml") == "568ml"

    def test_litres_with_space(self):
        assert _extract_size("Whole Milk 2.27 ltr") == "2.27 ltr"

    def test_kg(self):
        assert _extract_size("Cheddar Cheese 400g") == "400g"

    def test_pints(self):
        assert _extract_size("Whole Milk 6 pints") == "6 pints"

    def test_dual_label_keeps_derived_field_but_quantity_parser_prefers_metric(self):
        assert _extract_size("Whole Milk 1.13L (2 pint)") == "2 pint"

    def test_no_size(self):
        assert _extract_size("Organic Honey") is None

    def test_picks_last_match(self):
        # "220ml" appears at the end; that is the canonical size
        assert _extract_size("Starbucks Latte 220ml Coffee Drink 220ml") == "220ml"


class TestBestImage:
    def test_prefers_plp_image(self):
        item = {
            "assets": {"plp_image": "https://cdn.example.com/plp.jpg", "images": []},
            "image": "https://wcsstore.example.com/img.jpg",
        }
        assert _best_image(item) == "https://cdn.example.com/plp.jpg"

    def test_falls_back_to_640_size(self):
        item = {
            "assets": {
                "plp_image": None,
                "images": [
                    {"id": "1", "sizes": [
                        {"width": 300, "url": "https://cdn.example.com/300.jpg"},
                        {"width": 640, "url": "https://cdn.example.com/640.jpg"},
                    ]}
                ],
            },
            "image": "https://wcsstore.example.com/img.jpg",
        }
        assert _best_image(item) == "https://cdn.example.com/640.jpg"

    def test_falls_back_to_top_level_image(self):
        item = {"assets": {}, "image": "https://wcsstore.example.com/img.jpg"}
        assert _best_image(item) == "https://wcsstore.example.com/img.jpg"

    def test_returns_none_when_no_image(self):
        assert _best_image({}) is None


# ---------------------------------------------------------------------------
# Unit tests: _parse_product
# ---------------------------------------------------------------------------

class TestParseProduct:
    def test_full_product(self):
        result = _parse_product(_FULL_PRODUCT)
        assert result is not None
        assert result.external_id == "7260789"
        assert result.name == "Starbucks Caffé Latte Flavoured Milk Iced Coffee 220ml"
        assert result.price == 2.2
        assert result.unit_price == 10.0
        assert result.unit == "ltr"
        assert result.unit_price_text == "£10.00/ltr"
        assert result.brand == "Starbucks"
        assert result.size_text == "220ml"
        assert result.promo_text == "Buy any 3 for £3.50 with Nectar"
        assert result.image_url == "https://assets.sainsburys-groceries.co.uk/gol/7260789/image.jpg"
        assert result.in_stock is True
        assert "gol-ui/product/starbucks" in result.url

    def test_minimal_product(self):
        result = _parse_product(_MINIMAL_PRODUCT)
        assert result is not None
        assert result.external_id == "1234567"
        assert result.price == 1.35
        assert result.brand is None
        assert result.promo_text is None
        assert result.image_url is None

    def test_returns_none_when_uid_missing(self):
        item = {k: v for k, v in _FULL_PRODUCT.items() if k != "product_uid"}
        assert _parse_product(item) is None

    def test_returns_none_when_name_missing(self):
        item = {**_FULL_PRODUCT, "name": ""}
        assert _parse_product(item) is None

    def test_returns_none_when_price_missing(self):
        item = {**_FULL_PRODUCT, "retail_price": {}}
        assert _parse_product(item) is None

    def test_returns_none_when_retail_price_absent(self):
        item = {k: v for k, v in _FULL_PRODUCT.items() if k != "retail_price"}
        assert _parse_product(item) is None

    def test_out_of_stock(self):
        item = {**_FULL_PRODUCT, "is_available": False}
        result = _parse_product(item)
        assert result is not None
        assert result.in_stock is False

    def test_no_promotions(self):
        item = {**_FULL_PRODUCT, "promotions": []}
        result = _parse_product(item)
        assert result is not None
        assert result.promo_text is None

    def test_unit_price_text_with_measure_amount(self):
        item = {**_FULL_PRODUCT, "unit_price": {"price": 5.0, "measure": "100g", "measure_amount": 100}}
        result = _parse_product(item)
        assert result is not None
        assert result.unit_price_text == "£5.00/100100g"

    def test_url_fallback_when_full_url_absent(self):
        item = {k: v for k, v in _FULL_PRODUCT.items() if k != "full_url"}
        result = _parse_product(item)
        assert result is not None
        assert result.url == "https://www.sainsburys.co.uk/gol-ui/product/7260789"


# ---------------------------------------------------------------------------
# Integration tests: adapter with mocked HTTP
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter():
    return SainsburysAdapter()


def _patch_session(adapter, get_side_effect):
    """Patch the adapter's internal session.get."""
    adapter._session.get = MagicMock(side_effect=get_side_effect)
    adapter._seeded = True  # skip real cookie seeding in unit tests


class TestSainsburysAdapterSearch:
    def test_returns_parsed_results(self, adapter):
        _patch_session(adapter, [_mock_response(_SEARCH_RESPONSE)])
        results = adapter.search("milk")
        assert len(results) == 2
        assert results[0].external_id == "7260789"
        assert results[0].price == 2.2

    def test_empty_search_returns_empty_list(self, adapter):
        _patch_session(adapter, [_mock_response(_EMPTY_SEARCH)])
        results = adapter.search("xyznonexistentproduct")
        assert results == []

    def test_filters_products_with_missing_price(self, adapter):
        no_price = {k: v for k, v in _FULL_PRODUCT.items() if k != "retail_price"}
        response = {"products": [no_price, _MINIMAL_PRODUCT], "controls": {}}
        _patch_session(adapter, [_mock_response(response)])
        results = adapter.search("milk")
        assert len(results) == 1
        assert results[0].external_id == "1234567"

    def test_returns_empty_on_network_error(self, adapter):
        adapter._session.get = MagicMock(side_effect=Exception("connection refused"))
        adapter._seeded = True
        results = adapter.search("milk")
        assert results == []

    def test_returns_empty_on_non_200_status(self, adapter):
        _patch_session(adapter, [_mock_response({}, status=500)])
        results = adapter.search("milk")
        assert results == []

    def test_reseeds_and_retries_on_403(self, adapter):
        # First call → 403, second call (after reseed) → 200
        adapter._seeded = True
        call_count = 0

        def side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_403()   # API blocked
            if call_count == 2:
                return _mock_403()   # reseed homepage (ignored)
            return _mock_response(_SEARCH_RESPONSE)

        adapter._session.get = MagicMock(side_effect=side_effect)
        results = adapter.search("milk")
        assert len(results) == 2

    def test_returns_empty_when_json_invalid(self, adapter):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.side_effect = ValueError("bad json")
        _patch_session(adapter, [mock])
        results = adapter.search("milk")
        assert results == []

    def test_seeds_session_on_first_call(self, adapter):
        assert adapter._seeded is False
        seeded_calls = []

        def side_effect(url, **kwargs):
            seeded_calls.append(url)
            if "groceries-api" in url:
                return _mock_response(_SEARCH_RESPONSE)
            return _mock_403()  # homepage

        adapter._session.get = MagicMock(side_effect=side_effect)
        results = adapter.search("milk")
        # First call should be to homepage, second to API
        assert seeded_calls[0] == "https://www.sainsburys.co.uk/"
        assert "groceries-api" in seeded_calls[1]
        assert len(results) == 2


class TestSainsburysAdapterFetchPrice:
    def test_returns_result_for_known_uid(self, adapter):
        _patch_session(adapter, [_mock_response(_FULL_PRODUCT)])
        result = adapter.fetch_price("7260789")
        assert result is not None
        assert result.price == 2.2
        assert result.brand == "Starbucks"

    def test_constructs_correct_url(self, adapter):
        _patch_session(adapter, [_mock_response(_FULL_PRODUCT)])
        adapter.fetch_price("7260789")
        called_url = adapter._session.get.call_args[0][0]
        assert called_url == "https://www.sainsburys.co.uk/groceries-api/gol-services/product/v1/product/7260789"

    def test_returns_none_on_404(self, adapter):
        _patch_session(adapter, [_mock_response({}, status=404)])
        result = adapter.fetch_price("0000000")
        assert result is None

    def test_returns_none_on_missing_price(self, adapter):
        item = {k: v for k, v in _FULL_PRODUCT.items() if k != "retail_price"}
        _patch_session(adapter, [_mock_response(item)])
        result = adapter.fetch_price("7260789")
        assert result is None

    def test_returns_none_on_network_error(self, adapter):
        adapter._session.get = MagicMock(side_effect=Exception("timeout"))
        adapter._seeded = True
        result = adapter.fetch_price("7260789")
        assert result is None

    def test_returns_none_on_invalid_json(self, adapter):
        mock = MagicMock()
        mock.status_code = 200
        mock.json.side_effect = ValueError("bad json")
        _patch_session(adapter, [mock])
        result = adapter.fetch_price("7260789")
        assert result is None

    def test_reseeds_on_403(self, adapter):
        adapter._seeded = True
        call_count = 0

        def side_effect(url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_403()  # first API attempt blocked
            if call_count == 2:
                return _mock_403()  # homepage reseed
            return _mock_response(_FULL_PRODUCT)

        adapter._session.get = MagicMock(side_effect=side_effect)
        result = adapter.fetch_price("7260789")
        assert result is not None
        assert result.price == 2.2
