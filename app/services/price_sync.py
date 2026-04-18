"""
Price sync service: maps ingredient names to best retailer products,
then optionally writes results back to the Supabase ingredient_prices table.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.adapters.base import ProductResult
from app.adapters.registry import get_adapter, all_adapters, RETAILER_NAMES


# Words that carry no matching signal
_STOP_WORDS = frozenset(
    ["a", "an", "the", "of", "with", "and", "or", "in", "for", "fresh", "british"]
)


def _normalise(text: str) -> list[str]:
    """Lowercase, strip punctuation, split into meaningful tokens."""
    text = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    return [t for t in text.split() if t and t not in _STOP_WORDS]


def _relevance(query_tokens: list[str], product_name: str) -> float:
    """0–1 relevance of a product name against query tokens."""
    if not query_tokens:
        return 0.0
    p_tokens = _normalise(product_name)
    hits = sum(1 for qt in query_tokens if any(qt in pt or pt in qt for pt in p_tokens))
    return hits / len(query_tokens)


def best_match(
    query: str, candidates: list[ProductResult], min_relevance: float = 0.5
) -> ProductResult | None:
    """Return the cheapest candidate that meets the relevance threshold."""
    tokens = _normalise(query)
    scored = [
        (c, _relevance(tokens, c.name))
        for c in candidates
        if c.in_stock
    ]
    eligible = [(c, s) for c, s in scored if s >= min_relevance]
    if not eligible:
        return None
    return min(eligible, key=lambda x: x[0].price)[0]


@dataclass
class IngredientMatch:
    ingredient: str
    product: ProductResult
    retailer: str


@dataclass
class SyncResult:
    synced: list[IngredientMatch] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)


def find_best_prices(
    ingredients: list[str],
    retailer: str | None = None,
) -> SyncResult:
    """
    For each ingredient, search the requested retailer(s) and pick the best match.

    Uses a thread pool to parallelise adapter calls (one thread per adapter per ingredient).
    Falls back gracefully — a failed adapter search produces no results, not an exception.
    """
    adapters = [get_adapter(retailer)] if retailer else all_adapters()
    adapters = [a for a in adapters if a is not None]

    result = SyncResult()
    if not adapters or not ingredients:
        result.not_found = list(ingredients)
        return result

    def _search_one(adapter, query: str) -> list[ProductResult]:
        try:
            return adapter.search(query)
        except Exception:
            return []

    for ingredient in ingredients:
        candidates: list[ProductResult] = []
        # Parallelise across adapters for this ingredient
        with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
            futures = {pool.submit(_search_one, a, ingredient): a for a in adapters}
            for future in as_completed(futures):
                adapter = futures[future]
                hits = future.result()
                # Tag each result with its retailer key
                for h in hits:
                    # Attach retailer to the result object temporarily
                    h.__dict__["_retailer_key"] = adapter.retailer_key
                candidates.extend(hits)

        match = best_match(ingredient, candidates)
        if match:
            retailer_key = match.__dict__.get("_retailer_key", retailer or "unknown")
            result.synced.append(IngredientMatch(ingredient, match, retailer_key))
        else:
            result.not_found.append(ingredient)

    return result


@dataclass
class RetailerBasket:
    retailer: str
    retailer_name: str
    total: float
    items: list[dict]
    not_found: list[str]


def compare_basket(ingredients: list[str]) -> list[RetailerBasket]:
    """
    For each retailer, find the best match for every ingredient and return a basket total.
    Retailers are queried in parallel (one thread per retailer).
    """

    def _basket_for_retailer(retailer_key: str) -> RetailerBasket:
        sync = find_best_prices(ingredients, retailer=retailer_key)
        total = sum(m.product.price for m in sync.synced)
        items = [
            {
                "ingredient": m.ingredient,
                "product_name": m.product.name,
                "price": m.product.price,
                "unit_price": m.product.unit_price,
                "unit": m.product.unit,
                "url": m.product.url,
                "image_url": m.product.image_url,
            }
            for m in sync.synced
        ]
        return RetailerBasket(
            retailer=retailer_key,
            retailer_name=RETAILER_NAMES.get(retailer_key, retailer_key),
            total=round(total, 2),
            items=items,
            not_found=sync.not_found,
        )

    baskets: list[RetailerBasket] = []
    with ThreadPoolExecutor(max_workers=len(RETAILER_NAMES)) as pool:
        futures = {
            pool.submit(_basket_for_retailer, key): key
            for key in RETAILER_NAMES
        }
        for future in as_completed(futures):
            baskets.append(future.result())

    return sorted(baskets, key=lambda b: b.total)
