from collections import defaultdict, deque
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from decimal import Decimal

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import engine, get_db
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
    AdapterErrorInfo,
    BasketCoverageIssueInfo,
)
from app.adapters.registry import RETAILER_NAMES, retailer_metadata
from app.services.products import search_products, track_product, get_price_history, refresh_prices
from app.services.price_sync import BasketQuantityRequest, find_best_prices, compare_basket
from app.supabase_sync import build_price_row, upsert_ingredient_prices
from app.shopping_schemas import ShoppingPlanRequest, ShoppingPlanResponse
from app.services.shopping_plan import build_shopping_plan

logger = logging.getLogger("kitchen_companion.pricing")
app = FastAPI(
    title="Kitchen Companion Pricing API",
    description="Basket pricing for Kitchen Companion's buy-missing-items workflow.",
    version="1.3.0",
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    )

_rate_limit = max(0, int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "60")))
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = threading.Lock()
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@app.middleware("http")
async def request_context(request: Request, call_next):
    incoming_id = request.headers.get("X-Request-ID", "")
    request_id = incoming_id if _REQUEST_ID_RE.fullmatch(incoming_id) else str(uuid.uuid4())
    started = time.monotonic()

    if _rate_limit and request.url.path in {
        "/basket/compare",
        "/price-sync",
        "/shopping/plan",
    }:
        client = request.client.host if request.client else "unknown"
        key = f"{client}:{request.url.path}"
        now = time.monotonic()
        with _rate_lock:
            bucket = _rate_buckets[key]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= _rate_limit:
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "Rate limit exceeded. Try again shortly."},
                )
                response.headers["X-Request-ID"] = request_id
                return response
            bucket.append(now)

    response = await call_next(request)
    duration_ms = round((time.monotonic() - started) * 1000)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        json.dumps(
            {
                "event": "http_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
            separators=(",", ":"),
        )
    )
    return response


def require_service_auth(authorization: str | None = Header(default=None)) -> None:
    """Optional server-to-server auth; never place this secret in browser code."""
    expected = os.getenv("BASKET_API_KEY", "")
    if not expected:
        return
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.casefold() != "bearer" or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    """Report process readiness without requiring a database for stateless routes."""
    database_required = os.getenv("DATABASE_REQUIRED", "false").casefold() == "true"
    if not database_required:
        return {
            "status": "ready",
            "dependencies": {"database": "optional_not_checked"},
        }
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "dependencies": {"database": "unavailable"}},
        )
    return {"status": "ready", "dependencies": {"database": "available"}}


@app.get("/retailers", response_model=list[RetailerInfo])
def retailers():
    return retailer_metadata()


@app.get("/search", response_model=list[ProductResult])
def search(
    q: str = Query(..., min_length=1, description="Search query"),
    retailer: str | None = Query(None, description="Filter to a specific retailer key"),
):
    if retailer and retailer not in RETAILER_NAMES:
        raise HTTPException(status_code=400, detail=f"Unknown retailer '{retailer}'")
    results = search_products(q, retailer)
    return [
        ProductResult(
            external_id=hit.product.external_id,
            name=hit.product.name,
            retailer=hit.retailer,
            url=hit.product.url,
            image_url=hit.product.image_url,
            price=hit.product.price,
            unit_price=hit.product.unit_price,
            unit=hit.product.unit,
            in_stock=hit.product.in_stock,
        )
        for hit in results
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


@app.post(
    "/price-sync",
    response_model=PriceSyncResponse,
    dependencies=[Depends(require_service_auth)],
)
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
        errors=[AdapterErrorInfo(**error.__dict__) for error in result.errors],
        written_to_supabase=written,
    )


@app.post(
    "/basket/compare",
    response_model=BasketCompareResponse,
    dependencies=[Depends(require_service_auth)],
)
def basket_compare(payload: BasketCompareRequest):
    """
    Compare the cost of a basket of ingredients across all supported retailers.
    Returns retailers sorted cheapest-first.
    """
    if payload.ingredients is not None and not payload.ingredients:
        raise HTTPException(status_code=400, detail="ingredients list must not be empty")
    if payload.items is not None and not payload.items:
        raise HTTPException(status_code=400, detail="items list must not be empty")

    if payload.items is not None:
        requested_items = [
            BasketQuantityRequest(
                name=item.name,
                quantity=Decimal(str(item.quantity)),
                unit=item.unit,
            )
            for item in payload.items
        ]
        baskets = compare_basket(items=requested_items)
    else:
        baskets = compare_basket(payload.ingredients or [])

    return BasketCompareResponse(
        retailers=[
            RetailerBasketResult(
                retailer=b.retailer,
                retailer_name=b.retailer_name,
                total=b.total,
                items=[BasketItem(**item) for item in b.items],
                not_found=b.not_found,
                matched_count=b.matched_count,
                requested_count=b.requested_count,
                is_complete=b.is_complete,
                availability=b.availability,
                total_is_comparable=b.total_is_comparable,
                errors=[AdapterErrorInfo(**error.__dict__) for error in b.errors],
                duration_ms=b.duration_ms,
                calculation_mode=b.calculation_mode,
                coverage_issues=[
                    BasketCoverageIssueInfo(**issue.__dict__)
                    for issue in b.coverage_issues
                ],
            )
            for b in baskets
        ]
    )


@app.post(
    "/shopping/plan",
    response_model=ShoppingPlanResponse,
    dependencies=[Depends(require_service_auth)],
)
def shopping_plan(payload: ShoppingPlanRequest):
    """Net scheduled demand against pantry stock and price complete plans."""
    supplied_retailers = set(payload.allowed_retailers or []) | set(
        payload.retailer_costs
    )
    unknown = sorted(supplied_retailers - set(RETAILER_NAMES))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown retailer keys: {', '.join(unknown)}",
        )
    return build_shopping_plan(payload)
