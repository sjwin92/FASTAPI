"""Public request and response contract for stateless shopping plans."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.quantities import BaseUnit, RequestUnit, normalize_requested_quantity


_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$"


def _clean_name(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("name must not be blank")
    if len(cleaned) > 120:
        raise ValueError("name must be at most 120 characters")
    return cleaned


def _validate_quantity(value: float, unit: RequestUnit) -> None:
    normalize_requested_quantity(value, unit)


class ShoppingRequirement(BaseModel):
    requirement_key: str = Field(pattern=_KEY_PATTERN)
    ingredient_key: str = Field(pattern=_KEY_PATTERN)
    name: str
    quantity: float
    unit: RequestUnit
    needed_on: date
    source_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        return _clean_name(value)

    @field_validator("quantity", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value):
        if isinstance(value, bool):
            raise ValueError("quantity must be a number")
        return value

    @field_validator("source_refs")
    @classmethod
    def clean_source_refs(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = " ".join(raw.split())
            if not value:
                raise ValueError("source references must not be blank")
            if len(value) > 120:
                raise ValueError("source references must be at most 120 characters")
            if value not in seen:
                cleaned.append(value)
                seen.add(value)
        return cleaned

    @model_validator(mode="after")
    def validate_quantity(self):
        _validate_quantity(self.quantity, self.unit)
        return self


class PantryLot(BaseModel):
    lot_key: str = Field(pattern=_KEY_PATTERN)
    ingredient_key: str = Field(pattern=_KEY_PATTERN)
    available_quantity: float
    unit: RequestUnit
    expires_on: date | None = None

    @field_validator("available_quantity", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value):
        if isinstance(value, bool):
            raise ValueError("available_quantity must be a number")
        return value

    @model_validator(mode="after")
    def validate_quantity(self):
        _validate_quantity(self.available_quantity, self.unit)
        return self


class RetailerCostProfile(BaseModel):
    method: Literal["in_store", "collection", "delivery"]
    fixed_cost_gbp: float
    minimum_spend_gbp: float

    @field_validator("fixed_cost_gbp", "minimum_spend_gbp", mode="before")
    @classmethod
    def validate_money(cls, value):
        if isinstance(value, bool):
            raise ValueError("money values must be numbers")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("money values must be numbers") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError("money values must be finite and non-negative")
        if amount.as_tuple().exponent < -2:
            raise ValueError("money values must have at most two decimal places")
        return float(amount)


class ShoppingPlanRequest(BaseModel):
    shopping_on: date
    requirements: list[ShoppingRequirement] = Field(min_length=1, max_length=100)
    pantry_lots: list[PantryLot] = Field(default_factory=list, max_length=250)
    allowed_retailers: list[str] | None = Field(default=None, max_length=20)
    retailer_costs: dict[str, RetailerCostProfile] = Field(default_factory=dict)

    @field_validator("retailer_costs", mode="before")
    @classmethod
    def clean_retailer_cost_keys(cls, value):
        if not isinstance(value, dict):
            return value
        if len(value) > 20:
            raise ValueError("at most 20 retailer cost profiles are supported")
        cleaned = {}
        for raw_key, profile in value.items():
            key = str(raw_key).strip().casefold()
            if not key:
                raise ValueError("retailer cost keys must not be blank")
            if key in cleaned:
                raise ValueError("retailer cost keys must be unique")
            cleaned[key] = profile
        return cleaned

    @field_validator("allowed_retailers")
    @classmethod
    def clean_retailers(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = raw.strip().casefold()
            if not value:
                raise ValueError("retailer keys must not be blank")
            if value not in seen:
                cleaned.append(value)
                seen.add(value)
        if not cleaned:
            raise ValueError("allowed_retailers must not be empty")
        return cleaned

    @model_validator(mode="after")
    def validate_plan(self):
        requirement_keys = [item.requirement_key for item in self.requirements]
        if len(requirement_keys) != len(set(requirement_keys)):
            raise ValueError("requirement_key values must be unique")

        ingredient_keys = {item.ingredient_key for item in self.requirements}
        if len(ingredient_keys) > 50:
            raise ValueError("at most 50 unique ingredient_key values are supported")

        maximum_date = self.shopping_on + timedelta(days=90)
        identity: dict[str, tuple[str, str]] = {}
        for item in self.requirements:
            if not self.shopping_on <= item.needed_on <= maximum_date:
                raise ValueError(
                    "needed_on must be between shopping_on and 90 days later"
                )
            normalized = normalize_requested_quantity(item.quantity, item.unit)
            signature = (item.name.casefold(), normalized.dimension)
            existing = identity.get(item.ingredient_key)
            if existing and existing != signature:
                raise ValueError(
                    "rows sharing ingredient_key must use the same name and dimension"
                )
            identity[item.ingredient_key] = signature

        lot_keys = [lot.lot_key for lot in self.pantry_lots]
        if len(lot_keys) != len(set(lot_keys)):
            raise ValueError("lot_key values must be unique")
        for lot in self.pantry_lots:
            expected = identity.get(lot.ingredient_key)
            if expected is None:
                raise ValueError("pantry lots must reference a requested ingredient_key")
            normalized = normalize_requested_quantity(
                lot.available_quantity, lot.unit
            )
            if normalized.dimension != expected[1]:
                raise ValueError(
                    "pantry lot dimension must match its ingredient requirements"
                )

        if self.allowed_retailers is not None:
            extra_profiles = set(self.retailer_costs) - set(self.allowed_retailers)
            if extra_profiles:
                raise ValueError(
                    "retailer_costs keys must be present in allowed_retailers"
                )
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "shopping_on": "2026-09-01",
                    "requirements": [
                        {
                            "requirement_key": "meal-42:milk",
                            "ingredient_key": "ingredient:milk:fresh",
                            "name": "fresh milk",
                            "quantity": 500,
                            "unit": "ml",
                            "needed_on": "2026-09-02",
                            "source_refs": ["meal-42"],
                        }
                    ],
                    "pantry_lots": [
                        {
                            "lot_key": "pantry-lot-901",
                            "ingredient_key": "ingredient:milk:fresh",
                            "available_quantity": 250,
                            "unit": "ml",
                            "expires_on": "2026-09-02",
                        }
                    ],
                    "allowed_retailers": [
                        "ocado",
                        "morrisons",
                        "sainsburys",
                    ],
                    "retailer_costs": {
                        "ocado": {
                            "method": "delivery",
                            "fixed_cost_gbp": 3.99,
                            "minimum_spend_gbp": 40,
                        }
                    },
                }
            ]
        }
    }


class DemandSummaryItem(BaseModel):
    ingredient_key: str
    name: str
    unit: BaseUnit
    total_required: float
    pantry_allocated: float
    purchase_required: float


class PantryAllocation(BaseModel):
    requirement_key: str
    lot_key: str
    ingredient_key: str
    quantity: float
    unit: BaseUnit


class PantryInsight(BaseModel):
    code: Literal[
        "expired_before_shopping",
        "expires_before_need",
        "unknown_expiry_used",
        "unused_expiring_stock",
    ]
    ingredient_key: str
    lot_key: str
    requirement_key: str | None = None
    quantity: float
    unit: BaseUnit
    message: str


class ShoppingCoverageIssue(BaseModel):
    ingredient_key: str
    code: Literal[
        "not_found",
        "no_acceptable_variant",
        "package_size_unknown",
        "unit_incompatible",
    ]
    message: str
    candidate_product_name: str | None = None


class ShoppingSourceError(BaseModel):
    ingredient_key: str
    retailer: str
    code: str
    message: str


class RetailerDiagnostic(BaseModel):
    retailer: str
    status: Literal["available", "source_error", "excluded"]
    excluded_reason: str | None = None
    duration_ms: int = 0
    observed_at: datetime | None = None
    coverage_issues: list[ShoppingCoverageIssue] = Field(default_factory=list)
    errors: list[ShoppingSourceError] = Field(default_factory=list)


class RequirementPurchaseAllocation(BaseModel):
    requirement_key: str
    quantity: float
    unit: BaseUnit


class PurchaseManifestLine(BaseModel):
    manifest_line_id: str
    ingredient_key: str
    requirement_keys: list[str]
    retailer: str
    external_id: str
    product_name: str
    url: str
    retrieved_at: datetime
    pack_price: float
    package_quantity: float
    package_unit: BaseUnit
    packs_needed: int
    purchased_quantity: float
    planned_use_quantity: float
    unallocated_quantity: float
    unallocated_value_gbp: float
    expected_line_cost: float
    requirement_allocations: list[RequirementPurchaseAllocation]
    requires_checkout_confirmation: bool = True


class MinimumSpendCheck(BaseModel):
    retailer: str
    profile_supplied: bool
    merchandise_spend_gbp: float
    minimum_spend_gbp: float | None = None
    met: bool | None = None


class ShoppingOption(BaseModel):
    option_id: str
    type: Literal["pantry_only", "single", "split"]
    retailers: list[str]
    merchandise_total_gbp: float
    fixed_cost_total_gbp: float | None = None
    landed_total_gbp: float | None = None
    cost_basis: Literal["landed", "merchandise_only"]
    minimum_spend_checks: list[MinimumSpendCheck]
    coverage_complete: bool
    optimality_proven: bool
    decision_eligible: bool
    unallocated_purchase_value_gbp: float
    purchase_manifest: list[PurchaseManifestLine]


class ParetoLabels(BaseModel):
    fewest_stores: str | None = None
    lowest_landed_cost: str | None = None
    least_unallocated_value: str | None = None


class ShoppingPlanResponse(BaseModel):
    plan_fingerprint: str
    generated_at: datetime
    shopping_on: date
    demand_summary: list[DemandSummaryItem]
    pantry_allocations: list[PantryAllocation]
    pantry_insights: list[PantryInsight]
    retailer_diagnostics: list[RetailerDiagnostic]
    options: list[ShoppingOption]
    pareto_option_ids: list[str]
    pareto_labels: ParetoLabels
    decision_status: Literal[
        "pantry_only",
        "ready",
        "needs_store_costs",
        "source_uncertain",
        "optimization_limited",
        "no_complete_plan",
    ]
    issues: list[str] = Field(default_factory=list)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "plan_fingerprint": "sha256:example",
                    "generated_at": "2026-09-01T09:00:00Z",
                    "shopping_on": "2026-09-01",
                    "demand_summary": [
                        {
                            "ingredient_key": "ingredient:milk:fresh",
                            "name": "fresh milk",
                            "unit": "ml",
                            "total_required": 500,
                            "pantry_allocated": 250,
                            "purchase_required": 250,
                        }
                    ],
                    "pantry_allocations": [],
                    "pantry_insights": [],
                    "retailer_diagnostics": [],
                    "options": [],
                    "pareto_option_ids": [],
                    "pareto_labels": {},
                    "decision_status": "no_complete_plan",
                    "issues": ["No complete retailer plan was available."],
                }
            ]
        }
    }
