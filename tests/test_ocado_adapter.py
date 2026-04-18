from unittest.mock import patch, MagicMock
import pytest

from app.adapters.ocado import OcadoAdapter


@pytest.fixture
def adapter():
    return OcadoAdapter()


def _mock_response(json_data: dict, status: int = 200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    if status >= 400:
        from requests import HTTPError
        mock.raise_for_status.side_effect = HTTPError(response=mock)
    return mock


SEARCH_RESPONSE = {
    "products": [
        {
            "id": "12345",
            "name": "Organic Whole Milk 2L",
            "price": {"current": 1.65, "unitPrice": 0.83, "unitOfMeasure": "per litre"},
            "imageUrl": "https://ocado.com/images/12345.jpg",
            "availability": "IN_STOCK",
        },
        {
            "id": "67890",
            "name": "Semi-Skimmed Milk 4 Pints",
            "price": {"current": 1.35, "unitPrice": 0.59, "unitOfMeasure": "per litre"},
            "imageUrl": "https://ocado.com/images/67890.jpg",
            "availability": "IN_STOCK",
        },
    ]
}

PRODUCT_RESPONSE = {
    "id": "12345",
    "name": "Organic Whole Milk 2L",
    "price": {"current": 1.65, "unitPrice": 0.83, "unitOfMeasure": "per litre"},
    "imageUrl": "https://ocado.com/images/12345.jpg",
    "availability": "IN_STOCK",
}


def test_search_returns_results(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.return_value = _mock_response(SEARCH_RESPONSE)
        results = adapter.search("milk")

    assert len(results) == 2
    assert results[0].external_id == "12345"
    assert results[0].name == "Organic Whole Milk 2L"
    assert results[0].price == 1.65
    assert results[0].unit_price == 0.83
    assert results[0].unit == "per litre"
    assert results[0].in_stock is True


def test_search_empty_when_api_fails(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.side_effect = Exception("connection error")
        results = adapter.search("milk")

    assert results == []


def test_search_empty_when_no_products(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.return_value = _mock_response({"products": []})
        results = adapter.search("xyznonexistent")

    assert results == []


def test_fetch_price_returns_product(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.return_value = _mock_response(PRODUCT_RESPONSE)
        result = adapter.fetch_price("12345")

    assert result is not None
    assert result.external_id == "12345"
    assert result.price == 1.65
    assert result.in_stock is True


def test_fetch_price_returns_none_on_error(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.side_effect = Exception("not found")
        result = adapter.fetch_price("99999")

    assert result is None


def test_fetch_price_skips_missing_price(adapter):
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.return_value = _mock_response({"id": "1", "name": "No price item", "price": {}})
        result = adapter.fetch_price("1")

    assert result is None


def test_out_of_stock_flag(adapter):
    data = {**SEARCH_RESPONSE}
    data["products"] = [{**SEARCH_RESPONSE["products"][0], "availability": "OUT_OF_STOCK"}]
    with patch("app.adapters.ocado._SESSION") as mock_session:
        mock_session.get.return_value = _mock_response(data)
        results = adapter.search("milk")

    assert results[0].in_stock is False
