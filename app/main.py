from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.schemas import (
    ProductResult,
    TrackRequest,
    TrackResponse,
    PriceHistory,
    PricePoint,
    RetailerInfo,
    PriceSyncRequest,
    PriceSyncResponse,
    PriceSyncItem,
    BasketCompareRequest,
    BasketCompareResponse,
    RetailerBasketResult,
    BasketItem,
)
from app.adapters.registry import RETAILER_NAMES
from app.services.products import search_products, track_product, get_price_history, refresh_prices
from app.services.price_sync import find_best_prices, compare_basket
from app.supabase_sync import build_price_row, upsert_ingredient_prices

app = FastAPI(title="Supermarket Price Tracker API")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/retailers", response_model=list[RetailerInfo])
def retailers():
    return [{"key": k, "name": v} for k, v in RETAILER_NAMES.items()]


@app.get("/search", response_model=list[ProductResult])
def search(
    q: str = Query(..., min_length=1, description="Search query"),
    retailer: str | None = Query(None, description="Filter to a specific retailer key"),
    db: Session = Depends(get_db),
):
    if retailer and retailer not in RETAILER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown retailer '{retailer}'")
    results = search_products(q, retailer, db)
    return [
        ProductResult(
            external_id=r.external_id,
            name=r.name,
            retailer=retailer or "unknown",
            url=r.url,
            image_url=r.image_url,
            price=r.price,
            unit_price=r.unit_price,
            unit=r.unit,
            in_stock=r.in_stock,
        )
        for r in results
    ]


@app.post("/track", response_model=TrackResponse)
def track(payload: TrackRequest, db: Session = Depends(get_db)):
    if payload.retailer not in RETAILER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown retailer '{payload.retailer}'")
    tracked = track_product(payload, db)
    return TrackResponse(
        tracked_product_id=tracked.id,
        product_id=tracked.product_id,
        message="Product is now being tracked.",
    )


@app.get("/history/{product_id}", response_model=PriceHistory)
def history(product_id: int, db: Session = Depends(get_db)):
    product = get_price_history(product_id, db)
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")
    return PriceHistory(
        product_id=product.id,
        name=product.name,
        retailer=product.retailer,
        url=product.url,
        history=[
            PricePoint(
                price=p.price,
                unit_price=p.unit_price,
                unit=p.unit,
                in_stock=p.in_stock,
                captured_at=p.captured_at,
            )
            for p in product.prices
        ],
    )


@app.post("/refresh", response_model=dict)
def refresh(db: Session = Depends(get_db)):
    count = refresh_prices(db)
    return {"refreshed": count}


@app.post("/price-sync", response_model=PriceSyncResponse)
def price_sync(payload: PriceSyncRequest):
    """
    Find the best retailer product for each ingredient and optionally
    write the results back to the Supabase ingredient_prices table so
    that Kitchen Companion can show live price estimates.
    """
    if payload.retailer and payload.retailer not in RETAILER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown retailer '{payload.retailer}'")

    result = find_best_prices(payload.ingredients, retailer=payload.retailer)

    synced_items = [
        PriceSyncItem(
            ingredient=m.ingredient,
            product_name=m.product.name,
            retailer=m.retailer,
            price=m.product.price,
            unit_price=m.product.unit_price,
            unit=m.product.unit,
            url=m.product.url,
            image_url=m.product.image_url,
        )
        for m in result.synced
    ]

    written = False
    if payload.write_to_supabase and result.synced:
        rows = [
            build_price_row(
                ingredient_name=m.ingredient,
                price_gbp=m.product.price,
                retailer=m.retailer,
                retailer_product_id=m.product.external_id,
                retailer_product_url=m.product.url,
                unit=m.product.unit,
            )
            for m in result.synced
        ]
        upsert_ingredient_prices(rows)
        written = True

    return PriceSyncResponse(
        synced=synced_items,
        not_found=result.not_found,
        written_to_supabase=written,
    )


@app.post("/basket/compare", response_model=BasketCompareResponse)
def basket_compare(payload: BasketCompareRequest):
    """
    Compare the cost of a basket of ingredients across all supported retailers.
    Returns retailers sorted cheapest-first.
    """
    if not payload.ingredients:
        raise HTTPException(status_code=400, detail="ingredients list must not be empty")

    baskets = compare_basket(payload.ingredients)

    return BasketCompareResponse(
        retailers=[
            RetailerBasketResult(
                retailer=b.retailer,
                retailer_name=b.retailer_name,
                total=b.total,
                items=[BasketItem(**item) for item in b.items],
                not_found=b.not_found,
            )
            for b in baskets
        ]
    )
