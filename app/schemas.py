from datetime import datetime
from pydantic import BaseModel, HttpUrl


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
    ingredients: list[str]
    retailer: str | None = None
    write_to_supabase: bool = False


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
    written_to_supabase: bool = False


class BasketItem(BaseModel):
    ingredient: str
    product_name: str
    price: float
    unit_price: float | None = None
    unit: str | None = None
    url: str
    image_url: str | None = None


class RetailerBasketResult(BaseModel):
    retailer: str
    retailer_name: str
    total: float
    items: list[BasketItem]
    not_found: list[str]


class BasketCompareRequest(BaseModel):
    ingredients: list[str]


class BasketCompareResponse(BaseModel):
    retailers: list[RetailerBasketResult]
