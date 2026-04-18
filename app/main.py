from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy.orm import Session

from app.adapters.registry import registry
from app.db import get_db
from app.models import PriceHistory, Product
from app.schemas import (
    HealthResponse,
    PriceHistoryItem,
    ProductHistoryOut,
    ProductOut,
    RetailerResponse,
    TrackRequest,
    TrackResponse,
)
from app.services.products import get_history, list_products, track_product, upsert_from_parsed

app = FastAPI(title="Supermarket Price Tracker API", version="1.0.0")


def _to_product_out(product: Product) -> ProductOut:
    latest: PriceHistory | None = product.prices[0] if getattr(product, "prices", None) else None
    return ProductOut(
        id=product.id,
        retailer=product.retailer,
        external_id=product.external_id,
        name=product.name,
        url=product.url,
        brand=product.brand,
        image_url=product.image_url,
        currency=product.currency,
        created_at=product.created_at,
        updated_at=product.updated_at,
        latest_price=latest.price if latest else None,
        latest_captured_at=latest.captured_at if latest else None,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse()


@app.get("/retailers", response_model=list[RetailerResponse])
def retailers() -> list[RetailerResponse]:
    return [RetailerResponse(**info.__dict__) for info in registry.list_retailers()]


@app.get("/search", response_model=list[ProductOut])
def search(
    q: str | None = Query(default=None, min_length=1),
    retailer: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[ProductOut]:
    if retailer and q:
        adapter = registry.get(retailer)
        if adapter and hasattr(adapter, "search_products"):
            parsed_products = adapter.search_products(q)
            products = [upsert_from_parsed(db, retailer, parsed) for parsed in parsed_products]
            return [_to_product_out(product) for product in products]

    products = list_products(db, query=q, retailer=retailer)
    return [_to_product_out(product) for product in products]


@app.post("/track", response_model=TrackResponse)
def track(payload: TrackRequest, db: Session = Depends(get_db)) -> TrackResponse:
    adapter = registry.get(payload.retailer)
    if adapter is None:
        raise HTTPException(status_code=400, detail=f"Unsupported retailer '{payload.retailer}'")

    if payload.url and hasattr(adapter, "fetch_product"):
        parsed = adapter.fetch_product(str(payload.url))
        product = upsert_from_parsed(db, payload.retailer, parsed)
        return TrackResponse(message="tracking updated", product=_to_product_out(product))

    if not payload.external_id or not payload.name or payload.price is None:
        raise HTTPException(
            status_code=400,
            detail="external_id, name, and price are required for non-URL tracking",
        )

    product = track_product(db, payload)
    return TrackResponse(message="tracking updated", product=_to_product_out(product))


@app.get("/history/{id}", response_model=ProductHistoryOut)
def history(id: int, db: Session = Depends(get_db)) -> ProductHistoryOut:
    result = get_history(db, id)
    if result is None:
        raise HTTPException(status_code=404, detail="Product not found")

    product, entries = result
    product_out = _to_product_out(product)
    history_out = [PriceHistoryItem.model_validate(entry) for entry in entries]
    return ProductHistoryOut(product=product_out, history=history_out)
