from __future__ import annotations

from typing import Any

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session, selectinload

from app.models import PriceHistory, Product
from app.schemas import TrackRequest


def list_products(db: Session, query: str | None = None, retailer: str | None = None) -> list[Product]:
    stmt: Select[tuple[Product]] = select(Product).options(selectinload(Product.prices))

    if query:
        stmt = stmt.where(Product.name.ilike(f"%{query}%"))
    if retailer:
        stmt = stmt.where(Product.retailer == retailer)

    stmt = stmt.order_by(Product.updated_at.desc(), Product.id.desc()).limit(100)
    return db.execute(stmt).scalars().unique().all()


def _from_parsed(parsed: Any) -> dict[str, Any]:
    product_url = getattr(parsed, "product_url", None) or getattr(parsed, "url", None)
    price = getattr(parsed, "price_gbp", None)
    if price is None:
        price = getattr(parsed, "price", None)

    return {
        "external_id": getattr(parsed, "external_id"),
        "name": getattr(parsed, "name"),
        "url": product_url,
        "brand": getattr(parsed, "brand", None),
        "image_url": getattr(parsed, "image_url", None),
        "price": float(price),
        "currency": getattr(parsed, "currency", "GBP"),
    }


def upsert_from_parsed(db: Session, retailer: str, parsed: Any) -> Product:
    normalized = _from_parsed(parsed)

    stmt = select(Product).where(
        Product.retailer == retailer,
        Product.external_id == normalized["external_id"],
    )
    product = db.execute(stmt).scalar_one_or_none()

    if product is None:
        product = Product(
            retailer=retailer,
            external_id=normalized["external_id"],
            name=normalized["name"],
            url=normalized["url"],
            brand=normalized["brand"],
            image_url=normalized["image_url"],
            currency=normalized["currency"],
        )
        db.add(product)
        db.flush()
    else:
        product.name = normalized["name"]
        product.url = normalized["url"]
        product.brand = normalized["brand"]
        product.image_url = normalized["image_url"]
        product.currency = normalized["currency"]

    db.add(PriceHistory(product_id=product.id, price=normalized["price"]))
    db.commit()
    db.refresh(product)
    return product


def track_product(db: Session, payload: TrackRequest) -> Product:
    stmt = select(Product).where(
        Product.retailer == payload.retailer,
        Product.external_id == payload.external_id,
    )
    product = db.execute(stmt).scalar_one_or_none()

    if product is None:
        product = Product(
            retailer=payload.retailer,
            external_id=payload.external_id,
            name=payload.name,
            url=str(payload.url) if payload.url else None,
            brand=payload.brand,
            image_url=str(payload.image_url) if payload.image_url else None,
            currency=payload.currency.upper(),
        )
        db.add(product)
        db.flush()
    else:
        product.name = payload.name
        product.url = str(payload.url) if payload.url else product.url
        product.brand = payload.brand
        product.image_url = str(payload.image_url) if payload.image_url else product.image_url
        product.currency = payload.currency.upper()

    price = PriceHistory(product_id=product.id, price=payload.price)
    db.add(price)
    db.commit()
    db.refresh(product)
    return product


def get_history(db: Session, product_id: int) -> tuple[Product, list[PriceHistory]] | None:
    product = db.get(Product, product_id)
    if product is None:
        return None

    history_stmt = (
        select(PriceHistory)
        .where(PriceHistory.product_id == product_id)
        .order_by(desc(PriceHistory.captured_at), desc(PriceHistory.id))
    )
    history = db.execute(history_stmt).scalars().all()
    return product, history
