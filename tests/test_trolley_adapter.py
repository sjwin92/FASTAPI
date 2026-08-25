from pathlib import Path
from unittest.mock import patch

from app.adapters import trolley
from app.adapters.base import ProductResult


FIXTURE = Path(__file__).parent / "fixtures" / "trolley_product.html"


def test_recorded_product_page_preserves_canonical_source_url():
    source_url = "https://www.trolley.co.uk/product/example-whole-milk/ABC123"
    products = trolley._parse_product_page(
        FIXTURE.read_text(), "ABC123", "Fallback", source_url
    )

    assert set(products) == {"asda", "waitrose"}
    assert products["asda"].name == "Example Whole Milk 2L"
    assert products["asda"].price == 1.50
    assert products["asda"].url == source_url


def test_cache_entry_expires():
    product = ProductResult("id", "Milk", "https://example.test/milk", 1.0)
    trolley._cache.clear()
    with (
        patch.object(trolley, "_CACHE_TTL_SECONDS", 10),
        patch.object(trolley.time, "monotonic", side_effect=[100.0, 105.0, 111.0]),
        patch.object(
            trolley,
            "_fetch_all",
            side_effect=[{"asda": [product]}, {"asda": []}],
        ) as fetch,
    ):
        assert trolley._cached_lookup("milk")["asda"] == [product]
        assert trolley._cached_lookup(" MILK ")["asda"] == [product]
        assert trolley._cached_lookup("milk")["asda"] == []

    assert fetch.call_count == 2


def test_cache_size_is_bounded():
    trolley._cache.clear()
    with (
        patch.object(trolley, "_CACHE_MAX_ENTRIES", 2),
        patch.object(trolley, "_fetch_all", return_value={}),
    ):
        trolley._cached_lookup("one")
        trolley._cached_lookup("two")
        trolley._cached_lookup("three")

    assert list(trolley._cache) == ["two", "three"]
