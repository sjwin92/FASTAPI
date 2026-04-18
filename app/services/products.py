from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.models import Product, PriceRecord, TrackedProduct
from app.adapters.base import ProductResult as AdapterResult
from app.adapters.registry import get_adapter, all_adapters
from app.schemas import TrackRequest


def search_products(query: str, retailer: str | None, db: Session) -> list[AdapterResult]:
    adapters = [get_adapter(retailer)] if retailer else all_adapters()
    results: list[AdapterResult] = []
    for adapter in adapters:
        if adapter:
            results.extend(adapter.search(query))
    return results


def track_product(req: TrackRequest, db: Session) -> TrackedProduct:
    stmt = select(Product).where(
        Product.retailer == req.retailer,
        Product.external_id == req.external_id,
    )
    product = db.scalars(stmt).first()

    if not product:
        product = Product(
            name=req.name,
            retailer=req.retailer,
            external_id=req.external_id,
            url=req.url,
            image_url=req.image_url,
        )
        db.add(product)
        db.flush()

    tracked = TrackedProduct(
        product_id=product.id,
        alert_threshold=req.alert_threshold,
    )
    db.add(tracked)
    db.commit()
    db.refresh(tracked)
    return tracked


def get_price_history(product_id: int, db: Session) -> Product | None:
    return db.get(Product, product_id)


def refresh_prices(db: Session) -> int:
    """Pull latest prices for all actively tracked products and store records."""
    stmt = (
        select(TrackedProduct)
        .where(TrackedProduct.active == True)  # noqa: E712
        .join(Product)
    )
    tracked = db.scalars(stmt).all()
    refreshed = 0

    for t in tracked:
        product = t.product
        adapter = get_adapter(product.retailer)
        if not adapter:
            continue
        result = adapter.fetch_price(product.external_id)
        if not result:
            continue
        record = PriceRecord(
            product_id=product.id,
            price=result.price,
            unit_price=result.unit_price,
            unit=result.unit,
            in_stock=result.in_stock,
            captured_at=datetime.utcnow(),
        )
        db.add(record)
        refreshed += 1

    db.commit()
    return refreshed
