from __future__ import annotations

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session, selectinload

from app.adapters.ocado import OcadoParsedProduct
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


def upsert_from_parsed(db: Session, retailer: str, parsed: OcadoParsedProduct) -> Product:
    stmt = select(Product).where(
        Product.retailer == retailer,
        Product.external_id == parsed.external_id,
    )
    product = db.execute(stmt).scalar_one_or_none()

    if product is None:
        product = Product(
            retailer=retailer,
            external_id=parsed.external_id,
            name=parsed.name,
            url=parsed.product_url,
            brand=parsed.brand,
            image_url=parsed.image_url,
            currency="GBP",
        )
        db.add(product)
        db.flush()
    else:
        product.name = parsed.name
        product.url = parsed.product_url
        product.brand = parsed.brand
        product.image_url = parsed.image_url
        product.currency = "GBP"

    db.add(PriceHistory(product_id=product.id, price=parsed.price_gbp))
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
