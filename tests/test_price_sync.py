"""Tests for price_sync service and /price-sync + /basket/compare endpoints."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.adapters.base import ProductResult
from app.services.price_sync import (
    _normalise,
    _relevance,
    best_match,
    find_best_prices,
    compare_basket,
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


# ---------------------------------------------------------------------------
# Service layer with mocked adapters
# ---------------------------------------------------------------------------

def _make_adapter(retailer_key: str, results: list[ProductResult]):
    adapter = MagicMock()
    adapter.retailer_key = retailer_key
    adapter.search.return_value = results
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
        bad.search.side_effect = RuntimeError("network error")
        mock_all.return_value = [bad]
        result = find_best_prices(["pasta"])
        assert result.not_found == ["pasta"]

    @patch("app.services.price_sync.all_adapters")
    def test_multiple_adapters_picks_cheapest(self, mock_all):
        mock_all.return_value = [
            _make_adapter("tesco", [self._p("Pasta 500g", 1.50)]),
            _make_adapter("sainsburys", [self._p("Pasta Penne 500g", 0.90)]),
        ]
        result = find_best_prices(["pasta"])
        assert result.synced[0].product.price == 0.90


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

    def test_empty_ingredients_returns_400(self, client):
        resp = client.post("/basket/compare", json={"ingredients": []})
        assert resp.status_code == 400
