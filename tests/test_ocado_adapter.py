"""Ocado adapter tests against the currently implemented v6 response shape."""

from unittest.mock import MagicMock, patch

import pytest

from app.adapters.ocado import OcadoAdapter


@pytest.fixture
def adapter():
    return OcadoAdapter()


def _mock_response(json_data: dict, status: int = 200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    if status >= 400:
        from requests import HTTPError

        response.raise_for_status.side_effect = HTTPError(response=response)
    return response


PRODUCT = {
    "retailerProductId": "12345",
    "name": "Organic Whole Milk 2L",
    "price": {"amount": 1.65},
    "unitPrice": {"price": {"amount": 0.83}, "unit": "per litre"},
    "image": {"src": "https://ocado.com/images/12345.jpg"},
    "available": True,
    "brand": "Example Dairy",
    "packSizeDescription": "2L",
}
SEARCH_RESPONSE = {
    "productGroups": [{"decoratedProducts": [PRODUCT]}],
}


def _session_with(response=None, error=None):
    session = MagicMock()
    if error:
        session.get.side_effect = error
    else:
        session.get.return_value = response
    return session


def test_search_returns_results(adapter):
    session = _session_with(_mock_response(SEARCH_RESPONSE))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        results = adapter.search("milk")

    assert len(results) == 1
    assert results[0].external_id == "12345"
    assert results[0].name == "Organic Whole Milk 2L"
    assert results[0].price == 1.65
    assert results[0].unit_price == 0.83
    assert results[0].unit == "per litre"
    assert results[0].in_stock is True


def test_search_marks_source_unavailable_when_api_fails(adapter):
    session = _session_with(error=Exception("connection error"))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        outcome = adapter.search_with_status("milk")

    assert outcome.products == []
    assert outcome.error_code == "source_unavailable"


def test_search_empty_is_a_genuine_no_match(adapter):
    session = _session_with(_mock_response({"productGroups": []}))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        outcome = adapter.search_with_status("xyznonexistent")

    assert outcome.products == []
    assert outcome.error_code is None


def test_fetch_price_returns_matching_product(adapter):
    session = _session_with(_mock_response(SEARCH_RESPONSE))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        result = adapter.fetch_price("12345")

    assert result is not None
    assert result.external_id == "12345"
    assert result.price == 1.65


def test_fetch_price_returns_none_on_error(adapter):
    session = _session_with(error=Exception("not found"))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        assert adapter.fetch_price("99999") is None


def test_search_skips_missing_price(adapter):
    no_price = {**PRODUCT, "price": {}}
    payload = {"productGroups": [{"decoratedProducts": [no_price]}]}
    session = _session_with(_mock_response(payload))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        assert adapter.search("milk") == []


def test_out_of_stock_flag(adapter):
    payload = {
        "productGroups": [{"decoratedProducts": [{**PRODUCT, "available": False}]}],
    }
    session = _session_with(_mock_response(payload))
    with patch("app.adapters.ocado._ensure_session", return_value=session):
        results = adapter.search("milk")

    assert results[0].in_stock is False
