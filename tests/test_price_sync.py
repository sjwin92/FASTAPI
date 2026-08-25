"""Tests for price_sync service and /price-sync + /basket/compare endpoints."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.base import AdapterSearchOutcome, ProductResult
from app.services.price_sync import (
    _normalise,
    _relevance,
    best_match,
    find_best_prices,
    compare_basket,
    normalise_ingredients,
    BasketQuantityRequest,
)


# ---------------------------------------------------------------------------
# Unit helpers
# ---------------------------------------------------------------------------

class TestNormalise:
    def test_lowercases(self):
        assert "chicken" in _normalise("Chicken Breast")

    def test_strips_stop_words(self):
        tokens = _normalise("fresh british chicken")
        assert "fresh" not in tokens
        assert "british" not in tokens
        assert "chicken" in tokens

    def test_strips_punctuation(self):
        assert "milk" in _normalise("semi-skimmed milk")

    def test_deduplicates_and_trims_ingredients(self):
        assert normalise_ingredients([" Milk ", "milk", "olive   oil"]) == [
            "Milk", "olive oil"
        ]


class TestRelevance:
    def test_full_match(self):
        assert _relevance(["chicken", "breast"], "Chicken Breast Fillets 500g") == 1.0

    def test_partial_match(self):
        score = _relevance(["chicken", "breast", "smoked"], "Chicken Breast 500g")
        assert 0 < score < 1.0

    def test_zero_on_no_match(self):
        assert _relevance(["salmon"], "Chicken Breast") == 0.0

    def test_empty_tokens(self):
        assert _relevance([], "anything") == 0.0


class TestBestMatch:
    def _p(self, name, price, in_stock=True):
        return ProductResult(
            external_id="x", name=name, url="http://x", price=price, in_stock=in_stock
        )

    def test_picks_cheapest_relevant(self):
        candidates = [
            self._p("Chicken Breast Fillets 500g", 4.50),
            self._p("Chicken Breast Fillets 1kg", 7.00),
        ]
        result = best_match("chicken breast", candidates)
        assert result is not None
        assert result.price == 4.50

    def test_returns_none_below_threshold(self):
        candidates = [self._p("Tuna Steak 150g", 3.00)]
        assert best_match("chicken breast", candidates) is None

    def test_skips_out_of_stock(self):
        candidates = [
            self._p("Chicken Breast 500g", 3.00, in_stock=False),
            self._p("Chicken Breast Mini Fillets 400g", 3.50),
        ]
        result = best_match("chicken breast", candidates)
        assert result is not None
        assert result.price == 3.50

    def test_returns_none_if_empty(self):
        assert best_match("milk", []) is None

    def test_rejects_substring_false_positive(self):
        assert best_match("ham", [self._p("Champagne 75cl", 9.00)]) is None

    def test_requires_every_meaningful_query_token(self):
        candidates = [self._p("Chicken Breast Fillets 500g", 4.50)]
        assert best_match("smoked chicken breast", candidates) is None

    def test_rejects_unrequested_material_form(self):
        candidates = [
            self._p("Dairy Pride UHT Milk 1L", 0.69),
            self._p("British Semi Skimmed Milk 1L", 0.85),
        ]
        assert best_match("milk", candidates).name == "British Semi Skimmed Milk 1L"

    def test_accepts_material_form_when_explicitly_requested(self):
        candidate = self._p("Dairy Pride UHT Milk 1L", 0.69)
        assert best_match("UHT milk", [candidate]) is candidate

    def test_fresh_request_also_rejects_uht_milk(self):
        candidates = [
            self._p("Dairy Pride UHT Milk 1L", 0.69),
            self._p("British Whole Milk 1L", 0.85),
        ]
        assert best_match("fresh milk", candidates).name == "British Whole Milk 1L"


# ---------------------------------------------------------------------------
# Service layer with mocked adapters
# ---------------------------------------------------------------------------

def _make_adapter(retailer_key: str, results: list[ProductResult]):
    adapter = MagicMock()
    adapter.retailer_key = retailer_key
    adapter.search.return_value = results
    adapter.search_with_status.return_value = AdapterSearchOutcome(results)
    return adapter


class TestFindBestPrices:
    def _p(self, name, price, external_id="eid", url="http://x"):
        return ProductResult(external_id=external_id, name=name, url=url, price=price)

    @patch("app.services.price_sync.all_adapters")
    def test_synced_single_ingredient(self, mock_all):
        mock_all.return_value = [
            _make_adapter("tesco", [self._p("Semi Skimmed Milk 2pt", 1.10)])
        ]
        result = find_best_prices(["milk"])
        assert len(result.synced) == 1
        assert result.synced[0].ingredient == "milk"
        assert result.not_found == []

    @patch("app.services.price_sync.all_adapters")
    def test_not_found_when_no_relevant_results(self, mock_all):
        mock_all.return_value = [
            _make_adapter("tesco", [self._p("Tuna Steak", 3.00)])
        ]
        result = find_best_prices(["celeriac"])
        assert result.synced == []
        assert "celeriac" in result.not_found

    @patch("app.services.price_sync.get_adapter")
    def test_retailer_filter(self, mock_get):
        mock_get.return_value = _make_adapter(
            "sainsburys", [self._p("Whole Milk 4pt", 1.50)]
        )
        result = find_best_prices(["milk"], retailer="sainsburys")
        mock_get.assert_called_once_with("sainsburys")
        assert len(result.synced) == 1

    @patch("app.services.price_sync.all_adapters")
    def test_adapter_exception_handled(self, mock_all):
        bad = MagicMock()
        bad.retailer_key = "tesco"
        bad.search_with_status.side_effect = RuntimeError("network error")
        mock_all.return_value = [bad]
        result = find_best_prices(["pasta"])
        assert result.not_found == []
        assert result.errors[0].retailer == "tesco"
        assert result.errors[0].ingredient == "pasta"

    @patch("app.services.price_sync.all_adapters")
    def test_stops_querying_source_after_first_failure(self, mock_all):
        bad = MagicMock()
        bad.retailer_key = "tesco"
        bad.search_with_status.return_value = AdapterSearchOutcome(
            [], "source_unavailable"
        )
        mock_all.return_value = [bad]

        result = find_best_prices(["milk", "pasta"])

        assert bad.search_with_status.call_count == 1
        assert [error.ingredient for error in result.errors] == ["milk", "pasta"]

    @patch("app.services.price_sync.all_adapters")
    def test_multiple_adapters_picks_cheapest(self, mock_all):
        mock_all.return_value = [
            _make_adapter("tesco", [self._p("Pasta 500g", 1.50)]),
            _make_adapter("sainsburys", [self._p("Pasta Penne 500g", 0.90)]),
        ]
        result = find_best_prices(["pasta"])
        assert result.synced[0].product.price == 0.90


class TestCompareBasket:
    def _adapter(self, key, by_query):
        adapter = MagicMock()
        adapter.retailer_key = key
        adapter.search_with_status.side_effect = lambda query: by_query[query]
        return adapter

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES",
        {"complete": "Complete", "partial": "Partial"},
        clear=True,
    )
    @patch("app.services.price_sync.get_adapter")
    def test_complete_basket_ranks_before_cheaper_partial_basket(self, mock_get):
        milk = ProductResult("m", "Whole Milk", "https://example/m", 1.20)
        pasta = ProductResult("p", "Pasta", "https://example/p", 1.00)
        cheap_milk = ProductResult("cm", "Whole Milk", "https://example/cm", 0.50)
        adapters = {
            "complete": self._adapter(
                "complete",
                {
                    "milk": AdapterSearchOutcome([milk]),
                    "pasta": AdapterSearchOutcome([pasta]),
                },
            ),
            "partial": self._adapter(
                "partial",
                {
                    "milk": AdapterSearchOutcome([cheap_milk]),
                    "pasta": AdapterSearchOutcome([]),
                },
            ),
        }
        mock_get.side_effect = adapters.get

        baskets = compare_basket(["milk", "pasta"])

        assert baskets[0].retailer == "complete"
        assert baskets[0].is_complete is True
        assert baskets[1].total == 0.50
        assert baskets[1].total_is_comparable is False

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES",
        {"broken": "Broken"},
        clear=True,
    )
    @patch("app.services.price_sync.get_adapter")
    def test_unavailable_retailer_is_not_reported_as_not_found(self, mock_get):
        adapter = self._adapter(
            "broken", {"milk": AdapterSearchOutcome([], "source_unavailable")}
        )
        mock_get.return_value = adapter

        basket = compare_basket(["milk"])[0]

        assert basket.availability == "unavailable"
        assert basket.not_found == []
        assert basket.errors[0].code == "source_unavailable"
        assert basket.total_is_comparable is False


class TestQuantityAwareBasket:
    def _adapter(self, key, products):
        adapter = MagicMock()
        adapter.retailer_key = key
        adapter.search_with_status.return_value = AdapterSearchOutcome(products)
        return adapter

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_selects_lowest_checkout_cost_and_calculates_packs(self, mock_get):
        products = [
            ProductResult("small", "Whole Milk 500ml", "https://x/small", 0.60),
            ProductResult("large", "Whole Milk 1L", "https://x/large", 1.10),
        ]
        mock_get.return_value = self._adapter("shop", products)
        items = [BasketQuantityRequest("milk", Decimal("1"), "l")]

        basket = compare_basket(items=items)[0]

        assert basket.total == 1.10
        assert basket.calculation_mode == "quantity_aware"
        assert basket.is_complete is True
        assert basket.items[0]["product_name"] == "Whole Milk 1L"
        assert basket.items[0]["package_quantity"] == 1000.0
        assert basket.items[0]["package_unit"] == "ml"
        assert basket.items[0]["packs_needed"] == 1
        assert basket.items[0]["supplied_quantity"] == 1.0
        assert basket.items[0]["excess_quantity"] == 0.0
        assert basket.items[0]["line_total"] == 1.10

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_uses_multiple_packs_when_they_are_cheapest(self, mock_get):
        products = [
            ProductResult("small", "Pasta 400g", "https://x/small", 0.35),
            ProductResult("large", "Pasta 1kg", "https://x/large", 1.20),
        ]
        mock_get.return_value = self._adapter("shop", products)

        basket = compare_basket(
            items=[BasketQuantityRequest("pasta", Decimal("1"), "kg")]
        )[0]

        assert basket.items[0]["product_name"] == "Pasta 400g"
        assert basket.items[0]["packs_needed"] == 3
        assert basket.items[0]["supplied_quantity"] == 1.2
        assert basket.items[0]["excess_quantity"] == 0.2
        assert basket.total == 1.05

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_least_excess_breaks_equal_cost_tie(self, mock_get):
        products = [
            ProductResult("more", "Rice 750g", "https://x/more", 1.00),
            ProductResult("less", "Rice 500g", "https://x/less", 1.00),
        ]
        mock_get.return_value = self._adapter("shop", products)

        basket = compare_basket(
            items=[BasketQuantityRequest("rice", Decimal("500"), "g")]
        )[0]

        assert basket.items[0]["product_name"] == "Rice 500g"

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_product_id_breaks_an_exact_tie_deterministically(self, mock_get):
        products = [
            ProductResult("z-id", "Rice 500g", "https://x/z", 1.00),
            ProductResult("a-id", "Rice 500g", "https://x/a", 1.00),
        ]
        mock_get.return_value = self._adapter("shop", products)

        basket = compare_basket(
            items=[BasketQuantityRequest("rice", Decimal("500"), "g")]
        )[0]

        assert basket.items[0]["url"] == "https://x/a"

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES",
        {"complete": "Complete", "partial": "Partial"},
        clear=True,
    )
    @patch("app.services.price_sync.get_adapter")
    def test_complete_quantity_basket_outranks_cheaper_unproven_basket(self, mock_get):
        adapters = {
            "complete": self._adapter(
                "complete",
                [ProductResult("known", "Milk 1L", "https://x/known", 1.20)],
            ),
            "partial": self._adapter(
                "partial",
                [ProductResult("unknown", "Milk", "https://x/unknown", 0.50)],
            ),
        }
        mock_get.side_effect = adapters.get

        baskets = compare_basket(
            items=[BasketQuantityRequest("milk", Decimal("1"), "l")]
        )

        assert baskets[0].retailer == "complete"
        assert baskets[0].total_is_comparable is True
        assert baskets[1].total_is_comparable is False

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_rounds_line_and_basket_totals_to_pennies(self, mock_get):
        product = ProductResult("rice", "Rice 400g", "https://x/rice", 0.335)
        mock_get.return_value = self._adapter("shop", [product])

        basket = compare_basket(
            items=[BasketQuantityRequest("rice", Decimal("1"), "kg")]
        )[0]

        assert basket.items[0]["price"] == 0.335
        assert basket.items[0]["packs_needed"] == 3
        assert basket.items[0]["line_total"] == 1.01
        assert basket.total == 1.01

    @pytest.mark.parametrize(
        ("product", "request_unit", "issue_code"),
        [
            (
                ProductResult("unknown", "Pasta", "https://x/unknown", 1.00),
                "g",
                "package_size_unknown",
            ),
            (
                ProductResult("volume", "Pasta 500ml", "https://x/volume", 1.00),
                "g",
                "unit_incompatible",
            ),
        ],
    )
    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_unknown_or_incompatible_coverage_fails_closed(
        self, mock_get, product, request_unit, issue_code
    ):
        mock_get.return_value = self._adapter("shop", [product])

        basket = compare_basket(
            items=[BasketQuantityRequest("pasta", Decimal("500"), request_unit)]
        )[0]

        assert basket.matched_count == 0
        assert basket.availability == "partial"
        assert basket.total_is_comparable is False
        assert basket.coverage_issues[0].code == issue_code

    @patch.dict(
        "app.services.price_sync.RETAILER_NAMES", {"shop": "Shop"}, clear=True
    )
    @patch("app.services.price_sync.get_adapter")
    def test_specialized_form_becomes_explicit_coverage_issue(self, mock_get):
        product = ProductResult("uht", "UHT Milk 1L", "https://x/uht", 0.69)
        mock_get.return_value = self._adapter("shop", [product])

        basket = compare_basket(
            items=[BasketQuantityRequest("milk", Decimal("1"), "l")]
        )[0]

        assert basket.not_found == []
        assert basket.coverage_issues[0].code == "no_acceptable_variant"
        assert basket.is_complete is False


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------

class TestPriceSyncEndpoint:
    @patch("app.main.find_best_prices")
    def test_returns_synced_items(self, mock_find, client):
        from app.services.price_sync import IngredientMatch, SyncResult

        product = ProductResult(
            external_id="123", name="Semi Skimmed Milk 2pt", url="http://t.co/milk",
            price=1.10, unit="per litre", unit_price=0.97,
        )
        mock_find.return_value = SyncResult(
            synced=[IngredientMatch("milk", product, "tesco")],
            not_found=[],
        )
        resp = client.post("/price-sync", json={"ingredients": ["milk"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["synced"][0]["ingredient"] == "milk"
        assert body["not_found"] == []
        assert body["written_to_supabase"] is False

    @patch("app.main.find_best_prices")
    @patch("app.main.upsert_ingredient_prices")
    def test_writes_to_supabase_when_requested(self, mock_upsert, mock_find, client):
        from app.services.price_sync import IngredientMatch, SyncResult

        product = ProductResult(
            external_id="456", name="Pasta 500g", url="http://t.co/pasta", price=0.90,
        )
        mock_find.return_value = SyncResult(
            synced=[IngredientMatch("pasta", product, "tesco")],
            not_found=[],
        )
        resp = client.post(
            "/price-sync",
            json={"ingredients": ["pasta"], "write_to_supabase": True},
        )
        assert resp.status_code == 200
        mock_upsert.assert_called_once()
        assert resp.json()["written_to_supabase"] is True

    def test_unknown_retailer_returns_400(self, client):
        resp = client.post(
            "/price-sync", json={"ingredients": ["milk"], "retailer": "aldi"}
        )
        assert resp.status_code == 400


class TestBasketCompareEndpoint:
    @patch("app.main.compare_basket")
    def test_returns_sorted_baskets(self, mock_compare, client):
        from app.services.price_sync import RetailerBasket

        mock_compare.return_value = [
            RetailerBasket(
                retailer="tesco", retailer_name="Tesco",
                total=5.40, items=[], not_found=["celeriac"],
            ),
            RetailerBasket(
                retailer="sainsburys", retailer_name="Sainsbury's",
                total=6.10, items=[], not_found=[],
            ),
        ]
        resp = client.post(
            "/basket/compare", json={"ingredients": ["milk", "pasta", "celeriac"]}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["retailers"][0]["retailer"] == "tesco"
        assert body["retailers"][0]["total"] == 5.40
        assert "matched_count" in body["retailers"][0]
        assert "availability" in body["retailers"][0]

    def test_empty_ingredients_returns_400(self, client):
        resp = client.post("/basket/compare", json={"ingredients": []})
        assert resp.status_code == 400

    def test_normalises_and_deduplicates_input(self, client):
        with patch("app.main.compare_basket", return_value=[]) as mock_compare:
            resp = client.post(
                "/basket/compare", json={"ingredients": [" Milk ", "milk", "pasta"]}
            )
        assert resp.status_code == 200
        mock_compare.assert_called_once_with(["Milk", "pasta"])

    @patch("app.main.compare_basket")
    def test_serializes_quantity_aware_item_fields(self, mock_compare, client):
        from app.services.price_sync import RetailerBasket

        mock_compare.return_value = [
            RetailerBasket(
                retailer="ocado",
                retailer_name="Ocado",
                total=1.80,
                items=[
                    {
                        "ingredient": "pasta",
                        "product_name": "Pasta 400g",
                        "price": 0.60,
                        "unit_price": None,
                        "unit": None,
                        "url": "https://example/pasta",
                        "image_url": None,
                        "retrieved_at": None,
                        "requested_quantity": 1.0,
                        "requested_unit": "kg",
                        "package_quantity": 400.0,
                        "package_unit": "g",
                        "packs_needed": 3,
                        "supplied_quantity": 1.2,
                        "excess_quantity": 0.2,
                        "line_total": 1.80,
                    }
                ],
                not_found=[],
                matched_count=1,
                requested_count=1,
                is_complete=True,
                availability="available",
                total_is_comparable=True,
                calculation_mode="quantity_aware",
            )
        ]

        response = client.post(
            "/basket/compare",
            json={"items": [{"name": "pasta", "quantity": 1, "unit": "kg"}]},
        )

        assert response.status_code == 200
        result = response.json()["retailers"][0]
        assert result["calculation_mode"] == "quantity_aware"
        assert result["items"][0]["packs_needed"] == 3
        assert result["items"][0]["line_total"] == 1.8
