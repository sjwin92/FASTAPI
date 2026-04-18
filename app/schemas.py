from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class HealthResponse(BaseModel):
    status: str = "ok"


class RetailerResponse(BaseModel):
    key: str
    name: str
    scraping_implemented: bool


class PriceHistoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    price: float
    captured_at: datetime


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    retailer: str
    external_id: str
    name: str
    url: str | None
    brand: str | None
    image_url: str | None
    currency: str
    created_at: datetime
    updated_at: datetime
    latest_price: float | None = None
    latest_captured_at: datetime | None = None


class ProductHistoryOut(BaseModel):
    product: ProductOut
    history: list[PriceHistoryItem]


class TrackRequest(BaseModel):
    retailer: str = Field(min_length=1, max_length=64)
    external_id: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, min_length=1, max_length=500)
    price: float | None = Field(default=None, gt=0)
    currency: str = Field(min_length=3, max_length=3, default="GBP")
    url: HttpUrl | None = None
    brand: str | None = None
    image_url: HttpUrl | None = None


class TrackResponse(BaseModel):
    message: str
    product: ProductOut
