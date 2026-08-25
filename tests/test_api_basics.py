from decimal import Decimal
from unittest.mock import patch

from app.adapters.base import ProductResult
from app.services.products import RetailerSearchHit


def test_health_and_stateless_readiness_do_not_require_database(client):
    assert client.get("/health").json() == {"status": "ok"}
    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["dependencies"]["database"] == "optional_not_checked"


def test_request_id_is_returned(client):
    response = client.get("/health", headers={"X-Request-ID": "frontend-123"})
    assert response.headers["X-Request-ID"] == "frontend-123"


def test_optional_bearer_auth(monkeypatch, client):
    monkeypatch.setenv("BASKET_API_KEY", "test-only-secret")

    denied = client.post("/basket/compare", json={"ingredients": ["milk"]})
    assert denied.status_code == 401

    with monkeypatch.context() as context:
        context.setattr("app.main.compare_basket", lambda ingredients: [])
        allowed = client.post(
            "/basket/compare",
            json={"ingredients": ["milk"]},
            headers={"Authorization": "Bearer test-only-secret"},
        )
    assert allowed.status_code == 200


def test_basket_input_is_bounded(client):
    response = client.post(
        "/basket/compare",
        json={"ingredients": [f"ingredient-{index}" for index in range(51)]},
    )
    assert response.status_code == 422


def test_structured_basket_preserves_item_and_name_limits(client):
    too_many = client.post(
        "/basket/compare",
        json={
            "items": [
                {"name": f"ingredient-{index}", "quantity": 1, "unit": "each"}
                for index in range(51)
            ]
        },
    )
    long_name = client.post(
        "/basket/compare",
        json={"items": [{"name": "x" * 121, "quantity": 1, "unit": "each"}]},
    )
    assert too_many.status_code == 422
    assert long_name.status_code == 422


def test_structured_basket_combines_duplicate_units(client):
    with patch("app.main.compare_basket", return_value=[]) as compare:
        response = client.post(
            "/basket/compare",
            json={
                "items": [
                    {"name": " Pasta ", "quantity": 500, "unit": "g"},
                    {"name": "pasta", "quantity": 1, "unit": "kg"},
                ]
            },
        )

    assert response.status_code == 200
    requested = compare.call_args.kwargs["items"]
    assert len(requested) == 1
    assert requested[0].name == "Pasta"
    assert requested[0].quantity == Decimal("1500.0")
    assert requested[0].unit == "g"


def test_basket_rejects_both_input_forms(client):
    response = client.post(
        "/basket/compare",
        json={
            "ingredients": ["milk"],
            "items": [{"name": "milk", "quantity": 1, "unit": "l"}],
        },
    )
    assert response.status_code == 422


def test_basket_rejects_missing_input_form(client):
    assert client.post("/basket/compare", json={}).status_code == 422


def test_basket_rejects_null_input_forms(client):
    assert client.post("/basket/compare", json={"ingredients": None}).status_code == 422
    assert client.post("/basket/compare", json={"items": None}).status_code == 422


def test_basket_rejects_empty_structured_items(client):
    response = client.post("/basket/compare", json={"items": []})
    assert response.status_code == 400


def test_basket_rejects_incompatible_duplicate_dimensions(client):
    response = client.post(
        "/basket/compare",
        json={
            "items": [
                {"name": "tomatoes", "quantity": 500, "unit": "g"},
                {"name": "Tomatoes", "quantity": 500, "unit": "ml"},
            ]
        },
    )
    assert response.status_code == 422


def test_basket_rejects_fractional_counts_and_unsupported_units(client):
    fractional = client.post(
        "/basket/compare",
        json={"items": [{"name": "onion", "quantity": 1.5, "unit": "each"}]},
    )
    cooking_unit = client.post(
        "/basket/compare",
        json={"items": [{"name": "milk", "quantity": 1, "unit": "cup"}]},
    )
    assert fractional.status_code == 422
    assert cooking_unit.status_code == 422


def test_basket_rejects_boolean_quantity(client):
    response = client.post(
        "/basket/compare",
        json={"items": [{"name": "onion", "quantity": True, "unit": "each"}]},
    )
    assert response.status_code == 422


@patch("app.main.search_products")
def test_search_preserves_actual_retailer_identity(search_products, client):
    product = ProductResult("id", "Whole Milk 1L", "https://example/milk", 1.0)
    search_products.return_value = [RetailerSearchHit("ocado", product)]

    response = client.get("/search", params={"q": "milk"})

    assert response.status_code == 200
    assert response.json()[0]["retailer"] == "ocado"
    search_products.assert_called_once_with("milk", None)


def test_retailers_expose_configured_status_and_capabilities(client):
    response = client.get("/retailers")
    assert response.status_code == 200
    by_key = {retailer["key"]: retailer for retailer in response.json()}
    assert by_key["sainsburys"]["enabled"] is True
    assert "quantity_aware" in by_key["sainsburys"]["capabilities"]
    assert by_key["tesco"]["enabled"] is False
    assert by_key["tesco"]["disabled_reason"]
    assert by_key["tesco"]["capabilities"] == []


def test_openapi_describes_quantity_contract_and_version(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["version"] == "1.2.0"
    basket_request = schema["components"]["schemas"]["BasketCompareRequest"]
    assert {"ingredients", "items"} <= basket_request["properties"].keys()
    item = schema["components"]["schemas"]["BasketRequestItem"]
    assert item["properties"]["unit"]["enum"] == ["g", "kg", "ml", "cl", "l", "each"]
    result = schema["components"]["schemas"]["RetailerBasketResult"]
    assert "calculation_mode" in result["properties"]
    assert "coverage_issues" in result["properties"]
