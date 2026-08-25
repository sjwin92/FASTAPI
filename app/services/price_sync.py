"""Fail-safe ingredient matching and retailer basket comparison."""

from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from app.adapters.base import AdapterSearchOutcome, ProductResult
from app.adapters.registry import RETAILER_NAMES, all_adapters, get_adapter


logger = logging.getLogger(__name__)
_MAX_ADAPTER_WORKERS = max(1, min(int(os.getenv("MAX_ADAPTER_WORKERS", "4")), 8))

_STOP_WORDS = frozenset(
    {
        "a", "an", "the", "of", "with", "and", "or", "in", "for",
        "fresh", "british", "g", "gram", "grams", "kg", "kilogram",
        "kilograms", "ml", "cl", "l", "ltr", "litre", "litres", "cup",
        "cups", "tbsp", "tsp", "pack", "packet",
    }
)
_QUANTITY_TOKEN_RE = re.compile(
    r"^\d+(?:g|kg|ml|cl|l|ltr|litre|litres|oz|pt|pint|pints)?$"
)

_ERROR_MESSAGES = {
    "disabled_by_policy": "Source disabled pending permission for automated access.",
    "http_error": "Retailer source returned an HTTP error.",
    "invalid_response": "Retailer source returned an unsupported response.",
    "source_unavailable": "Retailer data could not be retrieved.",
}


def _canonical_token(token: str) -> str:
    if token.endswith("ies") and len(token) > 4:
        return f"{token[:-3]}y"
    if token.endswith("oes") and len(token) > 4:
        return token[:-2]
    if token.endswith("s") and not token.endswith(("ss", "us")) and len(token) > 3:
        return token[:-1]
    return token


def _normalise(text: str) -> list[str]:
    """Return exact, meaningful match tokens; never use substring matching."""
    text = re.sub(r"[^a-z0-9\s]", " ", text.casefold())
    return [
        _canonical_token(token)
        for token in text.split()
        if token not in _STOP_WORDS and not _QUANTITY_TOKEN_RE.fullmatch(token)
    ]


def normalise_ingredients(ingredients: list[str]) -> list[str]:
    """Trim whitespace and deduplicate case-insensitively while preserving order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in ingredients:
        value = " ".join(raw.split())
        key = value.casefold()
        if value and key not in seen:
            cleaned.append(value)
            seen.add(key)
    return cleaned


def _relevance(query_tokens: list[str], product_name: str) -> float:
    if not query_tokens:
        return 0.0
    product_tokens = set(_normalise(product_name))
    hits = sum(token in product_tokens for token in query_tokens)
    return hits / len(query_tokens)


def best_match(
    query: str,
    candidates: list[ProductResult],
    min_relevance: float = 1.0,
) -> ProductResult | None:
    """Choose the cheapest in-stock exact-token match, failing closed."""
    tokens = _normalise(query)
    scored = [
        (candidate, _relevance(tokens, candidate.name))
        for candidate in candidates
        if candidate.in_stock
    ]
    eligible = [(candidate, score) for candidate, score in scored if score >= min_relevance]
    if not eligible:
        return None
    return min(eligible, key=lambda entry: (-entry[1], entry[0].price))[0]


@dataclass(frozen=True)
class AdapterError:
    ingredient: str
    retailer: str
    code: str
    message: str = "Retailer data could not be retrieved."


@dataclass
class IngredientMatch:
    ingredient: str
    product: ProductResult
    retailer: str


@dataclass
class SyncResult:
    synced: list[IngredientMatch] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    errors: list[AdapterError] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)


def find_best_prices(ingredients: list[str], retailer: str | None = None) -> SyncResult:
    """Find strong matches while keeping source failures distinct from no-match."""
    ingredients = normalise_ingredients(ingredients)
    adapters = [get_adapter(retailer)] if retailer else all_adapters()
    adapters = [adapter for adapter in adapters if adapter is not None]

    result = SyncResult()
    if not adapters or not ingredients:
        result.not_found = list(ingredients)
        return result

    def _search_one(adapter, query: str) -> tuple[AdapterSearchOutcome, int]:
        started = time.monotonic()
        outcome = adapter.search_with_status(query)
        duration_ms = round((time.monotonic() - started) * 1000)
        logger.info(
            "adapter_search retailer=%s duration_ms=%d available=%s results=%d",
            adapter.retailer_key,
            duration_ms,
            outcome.is_available,
            len(outcome.products),
        )
        return outcome, duration_ms

    failed_adapters: dict[str, str] = {}

    def _adapter_error(ingredient: str, retailer_key: str, code: str) -> AdapterError:
        return AdapterError(
            ingredient,
            retailer_key,
            code,
            _ERROR_MESSAGES.get(code, "Retailer data could not be retrieved."),
        )

    for ingredient in ingredients:
        candidates: list[tuple[ProductResult, str]] = []
        available_adapters = 0
        active_adapters = [
            adapter for adapter in adapters if adapter.retailer_key not in failed_adapters
        ]
        for retailer_key, code in failed_adapters.items():
            result.errors.append(_adapter_error(ingredient, retailer_key, code))

        workers = min(_MAX_ADAPTER_WORKERS, len(active_adapters))
        if not active_adapters:
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_search_one, adapter, ingredient): adapter
                for adapter in active_adapters
            }
            for future in as_completed(futures):
                adapter = futures[future]
                try:
                    outcome, duration_ms = future.result()
                except Exception:
                    outcome, duration_ms = AdapterSearchOutcome([], "source_unavailable"), 0
                result.timings_ms[adapter.retailer_key] = (
                    result.timings_ms.get(adapter.retailer_key, 0) + duration_ms
                )
                if not outcome.is_available:
                    code = outcome.error_code or "source_unavailable"
                    failed_adapters[adapter.retailer_key] = code
                    result.errors.append(_adapter_error(ingredient, adapter.retailer_key, code))
                    continue
                available_adapters += 1
                candidates.extend((product, adapter.retailer_key) for product in outcome.products)

        match = best_match(ingredient, [candidate[0] for candidate in candidates])
        if match:
            retailer_key = next(key for product, key in candidates if product is match)
            result.synced.append(IngredientMatch(ingredient, match, retailer_key))
        elif available_adapters == len(adapters):
            result.not_found.append(ingredient)

    return result


@dataclass
class RetailerBasket:
    retailer: str
    retailer_name: str
    total: float
    items: list[dict]
    not_found: list[str]
    matched_count: int = 0
    requested_count: int = 0
    is_complete: bool = False
    availability: str = "unavailable"
    total_is_comparable: bool = False
    errors: list[AdapterError] = field(default_factory=list)
    duration_ms: int = 0


def compare_basket(ingredients: list[str]) -> list[RetailerBasket]:
    """Compare retailer baskets, ranking completeness before subtotal."""
    ingredients = normalise_ingredients(ingredients)
    requested_count = len(ingredients)

    def _basket_for_retailer(retailer_key: str) -> RetailerBasket:
        sync = find_best_prices(ingredients, retailer=retailer_key)
        total = sum(match.product.price for match in sync.synced)
        items = [
            {
                "ingredient": match.ingredient,
                "product_name": match.product.name,
                "price": match.product.price,
                "unit_price": match.product.unit_price,
                "unit": match.product.unit,
                "url": match.product.url,
                "image_url": match.product.image_url,
                "retrieved_at": match.product.retrieved_at,
            }
            for match in sync.synced
        ]
        matched_count = len(sync.synced)
        is_complete = requested_count > 0 and matched_count == requested_count and not sync.errors
        if is_complete:
            availability = "available"
        elif matched_count == 0 and sync.errors:
            availability = "unavailable"
        else:
            availability = "partial"
        return RetailerBasket(
            retailer=retailer_key,
            retailer_name=RETAILER_NAMES.get(retailer_key, retailer_key),
            total=round(total, 2),
            items=items,
            not_found=sync.not_found,
            matched_count=matched_count,
            requested_count=requested_count,
            is_complete=is_complete,
            availability=availability,
            total_is_comparable=is_complete,
            errors=sync.errors,
            duration_ms=sync.timings_ms.get(retailer_key, 0),
        )

    baskets: list[RetailerBasket] = []
    workers = min(_MAX_ADAPTER_WORKERS, len(RETAILER_NAMES))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_basket_for_retailer, key) for key in RETAILER_NAMES]
        baskets.extend(future.result() for future in as_completed(futures))

    availability_rank = {"available": 0, "partial": 1, "unavailable": 2}
    return sorted(
        baskets,
        key=lambda basket: (
            availability_rank[basket.availability],
            -basket.matched_count,
            basket.total,
            basket.retailer,
        ),
    )
