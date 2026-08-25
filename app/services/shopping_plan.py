"""Stateless pantry allocation and exact one/two-retailer shopping plans."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from itertools import combinations

from app.adapters.base import AdapterSearchOutcome, DisabledAdapter
from app.adapters.registry import RETAILER_NAMES, get_adapter
from app.quantities import BaseUnit, NormalizedQuantity, money, normalize_requested_quantity
from app.shopping_schemas import (
    DemandSummaryItem,
    MinimumSpendCheck,
    PantryAllocation,
    PantryInsight,
    ParetoLabels,
    PurchaseManifestLine,
    RequirementPurchaseAllocation,
    RetailerDiagnostic,
    ShoppingCoverageIssue,
    ShoppingOption,
    ShoppingPlanRequest,
    ShoppingPlanResponse,
    ShoppingSourceError,
)
from app.services.price_sync import QuantityCandidate, quantity_candidate_frontier


logger = logging.getLogger(__name__)
_MAX_WORKERS = max(1, min(int(os.getenv("MAX_ADAPTER_WORKERS", "4")), 8))
_MAX_OPTIMIZER_STATES = max(
    10_000,
    min(int(os.getenv("SHOPPING_PLAN_MAX_OPTIMIZER_STATES", "200000")), 1_000_000),
)
_MAX_OPTIONS = 12
_SOURCE_MESSAGES = {
    "disabled_by_policy": "Source disabled pending permission for automated access.",
    "http_error": "Retailer source returned an HTTP error.",
    "invalid_response": "Retailer source returned an unsupported response.",
    "source_unavailable": "Retailer data could not be retrieved.",
}


@dataclass
class _LotState:
    lot_key: str
    ingredient_key: str
    amount: Decimal
    unit: BaseUnit
    expires_on: date | None
    remaining: Decimal


@dataclass
class _RequirementState:
    requirement_key: str
    ingredient_key: str
    name: str
    needed_on: date
    amount: Decimal
    unit: BaseUnit
    remaining: Decimal


@dataclass
class _DemandGroup:
    ingredient_key: str
    name: str
    unit: BaseUnit
    total_required: Decimal = Decimal("0")
    pantry_allocated: Decimal = Decimal("0")
    purchase_required: Decimal = Decimal("0")
    requirement_shortages: dict[str, Decimal] = field(default_factory=dict)

    @property
    def normalized_purchase(self) -> NormalizedQuantity:
        return normalize_requested_quantity(self.purchase_required, self.unit)


@dataclass
class _AllocationResult:
    groups: dict[str, _DemandGroup]
    allocations: list[PantryAllocation]
    insights: list[PantryInsight]


@dataclass
class _MatrixResult:
    matrix: dict[str, dict[str, list[QuantityCandidate]]]
    diagnostics: list[RetailerDiagnostic]
    active_retailers: list[str]
    source_uncertain: bool


@dataclass(frozen=True)
class _OptimizerState:
    spends: tuple[int, ...]
    total_pence: int
    unallocated_pence: int
    used_mask: int
    assignments: tuple[tuple[str, QuantityCandidate], ...]
    signature: tuple[str, ...]


@dataclass
class _OptimizationResult:
    options: list[ShoppingOption]
    limited: bool = False
    issues: list[str] = field(default_factory=list)


def _float(amount: Decimal) -> float:
    return float(amount)


def _pence(amount: Decimal | float) -> int:
    return int(money(Decimal(str(amount))) * 100)


def _gbp(pence: int) -> float:
    return float(Decimal(pence) / Decimal("100"))


def _hash(prefix: str, value) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def allocate_pantry(payload: ShoppingPlanRequest) -> _AllocationResult:
    """Allocate pantry lots FEFO while respecting each meal's required date."""
    requirements: list[_RequirementState] = []
    groups: dict[str, _DemandGroup] = {}
    for item in payload.requirements:
        normalized = normalize_requested_quantity(item.quantity, item.unit)
        requirements.append(
            _RequirementState(
                requirement_key=item.requirement_key,
                ingredient_key=item.ingredient_key,
                name=item.name,
                needed_on=item.needed_on,
                amount=normalized.base_amount,
                unit=normalized.base_unit,
                remaining=normalized.base_amount,
            )
        )
        group = groups.setdefault(
            item.ingredient_key,
            _DemandGroup(item.ingredient_key, item.name, normalized.base_unit),
        )
        group.total_required += normalized.base_amount

    lots_by_ingredient: dict[str, list[_LotState]] = {}
    for lot in payload.pantry_lots:
        normalized = normalize_requested_quantity(lot.available_quantity, lot.unit)
        state = _LotState(
            lot_key=lot.lot_key,
            ingredient_key=lot.ingredient_key,
            amount=normalized.base_amount,
            unit=normalized.base_unit,
            expires_on=lot.expires_on,
            remaining=normalized.base_amount,
        )
        lots_by_ingredient.setdefault(lot.ingredient_key, []).append(state)

    allocations: list[PantryAllocation] = []
    insights: list[PantryInsight] = []
    expired_reported: set[str] = set()
    expiry_conflict_reported: set[str] = set()
    unknown_reported: set[str] = set()

    for lot_states in lots_by_ingredient.values():
        lot_states.sort(
            key=lambda lot: (
                lot.expires_on is None,
                lot.expires_on or date.max,
                lot.lot_key,
            )
        )
        for lot in lot_states:
            if lot.expires_on is not None and lot.expires_on < payload.shopping_on:
                expired_reported.add(lot.lot_key)
                insights.append(
                    PantryInsight(
                        code="expired_before_shopping",
                        ingredient_key=lot.ingredient_key,
                        lot_key=lot.lot_key,
                        quantity=_float(lot.remaining),
                        unit=lot.unit,
                        message="Pantry stock expires before the shopping date.",
                    )
                )

    for requirement in sorted(
        requirements,
        key=lambda item: (item.needed_on, item.requirement_key),
    ):
        lots = lots_by_ingredient.get(requirement.ingredient_key, [])
        for lot in lots:
            if requirement.remaining <= 0:
                break
            if lot.remaining <= 0 or lot.lot_key in expired_reported:
                continue
            if lot.expires_on is not None and lot.expires_on < requirement.needed_on:
                if lot.lot_key not in expiry_conflict_reported:
                    expiry_conflict_reported.add(lot.lot_key)
                    insights.append(
                        PantryInsight(
                            code="expires_before_need",
                            ingredient_key=lot.ingredient_key,
                            lot_key=lot.lot_key,
                            requirement_key=requirement.requirement_key,
                            quantity=_float(lot.remaining),
                            unit=lot.unit,
                            message="Pantry stock expires before this planned requirement.",
                        )
                    )
                continue

            allocated = min(requirement.remaining, lot.remaining)
            requirement.remaining -= allocated
            lot.remaining -= allocated
            groups[requirement.ingredient_key].pantry_allocated += allocated
            allocations.append(
                PantryAllocation(
                    requirement_key=requirement.requirement_key,
                    lot_key=lot.lot_key,
                    ingredient_key=requirement.ingredient_key,
                    quantity=_float(allocated),
                    unit=requirement.unit,
                )
            )
            if lot.expires_on is None and lot.lot_key not in unknown_reported:
                unknown_reported.add(lot.lot_key)
                insights.append(
                    PantryInsight(
                        code="unknown_expiry_used",
                        ingredient_key=lot.ingredient_key,
                        lot_key=lot.lot_key,
                        requirement_key=requirement.requirement_key,
                        quantity=_float(allocated),
                        unit=lot.unit,
                        message="Pantry stock with unknown expiry was allocated last.",
                    )
                )

        group = groups[requirement.ingredient_key]
        if requirement.remaining > 0:
            group.requirement_shortages[requirement.requirement_key] = (
                requirement.remaining
            )
            group.purchase_required += requirement.remaining

    maximum_need = max(item.needed_on for item in requirements)
    for lot_states in lots_by_ingredient.values():
        for lot in lot_states:
            if (
                lot.remaining > 0
                and lot.expires_on is not None
                and payload.shopping_on <= lot.expires_on <= maximum_need
                and lot.lot_key not in expired_reported
            ):
                insights.append(
                    PantryInsight(
                        code="unused_expiring_stock",
                        ingredient_key=lot.ingredient_key,
                        lot_key=lot.lot_key,
                        quantity=_float(lot.remaining),
                        unit=lot.unit,
                        message="Relevant pantry stock remains unused within the plan horizon.",
                    )
                )

    allocations.sort(key=lambda row: (row.requirement_key, row.lot_key))
    insights.sort(
        key=lambda row: (
            row.ingredient_key,
            row.lot_key,
            row.requirement_key or "",
            row.code,
        )
    )
    return _AllocationResult(groups, allocations, insights)


def _enabled_retailers(payload: ShoppingPlanRequest) -> tuple[list[str], list[RetailerDiagnostic]]:
    requested = payload.allowed_retailers or list(RETAILER_NAMES)
    active: list[str] = []
    excluded: list[RetailerDiagnostic] = []
    for retailer in requested:
        adapter = get_adapter(retailer)
        if isinstance(adapter, DisabledAdapter):
            excluded.append(
                RetailerDiagnostic(
                    retailer=retailer,
                    status="excluded",
                    excluded_reason="Automated source disabled pending permission.",
                )
            )
        elif adapter is not None:
            active.append(retailer)
    return active, excluded


def _build_candidate_matrix(
    payload: ShoppingPlanRequest,
    groups: dict[str, _DemandGroup],
) -> _MatrixResult:
    active, diagnostics = _enabled_retailers(payload)
    shortages = [
        group
        for group in sorted(groups.values(), key=lambda item: item.ingredient_key)
        if group.purchase_required > 0
    ]
    matrix: dict[str, dict[str, list[QuantityCandidate]]] = {
        group.ingredient_key: {} for group in shortages
    }

    def search_retailer(retailer: str):
        adapter = get_adapter(retailer)
        assert adapter is not None and not isinstance(adapter, DisabledAdapter)
        started = time.monotonic()
        observed_at: datetime | None = None
        query_cache: dict[str, AdapterSearchOutcome] = {}
        local: dict[str, list[QuantityCandidate]] = {}
        issues: list[ShoppingCoverageIssue] = []
        errors: list[ShoppingSourceError] = []
        failure_code: str | None = None

        for group in shortages:
            if failure_code:
                errors.append(
                    ShoppingSourceError(
                        ingredient_key=group.ingredient_key,
                        retailer=retailer,
                        code=failure_code,
                        message=_SOURCE_MESSAGES.get(
                            failure_code, "Retailer data could not be retrieved."
                        ),
                    )
                )
                continue

            query_key = group.name.casefold()
            outcome = query_cache.get(query_key)
            if outcome is None:
                try:
                    outcome = adapter.search_with_status(group.name)
                except Exception:
                    outcome = AdapterSearchOutcome([], "source_unavailable")
                query_cache[query_key] = outcome
            if not outcome.is_available:
                failure_code = outcome.error_code or "source_unavailable"
                errors.append(
                    ShoppingSourceError(
                        ingredient_key=group.ingredient_key,
                        retailer=retailer,
                        code=failure_code,
                        message=_SOURCE_MESSAGES.get(
                            failure_code, "Retailer data could not be retrieved."
                        ),
                    )
                )
                continue

            now = datetime.now(timezone.utc)
            observed_at = max(
                [product.retrieved_at for product in outcome.products] + [now]
            )
            frontier, issue = quantity_candidate_frontier(
                group.name,
                group.normalized_purchase,
                [(product, retailer) for product in outcome.products],
                max_candidates=5,
            )
            if frontier:
                local[group.ingredient_key] = frontier
            elif issue:
                issues.append(
                    ShoppingCoverageIssue(
                        ingredient_key=group.ingredient_key,
                        code=issue.code,
                        message=issue.message,
                        candidate_product_name=issue.candidate_product_name,
                    )
                )
            else:
                issues.append(
                    ShoppingCoverageIssue(
                        ingredient_key=group.ingredient_key,
                        code="not_found",
                        message="No strongly related in-stock product was found.",
                    )
                )

        duration_ms = round((time.monotonic() - started) * 1000)
        diagnostic = RetailerDiagnostic(
            retailer=retailer,
            status="source_error" if errors else "available",
            duration_ms=duration_ms,
            observed_at=observed_at or datetime.now(timezone.utc),
            coverage_issues=issues,
            errors=errors,
        )
        return retailer, local, diagnostic

    workers = min(_MAX_WORKERS, len(active))
    if workers:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(search_retailer, retailer): retailer
                for retailer in active
            }
            for future in as_completed(futures):
                retailer = futures[future]
                try:
                    retailer, local, diagnostic = future.result()
                except Exception:
                    local = {}
                    diagnostic = RetailerDiagnostic(
                        retailer=retailer,
                        status="source_error",
                        observed_at=datetime.now(timezone.utc),
                        errors=[
                            ShoppingSourceError(
                                ingredient_key=group.ingredient_key,
                                retailer=retailer,
                                code="source_unavailable",
                                message=_SOURCE_MESSAGES["source_unavailable"],
                            )
                            for group in shortages
                        ],
                    )
                for ingredient_key, frontier in local.items():
                    matrix[ingredient_key][retailer] = frontier
                diagnostics.append(diagnostic)

    order = {key: index for index, key in enumerate(RETAILER_NAMES)}
    diagnostics.sort(key=lambda row: order.get(row.retailer, len(order)))
    source_uncertain = any(row.status == "source_error" for row in diagnostics)
    return _MatrixResult(matrix, diagnostics, active, source_uncertain)


def _dominates(left: _OptimizerState, right: _OptimizerState) -> bool:
    return (
        left.total_pence <= right.total_pence
        and left.unallocated_pence <= right.unallocated_pence
        and (
            left.total_pence < right.total_pence
            or left.unallocated_pence < right.unallocated_pence
        )
    )


def _insert_state(
    frontier: list[_OptimizerState],
    candidate: _OptimizerState,
) -> bool:
    for existing in frontier:
        if _dominates(existing, candidate):
            return False
        if (
            existing.total_pence == candidate.total_pence
            and existing.unallocated_pence == candidate.unallocated_pence
            and existing.signature <= candidate.signature
        ):
            return False
    frontier[:] = [state for state in frontier if not _dominates(candidate, state)]
    frontier.append(candidate)
    frontier.sort(
        key=lambda state: (
            state.total_pence,
            state.unallocated_pence,
            state.signature,
        )
    )
    return True


def _option_id(retailers: tuple[str, ...], state: _OptimizerState) -> str:
    payload = {
        "retailers": retailers,
        "assignments": [
            {
                "ingredient_key": ingredient_key,
                "retailer": choice.retailer,
                "external_id": choice.product.external_id,
                "packs": choice.packs_needed,
                "line_total": str(choice.line_total),
            }
            for ingredient_key, choice in state.assignments
        ],
    }
    return _hash("option", payload).split(":", 1)[1][:20]


def _build_option(
    retailers: tuple[str, ...],
    state: _OptimizerState,
    payload: ShoppingPlanRequest,
    groups: dict[str, _DemandGroup],
    optimality_proven: bool,
) -> ShoppingOption:
    option_id = _option_id(retailers, state)
    profiles = [payload.retailer_costs.get(retailer) for retailer in retailers]
    complete_costs = all(profile is not None for profile in profiles)
    fixed_pence = (
        sum(_pence(profile.fixed_cost_gbp) for profile in profiles if profile)
        if complete_costs
        else None
    )
    landed_pence = state.total_pence + fixed_pence if fixed_pence is not None else None

    checks: list[MinimumSpendCheck] = []
    for index, retailer in enumerate(retailers):
        profile = payload.retailer_costs.get(retailer)
        spend = state.spends[index]
        checks.append(
            MinimumSpendCheck(
                retailer=retailer,
                profile_supplied=profile is not None,
                merchandise_spend_gbp=_gbp(spend),
                minimum_spend_gbp=(profile.minimum_spend_gbp if profile else None),
                met=(spend >= _pence(profile.minimum_spend_gbp) if profile else None),
            )
        )

    manifest: list[PurchaseManifestLine] = []
    for ingredient_key, choice in state.assignments:
        group = groups[ingredient_key]
        requirement_allocations = [
            RequirementPurchaseAllocation(
                requirement_key=requirement_key,
                quantity=_float(amount),
                unit=group.unit,
            )
            for requirement_key, amount in sorted(group.requirement_shortages.items())
            if amount > 0
        ]
        manifest_identity = {
            "option_id": option_id,
            "ingredient_key": ingredient_key,
            "retailer": choice.retailer,
            "external_id": choice.product.external_id,
            "packs": choice.packs_needed,
        }
        line_id = _hash("manifest", manifest_identity).split(":", 1)[1][:20]
        manifest.append(
            PurchaseManifestLine(
                manifest_line_id=line_id,
                ingredient_key=ingredient_key,
                requirement_keys=[
                    allocation.requirement_key
                    for allocation in requirement_allocations
                ],
                retailer=choice.retailer,
                external_id=choice.product.external_id,
                product_name=choice.product.name,
                url=choice.product.url,
                retrieved_at=choice.product.retrieved_at,
                pack_price=choice.product.price,
                package_quantity=_float(choice.package.amount),
                package_unit=choice.package.unit,
                packs_needed=choice.packs_needed,
                purchased_quantity=_float(choice.supplied_base),
                planned_use_quantity=_float(choice.requested.base_amount),
                unallocated_quantity=_float(choice.excess_base),
                unallocated_value_gbp=_float(choice.unallocated_value),
                expected_line_cost=_float(choice.line_total),
                requirement_allocations=requirement_allocations,
            )
        )
    manifest.sort(key=lambda line: (line.ingredient_key, line.retailer, line.external_id))

    return ShoppingOption(
        option_id=option_id,
        type="single" if len(retailers) == 1 else "split",
        retailers=list(retailers),
        merchandise_total_gbp=_gbp(state.total_pence),
        fixed_cost_total_gbp=(_gbp(fixed_pence) if fixed_pence is not None else None),
        landed_total_gbp=(_gbp(landed_pence) if landed_pence is not None else None),
        cost_basis="landed" if complete_costs else "merchandise_only",
        minimum_spend_checks=checks,
        coverage_complete=True,
        optimality_proven=optimality_proven,
        decision_eligible=complete_costs and optimality_proven,
        unallocated_purchase_value_gbp=_gbp(state.unallocated_pence),
        purchase_manifest=manifest,
    )


def _optimize_retailer_set(
    retailers: tuple[str, ...],
    payload: ShoppingPlanRequest,
    groups: dict[str, _DemandGroup],
    matrix: dict[str, dict[str, list[QuantityCandidate]]],
    optimality_proven: bool,
) -> tuple[list[ShoppingOption], bool, list[str]]:
    shortage_keys = sorted(
        key for key, group in groups.items() if group.purchase_required > 0
    )
    variants: dict[str, list[QuantityCandidate]] = {}
    for ingredient_key in shortage_keys:
        choices = [
            choice
            for retailer in retailers
            for choice in matrix.get(ingredient_key, {}).get(retailer, [])
        ]
        if not choices:
            return [], False, []
        choices.sort(
            key=lambda choice: (
                choice.line_total,
                choice.unallocated_value,
                choice.retailer,
                choice.product.external_id,
            )
        )
        variants[ingredient_key] = choices

    minimums = tuple(
        _pence(payload.retailer_costs[retailer].minimum_spend_gbp)
        if retailer in payload.retailer_costs
        else 0
        for retailer in retailers
    )
    initial = _OptimizerState(
        spends=tuple(0 for _ in retailers),
        total_pence=0,
        unallocated_pence=0,
        used_mask=0,
        assignments=(),
        signature=(),
    )
    frontier: dict[tuple[tuple[int, ...], int], list[_OptimizerState]] = {
        (tuple(0 for _ in retailers), 0): [initial]
    }

    for ingredient_key in shortage_keys:
        next_frontier: dict[tuple[tuple[int, ...], int], list[_OptimizerState]] = {}
        entry_count = 0
        for states in frontier.values():
            for state in states:
                for choice in variants[ingredient_key]:
                    retailer_index = retailers.index(choice.retailer)
                    line_pence = _pence(choice.line_total)
                    unallocated_pence = _pence(choice.unallocated_value)
                    spends = list(state.spends)
                    spends[retailer_index] += line_pence
                    used_mask = state.used_mask | (1 << retailer_index)
                    capped = tuple(
                        min(spend, minimums[index])
                        for index, spend in enumerate(spends)
                    )
                    signature_entry = (
                        f"{ingredient_key}|{choice.retailer}|"
                        f"{choice.product.external_id}|{choice.packs_needed}"
                    )
                    candidate = _OptimizerState(
                        spends=tuple(spends),
                        total_pence=state.total_pence + line_pence,
                        unallocated_pence=(
                            state.unallocated_pence + unallocated_pence
                        ),
                        used_mask=used_mask,
                        assignments=state.assignments + ((ingredient_key, choice),),
                        signature=state.signature + (signature_entry,),
                    )
                    bucket = next_frontier.setdefault((capped, used_mask), [])
                    before = len(bucket)
                    _insert_state(bucket, candidate)
                    entry_count += len(bucket) - before
                    if entry_count > _MAX_OPTIMIZER_STATES:
                        return [], True, [
                            f"split_optimizer_limit:{','.join(retailers)}"
                        ]
        frontier = next_frontier

    required_mask = (1 << len(retailers)) - 1
    terminal: list[_OptimizerState] = []
    required_mask_seen = False
    for states in frontier.values():
        for state in states:
            if state.used_mask != required_mask:
                continue
            required_mask_seen = True
            if any(
                state.spends[index] < minimums[index]
                for index in range(len(retailers))
            ):
                continue
            _insert_state(terminal, state)
    if not terminal:
        issues = (
            [f"minimum_spend_unmet:{','.join(retailers)}"]
            if required_mask_seen
            else []
        )
        return [], False, issues

    return (
        [
            _build_option(retailers, state, payload, groups, optimality_proven)
            for state in terminal
        ],
        False,
        [],
    )


def _option_dominates(left: ShoppingOption, right: ShoppingOption) -> bool:
    assert left.landed_total_gbp is not None and right.landed_total_gbp is not None
    left_metrics = (
        len(left.retailers),
        left.landed_total_gbp,
        left.unallocated_purchase_value_gbp,
    )
    right_metrics = (
        len(right.retailers),
        right.landed_total_gbp,
        right.unallocated_purchase_value_gbp,
    )
    return all(a <= b for a, b in zip(left_metrics, right_metrics)) and any(
        a < b for a, b in zip(left_metrics, right_metrics)
    )


def _optimize(
    payload: ShoppingPlanRequest,
    allocation: _AllocationResult,
    matrix_result: _MatrixResult,
) -> _OptimizationResult:
    retailer_sets = [(retailer,) for retailer in matrix_result.active_retailers]
    retailer_sets.extend(combinations(matrix_result.active_retailers, 2))
    options: list[ShoppingOption] = []
    issues: list[str] = []
    limited = False
    for retailers in retailer_sets:
        generated, set_limited, set_issues = _optimize_retailer_set(
            tuple(retailers),
            payload,
            allocation.groups,
            matrix_result.matrix,
            optimality_proven=not matrix_result.source_uncertain,
        )
        options.extend(generated)
        limited = limited or set_limited
        issues.extend(set_issues)

    if limited:
        for option in options:
            option.optimality_proven = False
            option.decision_eligible = False

    unique = {option.option_id: option for option in options}
    options = list(unique.values())
    options.sort(
        key=lambda option: (
            not option.decision_eligible,
            len(option.retailers),
            option.landed_total_gbp
            if option.landed_total_gbp is not None
            else option.merchandise_total_gbp,
            option.unallocated_purchase_value_gbp,
            option.option_id,
        )
    )
    return _OptimizationResult(options, limited, sorted(set(issues)))


def _select_options_and_pareto(
    options: list[ShoppingOption],
) -> tuple[list[ShoppingOption], list[str], ParetoLabels]:
    eligible = [option for option in options if option.decision_eligible]
    pareto = [
        option
        for option in eligible
        if not any(
            other.option_id != option.option_id
            and _option_dominates(other, option)
            for other in eligible
        )
    ]
    pareto.sort(
        key=lambda option: (
            len(option.retailers),
            option.landed_total_gbp,
            option.unallocated_purchase_value_gbp,
            option.option_id,
        )
    )

    labels = ParetoLabels()
    if pareto:
        labels.fewest_stores = min(
            pareto,
            key=lambda option: (
                len(option.retailers),
                option.landed_total_gbp,
                option.unallocated_purchase_value_gbp,
                option.option_id,
            ),
        ).option_id
        labels.lowest_landed_cost = min(
            pareto,
            key=lambda option: (
                option.landed_total_gbp,
                len(option.retailers),
                option.unallocated_purchase_value_gbp,
                option.option_id,
            ),
        ).option_id
        labels.least_unallocated_value = min(
            pareto,
            key=lambda option: (
                option.unallocated_purchase_value_gbp,
                option.landed_total_gbp,
                len(option.retailers),
                option.option_id,
            ),
        ).option_id

    required_ids = {
        option_id
        for option_id in (
            labels.fewest_stores,
            labels.lowest_landed_cost,
            labels.least_unallocated_value,
        )
        if option_id
    }
    selected = [option for option in options if option.option_id in required_ids]
    selected_ids = {option.option_id for option in selected}
    selected.extend(
        option
        for option in options
        if option.option_id not in selected_ids
    )
    selected = selected[:_MAX_OPTIONS]
    selected_ids = {option.option_id for option in selected}
    pareto_ids = [
        option.option_id for option in pareto if option.option_id in selected_ids
    ]
    return selected, pareto_ids, labels


def _plan_fingerprint(payload: ShoppingPlanRequest, options: list[ShoppingOption]) -> str:
    evidence = [
        {
            "option_id": option.option_id,
            "lines": [
                {
                    "retailer": line.retailer,
                    "external_id": line.external_id,
                    "pack_price": line.pack_price,
                    "package_quantity": line.package_quantity,
                    "package_unit": line.package_unit,
                    "packs_needed": line.packs_needed,
                    "retrieved_at": line.retrieved_at.isoformat(),
                }
                for line in option.purchase_manifest
            ],
        }
        for option in options
    ]
    return _hash(
        "sha256",
        {
            "request": payload.model_dump(mode="json", exclude_none=False),
            "evidence": evidence,
        },
    )


def build_shopping_plan(payload: ShoppingPlanRequest) -> ShoppingPlanResponse:
    started = time.monotonic()
    generated_at = datetime.now(timezone.utc)
    allocation = allocate_pantry(payload)
    demand_summary = [
        DemandSummaryItem(
            ingredient_key=group.ingredient_key,
            name=group.name,
            unit=group.unit,
            total_required=_float(group.total_required),
            pantry_allocated=_float(group.pantry_allocated),
            purchase_required=_float(group.purchase_required),
        )
        for group in sorted(
            allocation.groups.values(), key=lambda item: item.ingredient_key
        )
    ]
    shortages = [
        group for group in allocation.groups.values() if group.purchase_required > 0
    ]

    if not shortages:
        option_id = _hash(
            "option",
            payload.model_dump(mode="json", exclude_none=False),
        ).split(":", 1)[1][:20]
        pantry_option = ShoppingOption(
            option_id=option_id,
            type="pantry_only",
            retailers=[],
            merchandise_total_gbp=0,
            fixed_cost_total_gbp=0,
            landed_total_gbp=0,
            cost_basis="landed",
            minimum_spend_checks=[],
            coverage_complete=True,
            optimality_proven=True,
            decision_eligible=True,
            unallocated_purchase_value_gbp=0,
            purchase_manifest=[],
        )
        response = ShoppingPlanResponse(
            plan_fingerprint=_plan_fingerprint(payload, [pantry_option]),
            generated_at=generated_at,
            shopping_on=payload.shopping_on,
            demand_summary=demand_summary,
            pantry_allocations=allocation.allocations,
            pantry_insights=allocation.insights,
            retailer_diagnostics=[],
            options=[pantry_option],
            pareto_option_ids=[option_id],
            pareto_labels=ParetoLabels(
                fewest_stores=option_id,
                lowest_landed_cost=option_id,
                least_unallocated_value=option_id,
            ),
            decision_status="pantry_only",
        )
    else:
        matrix_result = _build_candidate_matrix(payload, allocation.groups)
        optimization = _optimize(payload, allocation, matrix_result)
        missing_costs = any(
            retailer not in payload.retailer_costs
            for retailer in matrix_result.active_retailers
        )
        if missing_costs:
            for option in optimization.options:
                option.decision_eligible = False
        options, pareto_ids, labels = _select_options_and_pareto(
            optimization.options
        )
        if not options:
            status = (
                "optimization_limited"
                if optimization.limited
                else "no_complete_plan"
            )
        elif matrix_result.source_uncertain:
            status = "source_uncertain"
        elif optimization.limited:
            status = "optimization_limited"
        elif missing_costs:
            status = "needs_store_costs"
        else:
            status = "ready"
        issues = list(optimization.issues)
        if missing_costs:
            issues.append("missing_store_cost_profiles")
        if matrix_result.source_uncertain:
            issues.append("source_results_incomplete")
        if not options:
            issues.append("no_complete_plan")
        response = ShoppingPlanResponse(
            plan_fingerprint=_plan_fingerprint(payload, options),
            generated_at=generated_at,
            shopping_on=payload.shopping_on,
            demand_summary=demand_summary,
            pantry_allocations=allocation.allocations,
            pantry_insights=allocation.insights,
            retailer_diagnostics=matrix_result.diagnostics,
            options=options,
            pareto_option_ids=pareto_ids,
            pareto_labels=labels,
            decision_status=status,
            issues=sorted(set(issues)),
        )

    logger.info(
        json.dumps(
            {
                "event": "shopping_plan",
                "requirement_count": len(payload.requirements),
                "ingredient_count": len(allocation.groups),
                "pantry_lot_count": len(payload.pantry_lots),
                "shortage_count": len(shortages),
                "option_count": len(response.options),
                "decision_status": response.decision_status,
                "issue_codes": response.issues,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
            separators=(",", ":"),
        )
    )
    return response
