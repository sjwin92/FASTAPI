from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.quantities import RequestUnit, amount_in_unit, normalize_requested_quantity


class ProductResult(BaseModel):
    external_id: str
    name: str
    retailer: str
    url: str
    image_url: str | None = None
    price: float
    unit_price: float | None = None
    unit: str | None = None
    in_stock: bool = True


class TrackRequest(BaseModel):
    retailer: str
    external_id: str
    name: str
    url: str
    image_url: str | None = None
    alert_threshold: float | None = None


class TrackResponse(BaseModel):
    tracked_product_id: int
    product_id: int
    message: str


class PricePoint(BaseModel):
    price: float
    unit_price: float | None
    unit: str | None
    in_stock: bool
    captured_at: datetime

    model_config = {"from_attributes": True}


class PriceHistory(BaseModel):
    product_id: int
    name: str
    retailer: str
    url: str
    history: list[PricePoint]

    model_config = {"from_attributes": True}


class RetailerInfo(BaseModel):
    key: str
    name: str
    enabled: bool = True
    disabled_reason: str | None = None
    capabilities: list[str] = Field(default_factory=list)


# --- Price sync / basket compare ---

class PriceSyncRequest(BaseModel):
    ingredients: list[str] = Field(max_length=50)
    retailer: str | None = None
    write_to_supabase: bool = False

    @field_validator("ingredients")
    @classmethod
    def clean_ingredients(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw in values:
            value = " ".join(raw.split())
            if len(value) > 120:
                raise ValueError("each ingredient must be at most 120 characters")
            key = value.casefold()
            if value and key not in seen:
                cleaned.append(value)
                seen.add(key)
        return cleaned


class PriceSyncItem(BaseModel):
    ingredient: str
    product_name: str
    retailer: str
    price: float
    unit_price: float | None = None
    unit: str | None = None
    url: str
    image_url: str | None = None


class PriceSyncResponse(BaseModel):
    synced: list[PriceSyncItem]
    not_found: list[str]
    errors: list["AdapterErrorInfo"] = Field(default_factory=list)
    written_to_supabase: bool = False


class BasketItem(BaseModel):
    ingredient: str
    product_name: str
    price: float
    unit_price: float | None = None
    unit: str | None = None
    url: str
    image_url: str | None = None
    retrieved_at: datetime | None = None
    requested_quantity: float | None = None
    requested_unit: RequestUnit | None = None
    package_quantity: float | None = None
    package_unit: Literal["g", "ml", "each"] | None = None
    packs_needed: int = 1
    supplied_quantity: float | None = None
    excess_quantity: float | None = None
    line_total: float | None = None


class AdapterErrorInfo(BaseModel):
    ingredient: str
    retailer: str
    code: str
    message: str


class BasketCoverageIssueInfo(BaseModel):
    ingredient: str
    code: Literal[
        "no_acceptable_variant",
        "package_size_unknown",
        "unit_incompatible",
    ]
    message: str
    candidate_product_name: str | None = None


class RetailerBasketResult(BaseModel):
    retailer: str
    retailer_name: str
    total: float
    items: list[BasketItem]
    not_found: list[str]
    matched_count: int = 0
    requested_count: int = 0
    is_complete: bool = False
    availability: Literal["available", "partial", "unavailable"] = "unavailable"
    total_is_comparable: bool = False
    errors: list[AdapterErrorInfo] = Field(default_factory=list)
    duration_ms: int = 0
    calculation_mode: Literal["one_pack", "quantity_aware"] = "one_pack"
    coverage_issues: list[BasketCoverageIssueInfo] = Field(default_factory=list)


class BasketRequestItem(BaseModel):
    name: str
    quantity: float
    unit: RequestUnit

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("ingredient name must not be blank")
        if len(cleaned) > 120:
            raise ValueError("ingredient name must be at most 120 characters")
        return cleaned

    @field_validator("quantity", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value):
        if isinstance(value, bool):
            raise ValueError("quantity must be a number")
        return value

    @model_validator(mode="after")
    def validate_quantity(self):
        normalize_requested_quantity(self.quantity, self.unit)
        return self


class BasketCompareRequest(BaseModel):
    ingredients: list[str] | None = Field(default=None, max_length=50)
    items: list[BasketRequestItem] | None = Field(default=None, max_length=50)

    @field_validator("ingredients")
    @classmethod
    def clean_ingredients(cls, values: list[str] | None) -> list[str] | None:
        return PriceSyncRequest.clean_ingredients(values) if values is not None else None

    @model_validator(mode="after")
    def select_and_normalize_input(self):
        has_ingredients = "ingredients" in self.model_fields_set
        has_items = "items" in self.model_fields_set
        if has_ingredients == has_items:
            raise ValueError("provide exactly one of 'ingredients' or 'items'")
        if has_ingredients and self.ingredients is None:
            raise ValueError("'ingredients' must be an array")
        if has_items and self.items is None:
            raise ValueError("'items' must be an array")

        if self.items is not None:
            aggregates: dict[str, tuple[BasketRequestItem, Decimal]] = {}
            dimensions: dict[str, str] = {}
            for item in self.items:
                normalized = normalize_requested_quantity(item.quantity, item.unit)
                key = item.name.casefold()
                previous_dimension = dimensions.get(key)
                if previous_dimension and previous_dimension != normalized.dimension:
                    raise ValueError(
                        f"duplicate ingredient '{item.name}' uses incompatible dimensions"
                    )
                dimensions[key] = normalized.dimension
                if key not in aggregates:
                    aggregates[key] = (item, normalized.base_amount)
                else:
                    first, total = aggregates[key]
                    aggregates[key] = (first, total + normalized.base_amount)

            combined: list[BasketRequestItem] = []
            for first, total_base in aggregates.values():
                combined_amount = amount_in_unit(total_base, first.unit)
                normalize_requested_quantity(combined_amount, first.unit)
                combined.append(
                    first.model_copy(update={"quantity": float(combined_amount)})
                )
            self.items = combined
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"ingredients": ["milk", "pasta"]},
                {
                    "items": [
                        {"name": "milk", "quantity": 1, "unit": "l"},
                        {"name": "pasta", "quantity": 500, "unit": "g"},
                    ]
                },
            ],
        }
    }


class BasketCompareResponse(BaseModel):
    retailers: list[RetailerBasketResult]

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "retailers": [
                        {
                            "retailer": "sainsburys",
                            "retailer_name": "Sainsbury's",
                            "total": 1.2,
                            "items": [
                                {
                                    "ingredient": "milk",
                                    "product_name": "British Whole Milk 1.13L",
                                    "price": 1.2,
                                    "url": "https://retailer.example/milk",
                                    "requested_quantity": 1,
                                    "requested_unit": "l",
                                    "package_quantity": 1130,
                                    "package_unit": "ml",
                                    "packs_needed": 1,
                                    "supplied_quantity": 1.13,
                                    "excess_quantity": 0.13,
                                    "line_total": 1.2,
                                }
                            ],
                            "not_found": [],
                            "matched_count": 1,
                            "requested_count": 1,
                            "is_complete": True,
                            "availability": "available",
                            "total_is_comparable": True,
                            "errors": [],
                            "calculation_mode": "quantity_aware",
                            "coverage_issues": [],
                        }
                    ]
                }
            ]
        }
    }
