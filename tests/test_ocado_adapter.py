from __future__ import annotations

import pytest

from app.adapters.ocado import OcadoAdapter


def test_parse_search_page_extracts_products() -> None:
    html = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "ItemList",
      "itemListElement": [
        {
          "@type": "ListItem",
          "position": 1,
          "item": {
            "@type": "Product",
            "sku": "12345",
            "name": "Ocado Bananas 5 Pack",
            "brand": {"@type": "Brand", "name": "Ocado"},
            "image": ["https://example.com/banana.jpg"],
            "url": "/products/ocado-bananas-12345",
            "offers": {
              "@type": "Offer",
              "price": "1.20",
              "priceCurrency": "GBP",
              "availability": "https://schema.org/InStock"
            }
          }
        }
      ]
    }
    </script>
    </head><body></body></html>
    """

    adapter = OcadoAdapter()
    results = adapter.parse_search_page(html, "https://www.ocado.com/search?entry=bananas")

    assert len(results) == 1
    item = results[0]
    assert item.external_id == "12345"
    assert item.name == "Ocado Bananas 5 Pack"
    assert item.brand == "Ocado"
    assert item.price_gbp == 1.20
    assert item.image_url == "https://example.com/banana.jpg"
    assert item.product_url == "https://www.ocado.com/products/ocado-bananas-12345"
    assert item.in_stock is True


def test_parse_search_page_empty_no_results() -> None:
    html = "<html><body><h1>No results</h1></body></html>"
    adapter = OcadoAdapter()

    results = adapter.parse_search_page(html, "https://www.ocado.com/search?entry=unknown")

    assert results == []


def test_parse_product_page_malformed_raises() -> None:
    malformed_html = "<html><script type='application/ld+json'>{bad-json</script></html>"
    adapter = OcadoAdapter()

    with pytest.raises(ValueError):
        adapter.parse_product_page(malformed_html, "https://www.ocado.com/products/test")
