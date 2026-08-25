# Kitchen Companion Pricing API

This FastAPI service supports Kitchen Companion's **buy missing items** step. It compares a requested ingredient basket across UK supermarkets; it is not a separate shopping product and does not place orders.

Retail prices come from fragile public retailer surfaces. Results are estimates, may vary by location, and must be verified on the linked retailer page before purchase. The API never treats a missing or unavailable basket as the cheapest complete basket.

## Requirements

- Python 3.12 (the declared and CI-tested version)
- PostgreSQL only for tracking/history endpoints; stateless endpoints such as `/health`, `/ready`, `/retailers`, `/price-sync`, and `/basket/compare` start without a database connection

## Local setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Then open `http://127.0.0.1:8000/docs` for OpenAPI or run:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
curl -X POST http://127.0.0.1:8000/basket/compare \
  -H 'Content-Type: application/json' \
  -d '{"ingredients":["milk","pasta"]}'
```

Run verification with:

```bash
python -m compileall -q app tests
python -m pytest -q
```

## Configuration

No environment variable is required for stateless local comparison.

| Variable | Default | Purpose |
|---|---|---|
| `BASKET_API_KEY` | unset | When set, `/basket/compare` and `/price-sync` require `Authorization: Bearer …`. Store this only in the Kitchen Companion server, never browser code. |
| `CORS_ALLOWED_ORIGINS` | none | Comma-separated exact browser origins. Wildcard CORS is not enabled. |
| `RATE_LIMIT_REQUESTS_PER_MINUTE` | `60` | Per-process, per-client limit for comparison and sync routes; `0` disables it. Use an edge/shared limiter when scaling to multiple instances. |
| `MAX_ADAPTER_WORKERS` | `4` | Bounded retailer request concurrency, clamped to 1–8. |
| `ADAPTER_CONNECT_TIMEOUT_SECONDS` | `3.05` | Retailer connection timeout. |
| `ADAPTER_READ_TIMEOUT_SECONDS` | `8` | Retailer response timeout. |
| `ADAPTER_MAX_RETRIES` | `1` | Bounded GET retry count, clamped to 0–2. |
| `TROLLEY_CACHE_TTL_SECONDS` | `300` | In-memory cache lifetime. |
| `TROLLEY_CACHE_MAX_ENTRIES` | `128` | Maximum cached Trolley queries. |
| `ENABLE_RESTRICTED_SOURCES` | `false` | Enables Tesco/Trolley only after written permission; see [source policy](docs/SOURCE_POLICY.md). |
| `DATABASE_URL` | local PostgreSQL URL | Database for tracking/history endpoints. It is not contacted at import or for basket comparison. |
| `DATABASE_REQUIRED` | `false` | When `true`, `/ready` checks the database and returns 503 if unavailable. |
| `ENABLE_LEGACY_SUPABASE_SYNC` | `false` | Explicit gate for the isolated legacy sync path. |
| `SUPABASE_URL` | unset | Legacy target; unnecessary for basket comparison. |
| `SUPABASE_SERVICE_KEY` | unset | Legacy server-only credential. Never expose it to a browser or commit it. |

## Basket contract

`POST /basket/compare` keeps the existing request and item fields. Ingredient strings are whitespace-normalised and case-insensitively deduplicated; at most 50 ingredients and 120 characters per ingredient are accepted.

Request:

```json
{"ingredients": ["milk", "pasta"]}
```

Response shape:

```json
{
  "retailers": [
    {
      "retailer": "sainsburys",
      "retailer_name": "Sainsbury's",
      "total": 1.59,
      "items": [
        {
          "ingredient": "milk",
          "product_name": "Example Whole Milk 1L",
          "price": 0.69,
          "unit_price": 0.69,
          "unit": "ltr",
          "url": "https://retailer.example/product",
          "image_url": null,
          "retrieved_at": "2026-08-25T12:00:00Z"
        }
      ],
      "not_found": [],
      "matched_count": 2,
      "requested_count": 2,
      "is_complete": true,
      "availability": "available",
      "total_is_comparable": true,
      "errors": [],
      "duration_ms": 314
    }
  ]
}
```

Ranking is `available` complete baskets first, then partial baskets by matched count, then unavailable retailers. Price only breaks ties within the same completeness class. `total` remains the subtotal of matched items for backward compatibility; clients must display it as a basket price only when `total_is_comparable` is `true`.

`not_found` means the retailer responded but no strongly related product matched. `errors` means the retailer could not be checked, for example:

```json
{
  "ingredient": "milk",
  "retailer": "tesco",
  "code": "disabled_by_policy",
  "message": "Source disabled pending permission for automated access."
}
```

Matching uses complete, normalised query tokens and rejects substring-only or partial semantic matches. It does not silently substitute unrelated products. Quantities in free-text ingredients are removed from matching, but basket quantities and package-size optimisation are not yet supported.

## Endpoints

| Method | Path | Database needed | Description |
|---|---|---:|---|
| GET | `/health` | No | Process liveness. |
| GET | `/ready` | Configurable | Dependency readiness. |
| GET | `/retailers` | No | Retailer keys and names. |
| GET | `/search` | Session only | Search one or all adapters. |
| POST | `/basket/compare` | No | Compare complete and partial baskets safely. |
| POST | `/price-sync` | No | Match ingredients; legacy write is opt-in. |
| POST | `/track` | Yes | Track a selected product. |
| GET | `/history/{product_id}` | Yes | Read captured prices. |
| POST | `/refresh` | Yes | Refresh actively tracked products. |

Every response includes `X-Request-ID`. HTTP and adapter timings are logged without credentials. Upstream exception details are intentionally not returned to clients.

## Source limitations and responsible use

Tesco and Trolley explicitly prohibit automated access without permission, so those providers are disabled by default. This makes Tesco, Asda, and Iceland visibly unavailable until an authorised source is configured. Other integrations use undocumented or changing retailer surfaces and may be blocked by CDNs or server IP policy. See [docs/SOURCE_POLICY.md](docs/SOURCE_POLICY.md) before any production use.

Recorded, sanitised response fragments cover parser formats in tests. Live checks are intentionally minimal and must not target policy-disabled sources.

## Docker and low-cost hosting

```bash
docker build -t kitchen-companion-pricing .
docker run --rm -p 8000:8000 kitchen-companion-pricing
```

The container runs as a non-root user and scales to zero cleanly. [Google Cloud Run pricing](https://cloud.google.com/run/pricing) documents monthly free allowances, but the service is pay-as-you-go beyond them and requires a billing account. Set a budget alert and maximum instances before deployment. **This repository is not deployed by this change, and no cost-bearing resource was created.**

For production, put the API behind the Kitchen Companion server. Configure that server with `PRICING_API_URL` and, if enabled here, a server-only `PRICING_API_KEY` whose value matches this service's `BASKET_API_KEY`. Do not call the authenticated endpoint directly from public browser JavaScript.
