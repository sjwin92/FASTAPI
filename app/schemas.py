from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


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


class AdapterErrorInfo(BaseModel):
    ingredient: str
    retailer: str
    code: str
    message: str


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


class BasketCompareRequest(BaseModel):
    ingredients: list[str] = Field(max_length=50)

    @field_validator("ingredients")
    @classmethod
    def clean_ingredients(cls, values: list[str]) -> list[str]:
        return PriceSyncRequest.clean_ingredients(values)

    model_config = {
        "json_schema_extra": {
            "examples": [{"ingredients": ["milk", "pasta"]}],
        }
    }


class BasketCompareResponse(BaseModel):
    retailers: list[RetailerBasketResult]
