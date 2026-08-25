from pathlib import Path

from app.adapters.morrisons import _parse_products


FIXTURE = Path(__file__).parent / "fixtures" / "morrisons_search.html"


def test_product_name_and_url_are_associated_from_same_card():
    products = _parse_products(FIXTURE.read_text())

    assert len(products) == 1
    assert products[0].name == "Morrisons British Semi Skimmed Milk 4 Pint"
    assert products[0].external_id == "113240422"
    assert products[0].url.endswith(
        "/morrisons-british-semi-skimmed-milk-4-pint/113240422"
    )
    assert products[0].unit_price == 0.726


def test_mismatched_positional_url_is_rejected():
    html = """
    <a href="/products/morrisons-savers-cooked-ham/100000001"><span class="salt-vc">Morrisons Savers Cooked Ham</span></a>
    <script>{"name":"Morrisons Whole Milk","price":{"current":{"amount":"1.50","currency":"GBP"}}}</script>
    """

    assert _parse_products(html) == []
