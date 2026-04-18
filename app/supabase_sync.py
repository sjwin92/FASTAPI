"""
Thin client for writing price data back to the Kitchen Companion Supabase instance.

Requires two env vars:
  SUPABASE_URL            – e.g. https://xyzxyz.supabase.co
  SUPABASE_SERVICE_KEY    – service-role key (NOT the anon key; needs INSERT/UPDATE on ingredient_prices)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import requests

_SUPABASE_URL = os.getenv("SUPABASE_URL", "")
_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "apikey": _SERVICE_KEY,
        "Authorization": f"Bearer {_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }


def upsert_ingredient_prices(rows: list[dict[str, Any]]) -> None:
    """Upsert rows into ingredient_prices, conflicting on ingredient_name."""
    if not _SUPABASE_URL or not _SERVICE_KEY:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set to sync prices"
        )
    url = f"{_SUPABASE_URL}/rest/v1/ingredient_prices"
    resp = requests.post(url, json=rows, headers=_headers(), timeout=15)
    resp.raise_for_status()


def build_price_row(
    ingredient_name: str,
    price_gbp: float,
    retailer: str,
    retailer_product_id: str,
    retailer_product_url: str,
    unit: str | None,
) -> dict[str, Any]:
    return {
        "ingredient_name": ingredient_name.lower().strip(),
        "estimated_price_gbp": round(price_gbp, 2),
        "retailer": retailer,
        "retailer_product_id": retailer_product_id,
        "retailer_product_url": retailer_product_url,
        "unit": unit,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
