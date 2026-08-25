from __future__ import annotations

import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.base import AdapterSearchOutcome, DisabledAdapter, ProductResult
from app.services.price_sync import quantity_candidate_frontier
from app.quantities import normalize_requested_quantity
from app.shopping_schemas import ShoppingPlanRequest
from app.services.shopping_plan import allocate_pantry, build_shopping_plan


def _request(**overrides) -> ShoppingPlanRequest:
    data = {
        "shopping_on": "2026-09-01",
        "requirements": [
            {
                "requirement_key": "meal-1:milk",
                "ingredient_key": "milk",
                "name": "milk",
                "quantity": 500,
                "unit": "ml",
                "needed_on": "2026-09-02",
            }
        ],
        "pantry_lots": [],
        "allowed_retailers": ["shop"],
        "retailer_costs": {
            "shop": {
                "method": "in_store",
                "fixed_cost_gbp": 0,
                "minimum_spend_gbp": 0,
            }
        },
    }
    data.update(overrides)
    return ShoppingPlanRequest.model_validate(data)


def _adapter(key: str, by_query: dict[str, AdapterSearchOutcome]):
    adapter = MagicMock()
    adapter.retailer_key = key
    adapter.search_with_status.side_effect = lambda query: by_query[query]
    return adapter


def _product(
    external_id: str,
    name: str,
    price: float,
    *,
    retrieved_at: datetime | None = None,
) -> ProductResult:
    return ProductResult(
        external_id=external_id,
        name=name,
        url=f"https://example/{external_id}",
        price=price,
        retrieved_at=retrieved_at or datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_fefo_allocation_respects_requirement_dates_and_conserves_quantity():
    payload = _request(
        requirements=[
            {
                "requirement_key": "r1",
                "ingredient_key": "milk",
                "name": "milk",
                "quantity": 300,
                "unit": "ml",
                "needed_on": "2026-09-02",
            },
            {
                "requirement_key": "r2",
                "ingredient_key": "milk",
                "name": "milk",
                "quantity": 300,
                "unit": "ml",
                "needed_on": "2026-09-05",
            },
        ],
        pantry_lots=[
            {
                "lot_key": "early",
                "ingredient_key": "milk",
                "available_quantity": 200,
                "unit": "ml",
                "expires_on": "2026-09-02",
            },
            {
                "lot_key": "later",
                "ingredient_key": "milk",
                "available_quantity": 500,
                "unit": "ml",
                "expires_on": "2026-09-05",
            },
        ],
    )

    result = allocate_pantry(payload)

    assert [(row.requirement_key, row.lot_key, row.quantity) for row in result.allocations] == [
        ("r1", "early", 200.0),
        ("r1", "later", 100.0),
        ("r2", "later", 300.0),
    ]
    group = result.groups["milk"]
    assert group.total_required == 600
    assert group.pantry_allocated == 600
    assert group.purchase_required == 0


def test_expired_and_too_early_lots_are_not_allocated():
    payload = _request(
        pantry_lots=[
            {
                "lot_key": "expired",
                "ingredient_key": "milk",
                "available_quantity": 100,
                "unit": "ml",
                "expires_on": "2026-08-31",
            },
            {
                "lot_key": "too-early",
                "ingredient_key": "milk",
                "available_quantity": 200,
                "unit": "ml",
                "expires_on": "2026-09-01",
            },
        ]
    )

    result = allocate_pantry(payload)

    assert result.allocations == []
    assert result.groups["milk"].purchase_required == 500
    assert {insight.code for insight in result.insights} >= {
        "expired_before_shopping",
        "expires_before_need",
    }


def test_unknown_expiry_is_allocated_after_known_stock_and_disclosed():
    payload = _request(
        pantry_lots=[
            {
                "lot_key": "known",
                "ingredient_key": "milk",
                "available_quantity": 200,
                "unit": "ml",
                "expires_on": "2026-09-02",
            },
            {
                "lot_key": "unknown",
                "ingredient_key": "milk",
                "available_quantity": 500,
                "unit": "ml",
                "expires_on": None,
            },
        ]
    )

    result = allocate_pantry(payload)

    assert [row.lot_key for row in result.allocations] == ["known", "unknown"]
    assert any(insight.code == "unknown_expiry_used" for insight in result.insights)


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"shop": "Shop"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_pantry_only_plan_never_calls_a_retailer(get_adapter):
    payload = _request(
        pantry_lots=[
            {
                "lot_key": "enough",
                "ingredient_key": "milk",
                "available_quantity": 500,
                "unit": "ml",
                "expires_on": "2026-09-02",
            }
        ]
    )

    result = build_shopping_plan(payload)

    assert result.decision_status == "pantry_only"
    assert result.options[0].type == "pantry_only"
    assert result.options[0].purchase_manifest == []
    get_adapter.assert_not_called()


def test_candidate_frontier_preserves_cost_surplus_tradeoff():
    requested = normalize_requested_quantity(600, "g")
    candidates = [
        (_product("cheap", "Rice 1kg", 1.00), "shop"),
        (_product("exact", "Rice 600g", 1.20), "shop"),
        (_product("dominated", "Rice 1kg", 1.50), "shop"),
    ]

    frontier, issue = quantity_candidate_frontier("rice", requested, candidates)

    assert issue is None
    assert [choice.product.external_id for choice in frontier] == ["cheap", "exact"]
    assert frontier[0].unallocated_value > frontier[1].unallocated_value


def test_candidate_frontier_rejects_unrequested_material_forms():
    requested = normalize_requested_quantity(1, "l")
    candidates = [(_product("uht", "UHT Milk 1L", 0.50), "shop")]

    frontier, issue = quantity_candidate_frontier("milk", requested, candidates)

    assert frontier == []
    assert issue is not None
    assert issue.code == "no_acceptable_variant"


def _two_item_request(costs=None) -> ShoppingPlanRequest:
    return _request(
        requirements=[
            {
                "requirement_key": "milk-r",
                "ingredient_key": "milk",
                "name": "milk",
                "quantity": 1,
                "unit": "l",
                "needed_on": "2026-09-02",
            },
            {
                "requirement_key": "pasta-r",
                "ingredient_key": "pasta",
                "name": "pasta",
                "quantity": 500,
                "unit": "g",
                "needed_on": "2026-09-03",
            },
        ],
        allowed_retailers=["a", "b"],
        retailer_costs=costs
        if costs is not None
        else {
            "a": {
                "method": "in_store",
                "fixed_cost_gbp": 0,
                "minimum_spend_gbp": 0,
            },
            "b": {
                "method": "in_store",
                "fixed_cost_gbp": 0,
                "minimum_spend_gbp": 0,
            },
        },
    )


def _two_retailer_adapters():
    return {
        "a": _adapter(
            "a",
            {
                "milk": AdapterSearchOutcome([_product("a-milk", "Milk 1L", 2)]),
                "pasta": AdapterSearchOutcome([_product("a-pasta", "Pasta 500g", 4)]),
            },
        ),
        "b": _adapter(
            "b",
            {
                "milk": AdapterSearchOutcome([_product("b-milk", "Milk 1L", 4)]),
                "pasta": AdapterSearchOutcome([_product("b-pasta", "Pasta 500g", 2)]),
            },
        ),
    }


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_returns_single_and_split_options_with_pareto_labels(get_adapter):
    adapters = _two_retailer_adapters()
    get_adapter.side_effect = adapters.get

    result = build_shopping_plan(_two_item_request())

    assert result.decision_status == "ready"
    assert any(option.type == "single" for option in result.options)
    split = next(option for option in result.options if option.type == "split")
    assert split.merchandise_total_gbp == 4
    assert split.landed_total_gbp == 4
    assert len(split.retailers) == 2
    cheapest = result.pareto_labels.lowest_landed_cost
    assert next(o for o in result.options if o.option_id == cheapest).type == "split"
    for line in split.purchase_manifest:
        assert line.purchased_quantity == (
            line.planned_use_quantity + line.unallocated_quantity
        )
        assert line.requires_checkout_confirmation is True


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"shop": "Shop"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_pareto_keeps_cheapest_and_least_surplus_package_choices(get_adapter):
    adapter = _adapter(
        "shop",
        {
            "rice": AdapterSearchOutcome(
                [
                    _product("cheap", "Rice 1kg", 1.00),
                    _product("exact", "Rice 600g", 1.20),
                ]
            )
        },
    )
    get_adapter.return_value = adapter
    payload = _request(
        requirements=[
            {
                "requirement_key": "rice-r",
                "ingredient_key": "rice",
                "name": "rice",
                "quantity": 600,
                "unit": "g",
                "needed_on": "2026-09-02",
            }
        ]
    )

    result = build_shopping_plan(payload)

    cheapest = next(
        option
        for option in result.options
        if option.option_id == result.pareto_labels.lowest_landed_cost
    )
    least_surplus = next(
        option
        for option in result.options
        if option.option_id == result.pareto_labels.least_unallocated_value
    )
    assert cheapest.purchase_manifest[0].external_id == "cheap"
    assert least_surplus.purchase_manifest[0].external_id == "exact"
    assert set(result.pareto_option_ids) == {
        cheapest.option_id,
        least_surplus.option_id,
    }


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_fixed_store_cost_can_make_single_store_preferable(get_adapter):
    adapters = _two_retailer_adapters()
    get_adapter.side_effect = adapters.get
    costs = {
        "a": {
            "method": "in_store",
            "fixed_cost_gbp": 0,
            "minimum_spend_gbp": 0,
        },
        "b": {
            "method": "in_store",
            "fixed_cost_gbp": 4,
            "minimum_spend_gbp": 0,
        },
    }

    result = build_shopping_plan(_two_item_request(costs))

    cheapest_id = result.pareto_labels.lowest_landed_cost
    cheapest = next(option for option in result.options if option.option_id == cheapest_id)
    assert cheapest.type == "single"
    assert cheapest.retailers == ["a"]
    assert cheapest.landed_total_gbp == 6


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_minimum_spend_can_force_a_different_exact_assignment(get_adapter):
    adapters = {
        "a": _adapter(
            "a",
            {
                "milk": AdapterSearchOutcome([_product("a-milk", "Milk 1L", 1)]),
                "pasta": AdapterSearchOutcome([_product("a-pasta", "Pasta 500g", 5)]),
            },
        ),
        "b": _adapter(
            "b",
            {
                "milk": AdapterSearchOutcome([_product("b-milk", "Milk 1L", 2)]),
                "pasta": AdapterSearchOutcome([_product("b-pasta", "Pasta 500g", 1)]),
            },
        ),
    }
    get_adapter.side_effect = adapters.get
    costs = {
        "a": {
            "method": "delivery",
            "fixed_cost_gbp": 0,
            "minimum_spend_gbp": 5,
        },
        "b": {
            "method": "in_store",
            "fixed_cost_gbp": 0,
            "minimum_spend_gbp": 0,
        },
    }

    result = build_shopping_plan(_two_item_request(costs))

    split = next(option for option in result.options if option.type == "split")
    checks = {check.retailer: check for check in split.minimum_spend_checks}
    assert checks["a"].merchandise_spend_gbp == 5
    assert checks["a"].met is True
    assert {line.external_id for line in split.purchase_manifest} == {
        "a-pasta",
        "b-milk",
    }


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_pair_without_lines_from_b_is_not_called_a_minimum_spend_failure(
    get_adapter,
):
    adapters = {
        "a": _adapter(
            "a",
            {
                "milk": AdapterSearchOutcome([_product("a-milk", "Milk 1L", 1)]),
                "pasta": AdapterSearchOutcome([_product("a-pasta", "Pasta 500g", 1)]),
            },
        ),
        "b": _adapter(
            "b",
            {
                "milk": AdapterSearchOutcome([]),
                "pasta": AdapterSearchOutcome([]),
            },
        ),
    }
    get_adapter.side_effect = adapters.get

    result = build_shopping_plan(_two_item_request())

    assert all(not issue.startswith("minimum_spend_unmet") for issue in result.issues)
    assert all(option.type != "split" for option in result.options)


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_missing_cost_profiles_prevent_recommendation(get_adapter):
    adapters = _two_retailer_adapters()
    get_adapter.side_effect = adapters.get
    payload = _two_item_request(costs={})

    result = build_shopping_plan(payload)

    assert result.decision_status == "needs_store_costs"
    assert result.pareto_option_ids == []
    assert all(option.decision_eligible is False for option in result.options)
    assert any(option.type == "split" for option in result.options)


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_one_missing_profile_blocks_global_landed_cost_labels(get_adapter):
    adapters = _two_retailer_adapters()
    get_adapter.side_effect = adapters.get
    costs = {
        "a": {
            "method": "in_store",
            "fixed_cost_gbp": 0,
            "minimum_spend_gbp": 0,
        }
    }

    result = build_shopping_plan(_two_item_request(costs))

    assert result.decision_status == "needs_store_costs"
    assert result.pareto_option_ids == []
    assert result.pareto_labels.lowest_landed_cost is None
    assert all(option.decision_eligible is False for option in result.options)


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_source_failure_prevents_optimality_claims_and_stops_queries(get_adapter):
    adapters = _two_retailer_adapters()
    adapters["b"].search_with_status.side_effect = [
        AdapterSearchOutcome([], "source_unavailable")
    ]
    get_adapter.side_effect = adapters.get

    result = build_shopping_plan(_two_item_request())

    assert result.decision_status == "source_uncertain"
    assert all(option.optimality_proven is False for option in result.options)
    assert adapters["b"].search_with_status.call_count == 1


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"shop": "Shop"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_reuses_one_search_for_distinct_keys_with_the_same_name(get_adapter):
    adapter = _adapter(
        "shop",
        {"milk": AdapterSearchOutcome([_product("milk", "Milk 1L", 1)])},
    )
    get_adapter.return_value = adapter
    payload = _request(
        requirements=[
            {
                "requirement_key": "r1",
                "ingredient_key": "milk-one",
                "name": "milk",
                "quantity": 500,
                "unit": "ml",
                "needed_on": "2026-09-02",
            },
            {
                "requirement_key": "r2",
                "ingredient_key": "milk-two",
                "name": "milk",
                "quantity": 250,
                "unit": "ml",
                "needed_on": "2026-09-03",
            },
        ]
    )

    result = build_shopping_plan(payload)

    assert result.decision_status == "ready"
    assert adapter.search_with_status.call_count == 1
    assert len(result.options[0].purchase_manifest) == 2


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"tesco": "Tesco"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_policy_disabled_retailer_is_excluded_without_search(get_adapter):
    disabled = DisabledAdapter("tesco")
    get_adapter.return_value = disabled
    payload = _request(
        allowed_retailers=["tesco"],
        retailer_costs={},
    )

    result = build_shopping_plan(payload)

    assert result.decision_status == "no_complete_plan"
    assert result.retailer_diagnostics[0].status == "excluded"
    assert not hasattr(disabled, "_search_state")


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"a": "A", "b": "B"}, clear=True)
@patch("app.services.shopping_plan._MAX_OPTIMIZER_STATES", 1)
@patch("app.services.shopping_plan.get_adapter")
def test_optimizer_limit_omits_split_instead_of_approximating(get_adapter):
    adapters = _two_retailer_adapters()
    get_adapter.side_effect = adapters.get

    result = build_shopping_plan(_two_item_request())

    assert result.decision_status == "optimization_limited"
    assert all(option.type != "split" for option in result.options)
    assert all(option.decision_eligible is False for option in result.options)
    assert any(issue.startswith("split_optimizer_limit") for issue in result.issues)


@patch.dict("app.services.shopping_plan.RETAILER_NAMES", {"shop": "Shop"}, clear=True)
@patch("app.services.shopping_plan.get_adapter")
def test_fingerprint_is_deterministic_and_logs_no_household_text(
    get_adapter, caplog
):
    adapter = _adapter(
        "shop",
        {"secret family milk": AdapterSearchOutcome([
            _product("milk", "Secret Family Milk 500ml", 1)
        ])},
    )
    get_adapter.return_value = adapter
    payload = _request(
        requirements=[
            {
                "requirement_key": "private-meal",
                "ingredient_key": "private-ingredient",
                "name": "secret family milk",
                "quantity": 500,
                "unit": "ml",
                "needed_on": "2026-09-02",
            }
        ]
    )

    with caplog.at_level(logging.INFO, logger="app.services.shopping_plan"):
        first = build_shopping_plan(payload)
        second = build_shopping_plan(payload)

    assert first.plan_fingerprint == second.plan_fingerprint
    log_text = " ".join(record.message for record in caplog.records)
    assert "secret family milk" not in log_text
    assert "private-meal" not in log_text
    assert "private-ingredient" not in log_text


def test_endpoint_validates_unknown_retailer_and_exposes_openapi(client):
    unknown = client.post(
        "/shopping/plan",
        json={
            "shopping_on": "2026-09-01",
            "requirements": [
                {
                    "requirement_key": "r1",
                    "ingredient_key": "milk",
                    "name": "milk",
                    "quantity": 1,
                    "unit": "l",
                    "needed_on": "2026-09-02",
                }
            ],
            "allowed_retailers": ["unknown-shop"],
        },
    )
    schema = client.get("/openapi.json").json()

    assert unknown.status_code == 422
    assert schema["info"]["version"] == "1.3.0"
    assert "/shopping/plan" in schema["paths"]


def test_shopping_plan_uses_optional_service_auth(monkeypatch, client):
    monkeypatch.setenv("BASKET_API_KEY", "shopping-secret")
    payload = {
        "shopping_on": "2026-09-01",
        "requirements": [
            {
                "requirement_key": "r1",
                "ingredient_key": "milk",
                "name": "milk",
                "quantity": 1,
                "unit": "l",
                "needed_on": "2026-09-02",
            }
        ],
    }

    assert client.post("/shopping/plan", json=payload).status_code == 401
    with patch("app.main.build_shopping_plan") as build:
        build.return_value = build_shopping_plan(
            ShoppingPlanRequest.model_validate(
                {
                    **payload,
                    "pantry_lots": [
                        {
                            "lot_key": "enough",
                            "ingredient_key": "milk",
                            "available_quantity": 1,
                            "unit": "l",
                            "expires_on": "2026-09-02",
                        }
                    ],
                }
            )
        )
        response = client.post(
            "/shopping/plan",
            json=payload,
            headers={"Authorization": "Bearer shopping-secret"},
        )
    assert response.status_code == 200


def test_request_requires_stable_unique_keys_and_compatible_identity():
    base = _request().model_dump(mode="json")
    duplicate = {
        **base,
        "requirements": base["requirements"] * 2,
    }
    conflicting = {
        **base,
        "requirements": [
            base["requirements"][0],
            {
                **base["requirements"][0],
                "requirement_key": "r2",
                "name": "oat milk",
            },
        ],
    }
    incompatible_lot = {
        **base,
        "pantry_lots": [
            {
                "lot_key": "bad-lot",
                "ingredient_key": "milk",
                "available_quantity": 1,
                "unit": "each",
            }
        ],
    }

    for invalid in (duplicate, conflicting, incompatible_lot):
        with pytest.raises(ValueError):
            ShoppingPlanRequest.model_validate(invalid)


def test_request_enforces_collection_and_unique_ingredient_limits():
    base_requirement = _request().model_dump(mode="json")["requirements"][0]
    too_many_rows = {
        "shopping_on": "2026-09-01",
        "requirements": [
            {
                **base_requirement,
                "requirement_key": f"row-{index}",
            }
            for index in range(101)
        ],
    }
    too_many_ingredients = {
        "shopping_on": "2026-09-01",
        "requirements": [
            {
                **base_requirement,
                "requirement_key": f"row-{index}",
                "ingredient_key": f"ingredient-{index}",
            }
            for index in range(51)
        ],
    }
    too_many_refs = {
        "shopping_on": "2026-09-01",
        "requirements": [
            {
                **base_requirement,
                "source_refs": [f"meal-{index}" for index in range(21)],
            }
        ],
    }
    too_many_lots = {
        "shopping_on": "2026-09-01",
        "requirements": [base_requirement],
        "pantry_lots": [
            {
                "lot_key": f"lot-{index}",
                "ingredient_key": "milk",
                "available_quantity": 1,
                "unit": "ml",
            }
            for index in range(251)
        ],
    }

    for invalid in (
        too_many_rows,
        too_many_ingredients,
        too_many_refs,
        too_many_lots,
    ):
        with pytest.raises(ValueError):
            ShoppingPlanRequest.model_validate(invalid)


def test_request_rejects_fractional_each():
    data = _request().model_dump(mode="json")
    data["requirements"][0].update({"quantity": 1.5, "unit": "each"})
    with pytest.raises(ValueError):
        ShoppingPlanRequest.model_validate(data)


@pytest.mark.parametrize(
    "mutation",
    [
        {"requirements.0.quantity": 0},
        {"requirements.0.unit": "cup"},
        {"requirements.0.needed_on": "2026-12-15"},
        {"retailer_costs.shop.fixed_cost_gbp": 1.005},
    ],
)
def test_request_validation_rejects_invalid_quantities_dates_and_money(mutation):
    data = _request().model_dump(mode="json")
    for path, value in mutation.items():
        target = data
        parts = path.split(".")
        for part in parts[:-1]:
            target = target[int(part)] if part.isdigit() else target[part]
        target[parts[-1]] = value
    with pytest.raises(ValueError):
        ShoppingPlanRequest.model_validate(data)
