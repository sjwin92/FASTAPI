"""Fail-safe ingredient matching and quantity-aware retailer basket comparison."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from decimal import Decimal

from app.adapters.base import AdapterSearchOutcome, ProductResult
from app.adapters.registry import RETAILER_NAMES, all_adapters, get_adapter
from app.quantities import (
    NormalizedQuantity,
    PackageMeasure,
    RequestUnit,
    amount_in_unit,
    money,
    normalize_requested_quantity,
    packs_required,
    parse_package_measure,
)


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
_MATERIAL_FORM_PATTERNS = {
    "uht": re.compile(r"\buht\b", re.IGNORECASE),
    "long_life": re.compile(r"\blong[\s-]*life\b", re.IGNORECASE),
    "powdered": re.compile(r"\bpowder(?:ed)?\b", re.IGNORECASE),
    "dried": re.compile(r"\bdried\b", re.IGNORECASE),
    "frozen": re.compile(r"\bfrozen\b", re.IGNORECASE),
    "canned": re.compile(r"\bcanned\b", re.IGNORECASE),
    "tinned": re.compile(r"\btinned\b", re.IGNORECASE),
    "condensed": re.compile(r"\bcondensed\b", re.IGNORECASE),
    "evaporated": re.compile(r"\bevaporated\b", re.IGNORECASE),
    "flavoured": re.compile(r"\bflavou?red\b", re.IGNORECASE),
    "ready_cooked": re.compile(r"\bready[\s-]*cooked\b", re.IGNORECASE),
    "breaded": re.compile(r"\bbreaded\b", re.IGNORECASE),
}
_ERROR_MESSAGES = {
    "disabled_by_policy": "Source disabled pending permission for automated access.",
    "http_error": "Retailer source returned an HTTP error.",
    "invalid_response": "Retailer source returned an unsupported response.",
    "source_unavailable": "Retailer data could not be retrieved.",
}
_COVERAGE_MESSAGES = {
    "no_acceptable_variant": "Related products were rejected because their form was not requested.",
    "package_size_unknown": "Related products did not expose a reliable package size.",
    "unit_incompatible": "Related products use a package dimension incompatible with the request.",
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
    text = re.sub(r"[^a-z0-9\s]", " ", text.casefold())
    return [
        _canonical_token(token)
        for token in text.split()
        if token not in _STOP_WORDS and not _QUANTITY_TOKEN_RE.fullmatch(token)
    ]


def normalise_ingredients(ingredients: list[str]) -> list[str]:
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


def _material_forms(text: str) -> set[str]:
    return {
        form
        for form, pattern in _MATERIAL_FORM_PATTERNS.items()
        if pattern.search(text)
    }


def _variant_is_acceptable(query: str, product_name: str) -> bool:
    return not (_material_forms(product_name) - _material_forms(query))


def _semantic_candidates(
    query: str,
    candidates: list[ProductResult],
) -> list[tuple[ProductResult, float]]:
    tokens = _normalise(query)
    semantic = []
    for candidate in candidates:
        score = _relevance(tokens, candidate.name)
        if candidate.in_stock and score >= 1.0:
            semantic.append((candidate, score))
    return semantic


def best_match(
    query: str,
    candidates: list[ProductResult],
    min_relevance: float = 1.0,
) -> ProductResult | None:
    semantic = _semantic_candidates(query, candidates)
    eligible = [
        (candidate, score)
        for candidate, score in semantic
        if score >= min_relevance and _variant_is_acceptable(query, candidate.name)
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda entry: (-entry[1], entry[0].price, entry[0].external_id),
    )[0]


@dataclass(frozen=True)
class BasketQuantityRequest:
    name: str
    quantity: Decimal
    unit: RequestUnit

    @property
    def normalized(self) -> NormalizedQuantity:
        return normalize_requested_quantity(self.quantity, self.unit)


@dataclass(frozen=True)
class AdapterError:
    ingredient: str
    retailer: str
    code: str
    message: str = "Retailer data could not be retrieved."


@dataclass(frozen=True)
class CoverageIssue:
    ingredient: str
    code: str
    message: str
    candidate_product_name: str | None = None


@dataclass(frozen=True)
class QuantityCandidate:
    product: ProductResult
    retailer: str
    score: float
    requested: NormalizedQuantity
    package: PackageMeasure
    packs_needed: int
    supplied_base: Decimal
    excess_base: Decimal
    line_total: Decimal
    unallocated_value: Decimal


@dataclass
class IngredientMatch:
    ingredient: str
    product: ProductResult
    retailer: str
    requested_quantity: Decimal | None = None
    requested_unit: RequestUnit | None = None
    package_quantity: Decimal | None = None
    package_unit: str | None = None
    packs_needed: int = 1
    supplied_quantity: Decimal | None = None
    excess_quantity: Decimal | None = None
    line_total: Decimal | None = None


@dataclass
class SyncResult:
    synced: list[IngredientMatch] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    errors: list[AdapterError] = field(default_factory=list)
    coverage_issues: list[CoverageIssue] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _SearchRequest:
    ingredient: str
    quantity: NormalizedQuantity | None = None


def quantity_candidate_frontier(
    query: str,
    requested: NormalizedQuantity,
    candidates: list[tuple[ProductResult, str]],
    max_candidates: int = 5,
) -> tuple[list[QuantityCandidate], CoverageIssue | None]:
    """Return non-dominated purchasable package choices for one requirement."""
    tokens = _normalise(query)
    semantic: list[tuple[ProductResult, float, str]] = []
    for candidate, retailer_key in candidates:
        score = _relevance(tokens, candidate.name)
        if candidate.in_stock and score >= 1.0:
            semantic.append((candidate, score, retailer_key))
    if not semantic:
        return [], None

    acceptable = [
        (candidate, score, retailer_key)
        for candidate, score, retailer_key in semantic
        if _variant_is_acceptable(query, candidate.name)
    ]
    if not acceptable:
        candidate = min(
            semantic,
            key=lambda entry: (-entry[1], entry[0].external_id, entry[2]),
        )[0]
        return [], CoverageIssue(
            query,
            "no_acceptable_variant",
            _COVERAGE_MESSAGES["no_acceptable_variant"],
            candidate.name,
        )

    measured: list[tuple[ProductResult, float, str, PackageMeasure | None]] = [
        (
            candidate,
            score,
            retailer_key,
            parse_package_measure(candidate.size_text, candidate.name, candidate.unit),
        )
        for candidate, score, retailer_key in acceptable
    ]
    compatible = [
        (candidate, score, retailer_key, package)
        for candidate, score, retailer_key, package in measured
        if package is not None and package.dimension == requested.dimension
    ]
    if not compatible:
        unknown = [entry for entry in measured if entry[3] is None]
        issue_candidates = unknown or measured
        candidate = min(
            issue_candidates,
            key=lambda entry: (-entry[1], entry[0].external_id, entry[2]),
        )[0]
        code = "package_size_unknown" if unknown else "unit_incompatible"
        return [], CoverageIssue(
            query,
            code,
            _COVERAGE_MESSAGES[code],
            candidate.name,
        )

    choices: list[QuantityCandidate] = []
    for candidate, score, retailer_key, package in compatible:
        assert package is not None
        price = Decimal(str(candidate.price))
        if not price.is_finite() or price <= 0:
            continue
        pack_count = packs_required(requested.base_amount, package.amount)
        supplied_base = package.amount * pack_count
        excess_base = supplied_base - requested.base_amount
        line_total = money(price * pack_count)
        unallocated_value = money(
            line_total * excess_base / supplied_base
        )
        choices.append(
            QuantityCandidate(
                product=candidate,
                retailer=retailer_key,
                score=score,
                requested=requested,
                package=package,
                packs_needed=pack_count,
                supplied_base=supplied_base,
                excess_base=excess_base,
                line_total=line_total,
                unallocated_value=unallocated_value,
            )
        )

    if not choices:
        candidate = min(
            acceptable,
            key=lambda entry: (-entry[1], entry[0].external_id, entry[2]),
        )[0]
        return [], CoverageIssue(
            query,
            "package_size_unknown",
            _COVERAGE_MESSAGES["package_size_unknown"],
            candidate.name,
        )

    frontier = []
    for choice in choices:
        dominated = any(
            other is not choice
            and other.line_total <= choice.line_total
            and other.unallocated_value <= choice.unallocated_value
            and other.score >= choice.score
            and (
                other.line_total < choice.line_total
                or other.unallocated_value < choice.unallocated_value
                or other.score > choice.score
            )
            for other in choices
        )
        if not dominated:
            frontier.append(choice)
    frontier.sort(
        key=lambda choice: (
            choice.line_total,
            choice.unallocated_value,
            choice.excess_base,
            -choice.score,
            choice.product.external_id,
            choice.retailer,
        )
    )
    return frontier[:max_candidates], None


def _quantity_match(
    request: _SearchRequest,
    candidates: list[tuple[ProductResult, str]],
) -> tuple[IngredientMatch | None, CoverageIssue | None]:
    assert request.quantity is not None
    frontier, issue = quantity_candidate_frontier(
        request.ingredient,
        request.quantity,
        candidates,
        max_candidates=max(5, len(candidates)),
    )
    if not frontier:
        return None, issue

    ranked: list[tuple[tuple[Decimal, Decimal, float, str], IngredientMatch]] = []
    for choice in frontier:
        match = IngredientMatch(
            ingredient=request.ingredient,
            product=choice.product,
            retailer=choice.retailer,
            requested_quantity=request.quantity.amount,
            requested_unit=request.quantity.unit,
            package_quantity=choice.package.amount,
            package_unit=choice.package.unit,
            packs_needed=choice.packs_needed,
            supplied_quantity=amount_in_unit(
                choice.supplied_base, request.quantity.unit
            ),
            excess_quantity=amount_in_unit(
                choice.excess_base, request.quantity.unit
            ),
            line_total=choice.line_total,
        )
        ranked.append(
            (
                (
                    choice.line_total,
                    choice.excess_base,
                    -choice.score,
                    choice.product.external_id,
                ),
                match,
            )
        )
    return min(ranked, key=lambda entry: entry[0])[1], None


def _find_prices(requests: list[_SearchRequest], retailer: str | None = None) -> SyncResult:
    adapters = [get_adapter(retailer)] if retailer else all_adapters()
    adapters = [adapter for adapter in adapters if adapter is not None]
    result = SyncResult()
    if not adapters or not requests:
        result.not_found = [request.ingredient for request in requests]
        return result

    def _search_one(adapter, query: str) -> tuple[AdapterSearchOutcome, int]:
        started = time.monotonic()
        outcome = adapter.search_with_status(query)
        duration_ms = round((time.monotonic() - started) * 1000)
        logger.info(
            json.dumps(
                {
                    "event": "adapter_search",
                    "retailer": adapter.retailer_key,
                    "duration_ms": duration_ms,
                    "available": outcome.is_available,
                    "result_count": len(outcome.products),
                },
                separators=(",", ":"),
            )
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

    for request in requests:
        candidates: list[tuple[ProductResult, str]] = []
        available_adapters = 0
        active_adapters = [
            adapter for adapter in adapters if adapter.retailer_key not in failed_adapters
        ]
        for retailer_key, code in failed_adapters.items():
            result.errors.append(_adapter_error(request.ingredient, retailer_key, code))

        workers = min(_MAX_ADAPTER_WORKERS, len(active_adapters))
        if not active_adapters:
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_search_one, adapter, request.ingredient): adapter
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
                    result.errors.append(
                        _adapter_error(request.ingredient, adapter.retailer_key, code)
                    )
                    continue
                available_adapters += 1
                candidates.extend((product, adapter.retailer_key) for product in outcome.products)

        products = [candidate[0] for candidate in candidates]
        if request.quantity is None:
            match = best_match(request.ingredient, products)
            if match:
                retailer_key = next(key for product, key in candidates if product is match)
                result.synced.append(IngredientMatch(request.ingredient, match, retailer_key))
            elif available_adapters == len(adapters):
                result.not_found.append(request.ingredient)
            continue

        quantity_match: IngredientMatch | None = None
        issue: CoverageIssue | None = None
        if candidates:
            quantity_match, issue = _quantity_match(request, candidates)
        if quantity_match:
            result.synced.append(quantity_match)
        elif issue and available_adapters == len(adapters):
            result.coverage_issues.append(issue)
        elif available_adapters == len(adapters):
            result.not_found.append(request.ingredient)

    return result


def find_best_prices(ingredients: list[str], retailer: str | None = None) -> SyncResult:
    requests = [_SearchRequest(ingredient) for ingredient in normalise_ingredients(ingredients)]
    return _find_prices(requests, retailer)


def find_best_prices_for_items(
    items: list[BasketQuantityRequest],
    retailer: str | None = None,
) -> SyncResult:
    requests = [_SearchRequest(item.name, item.normalized) for item in items]
    return _find_prices(requests, retailer)


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
    calculation_mode: str = "one_pack"
    coverage_issues: list[CoverageIssue] = field(default_factory=list)


def compare_basket(
    ingredients: list[str] | None = None,
    items: list[BasketQuantityRequest] | None = None,
) -> list[RetailerBasket]:
    comparison_started = time.monotonic()
    calculation_mode = "quantity_aware" if items is not None else "one_pack"
    if items is not None:
        requested_count = len(items)
    else:
        ingredients = normalise_ingredients(ingredients or [])
        requested_count = len(ingredients)

    def _basket_for_retailer(retailer_key: str) -> RetailerBasket:
        if items is not None:
            sync = find_best_prices_for_items(items, retailer=retailer_key)
        else:
            sync = find_best_prices(ingredients or [], retailer=retailer_key)

        item_rows = []
        total = Decimal("0")
        for match in sync.synced:
            line_total = match.line_total or money(Decimal(str(match.product.price)))
            total += line_total
            item_rows.append(
                {
                    "ingredient": match.ingredient,
                    "product_name": match.product.name,
                    "price": match.product.price,
                    "unit_price": match.product.unit_price,
                    "unit": match.product.unit,
                    "url": match.product.url,
                    "image_url": match.product.image_url,
                    "retrieved_at": match.product.retrieved_at,
                    "requested_quantity": (
                        float(match.requested_quantity)
                        if match.requested_quantity is not None else None
                    ),
                    "requested_unit": match.requested_unit,
                    "package_quantity": (
                        float(match.package_quantity)
                        if match.package_quantity is not None else None
                    ),
                    "package_unit": match.package_unit,
                    "packs_needed": match.packs_needed,
                    "supplied_quantity": (
                        float(match.supplied_quantity)
                        if match.supplied_quantity is not None else None
                    ),
                    "excess_quantity": (
                        float(match.excess_quantity)
                        if match.excess_quantity is not None else None
                    ),
                    "line_total": float(line_total),
                }
            )

        matched_count = len(sync.synced)
        is_complete = (
            requested_count > 0
            and matched_count == requested_count
            and not sync.errors
            and not sync.coverage_issues
            and not sync.not_found
        )
        if is_complete:
            availability = "available"
        elif matched_count == 0 and sync.errors:
            availability = "unavailable"
        else:
            availability = "partial"
        return RetailerBasket(
            retailer=retailer_key,
            retailer_name=RETAILER_NAMES.get(retailer_key, retailer_key),
            total=float(money(total)),
            items=item_rows,
            not_found=sync.not_found,
            matched_count=matched_count,
            requested_count=requested_count,
            is_complete=is_complete,
            availability=availability,
            total_is_comparable=is_complete,
            errors=sync.errors,
            duration_ms=sync.timings_ms.get(retailer_key, 0),
            calculation_mode=calculation_mode,
            coverage_issues=sync.coverage_issues,
        )

    baskets: list[RetailerBasket] = []
    workers = min(_MAX_ADAPTER_WORKERS, len(RETAILER_NAMES))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_basket_for_retailer, key) for key in RETAILER_NAMES]
        baskets.extend(future.result() for future in as_completed(futures))

    availability_rank = {"available": 0, "partial": 1, "unavailable": 2}
    baskets.sort(
        key=lambda basket: (
            availability_rank[basket.availability],
            -basket.matched_count,
            basket.total,
            basket.retailer,
        )
    )
    logger.info(
        json.dumps(
            {
                "event": "basket_calculation",
                "calculation_mode": calculation_mode,
                "item_count": requested_count,
                "complete_retailers": sum(basket.is_complete for basket in baskets),
                "duration_ms": round((time.monotonic() - comparison_started) * 1000),
                "coverage_issue_codes": sorted(
                    {
                        issue.code
                        for basket in baskets
                        for issue in basket.coverage_issues
                    }
                ),
            },
            separators=(",", ":"),
        )
    )
    return baskets
