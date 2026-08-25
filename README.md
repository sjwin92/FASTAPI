# Kitchen Companion Pricing API

This FastAPI service supports Kitchen Companion's **buy missing items** step. It compares a requested ingredient basket across UK supermarkets; it is not a separate shopping product and does not place orders.

Retail prices come from fragile public retailer surfaces. Results are estimates, may vary by location, and must be verified on the linked retailer page before purchase. The API never treats a missing or unavailable basket as the cheapest complete basket.

## Requirements

- Python 3.12 (the declared and CI-tested version)
- PostgreSQL only for tracking/history endpoints; stateless endpoints such as `/health`, `/ready`, `/retailers`, `/price-sync`, `/basket/compare`, and `/shopping/plan` start without a database connection

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
curl -X POST http://127.0.0.1:8000/basket/compare \
  -H 'Content-Type: application/json' \
  -d '{"items":[{"name":"milk","quantity":1,"unit":"l"}]}'
curl -X POST http://127.0.0.1:8000/shopping/plan \
  -H 'Content-Type: application/json' \
  -d '{"shopping_on":"2026-09-01","requirements":[{"requirement_key":"meal-1:milk","ingredient_key":"milk:fresh","name":"fresh milk","quantity":500,"unit":"ml","needed_on":"2026-09-02"}],"pantry_lots":[]}'
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
| `SHOPPING_PLAN_MAX_OPTIMIZER_STATES` | `200000` | Exact shopping-plan frontier limit, clamped to 10,000–1,000,000. A limit hit fails closed instead of returning an approximate optimum. |
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

`POST /basket/compare` accepts exactly one of two request forms. The legacy form keeps its one-package behaviour:

Request:

```json
{"ingredients": ["milk", "pasta"]}
```

The structured form proves package coverage and calculates the lowest checkout cost:

```json
{
  "items": [
    {"name": "milk", "quantity": 1, "unit": "l"},
    {"name": "pasta", "quantity": 500, "unit": "g"}
  ]
}
```

Supported request units are `g`, `kg`, `ml`, `cl`, `l`, and `each`. Quantities must be positive and finite, and `each` must be a whole number. Cooking measures and mass/volume/count conflicts for duplicate names return HTTP 422. Duplicate structured items are combined in canonical mass, volume, or count units. Both forms retain the 50-item and 120-character name limits.

Response shape:

```json
{
  "retailers": [
    {
      "retailer": "sainsburys",
      "retailer_name": "Sainsbury's",
      "total": 0.69,
      "items": [
        {
          "ingredient": "milk",
          "product_name": "Example Whole Milk 1L",
          "price": 0.69,
          "unit_price": 0.69,
          "unit": "ltr",
          "url": "https://retailer.example/product",
          "image_url": null,
          "retrieved_at": "2026-08-25T12:00:00Z",
          "requested_quantity": 1.0,
          "requested_unit": "l",
          "package_quantity": 1000.0,
          "package_unit": "ml",
          "packs_needed": 1,
          "supplied_quantity": 1.0,
          "excess_quantity": 0.0,
          "line_total": 0.69
        }
      ],
      "not_found": [],
      "matched_count": 1,
      "requested_count": 1,
      "is_complete": true,
      "availability": "available",
      "total_is_comparable": true,
      "errors": [],
      "duration_ms": 314,
      "calculation_mode": "quantity_aware",
      "coverage_issues": []
    }
  ]
}
```

For structured requests, `price` remains one pack's price, `line_total` is `price × packs_needed`, and `total` is the sum of line totals. Package quantities use canonical `g`, `ml`, or `each`; supplied and excess quantities use the request unit. Legacy totals remain the sum of one pack per matched ingredient, and responses identify that path with `calculation_mode: "one_pack"`.

Ranking is `available` complete baskets first, then partial baskets by matched count, then unavailable retailers. Price only breaks ties within the same completeness class. Clients must display `total` as a comparable basket price only when `total_is_comparable` is `true`.

`not_found` means the retailer responded but no strongly related product matched. `errors` means the retailer could not be checked, for example:

```json
{
  "ingredient": "milk",
  "retailer": "tesco",
  "code": "disabled_by_policy",
  "message": "Source disabled pending permission for automated access."
}
```

`coverage_issues` separates a related but unprovable product from `not_found`. Its codes are `no_acceptable_variant`, `package_size_unknown`, and `unit_incompatible`. Unknown package coverage fails closed, so it cannot make a basket complete or comparable.

Matching requires every meaningful query token and rejects unrequested material forms such as UHT/long-life, powdered, dried, frozen, canned/tinned, condensed, evaporated, flavoured, ready-cooked, and breaded. Structured selection requires an in-stock product with a compatible known package size. It chooses the lowest line checkout cost, then least excess, strongest match, and stable product ID. Conditional promotions, loyalty prices, and multibuy text are not applied.

## Pantry-to-purchase planning

`POST /shopping/plan` is a stateless bridge between Kitchen Companion's meal plan, pantry, shopping list, and expiry tracking. Kitchen Companion remains the source of truth and sends stable requirement, ingredient, and pantry-lot keys.

```json
{
  "shopping_on": "2026-09-01",
  "requirements": [
    {
      "requirement_key": "meal-42:milk",
      "ingredient_key": "ingredient:milk:fresh",
      "name": "fresh milk",
      "quantity": 500,
      "unit": "ml",
      "needed_on": "2026-09-02",
      "source_refs": ["meal-42"]
    }
  ],
  "pantry_lots": [
    {
      "lot_key": "pantry-lot-901",
      "ingredient_key": "ingredient:milk:fresh",
      "available_quantity": 250,
      "unit": "ml",
      "expires_on": "2026-09-02"
    }
  ],
  "allowed_retailers": ["ocado", "morrisons"],
  "retailer_costs": {
    "ocado": {
      "method": "delivery",
      "fixed_cost_gbp": 3.99,
      "minimum_spend_gbp": 40.00
    },
    "morrisons": {
      "method": "in_store",
      "fixed_cost_gbp": 2.50,
      "minimum_spend_gbp": 0
    }
  }
}
```

The planner allocates usable pantry stock by required date and earliest expiry, with unknown-expiry lots last. It searches only for the remaining quantities. Relevant stock expiring too early, unknown-expiry allocations, and unused expiring stock are returned as `pantry_insights`. If pantry stock covers the plan, the response is `pantry_only` and no retailer is contacted.

Complete options use one retailer or at most two. One ingredient key is always purchased wholly from one retailer. The exact optimizer enforces supplied minimum spends, adds fixed trip/delivery cost once per used store, and retains non-dominated checkout-cost and package-surplus choices. It never adds basket filler. Policy-disabled sources are reported as excluded and receive no network requests.

`decision_status` controls presentation:

- `ready` and `pantry_only` are safe to present as decision choices.
- `needs_store_costs` means split plans are merchandise-only estimates; missing costs are never assumed to be zero.
- `source_uncertain`, `optimization_limited`, and `no_complete_plan` must be shown as observed or incomplete results, not a proven cheapest plan.

When the decision is ready, `pareto_labels` identify the fewest-store, lowest-landed-cost, and least-unallocated-value options. The unallocated value is a proportional estimate of purchased value not assigned to the current meal plan, not predicted food waste.

Every option includes a provisional purchase manifest. Kitchen Companion must confirm the actual checkout product, packs, and price before adding the full `purchased_quantity` to inventory. The pricing service never writes pantry or purchase state.

## Endpoints

| Method | Path | Database needed | Description |
|---|---|---:|---|
| GET | `/health` | No | Process liveness. |
| GET | `/ready` | Configurable | Dependency readiness. |
| GET | `/retailers` | No | Retailer keys, configured enablement, reason, and capabilities. |
| GET | `/search` | No | Search one or all adapters with the source retailer on every result. |
| POST | `/basket/compare` | No | Compare complete and partial baskets safely. |
| POST | `/shopping/plan` | No | Net scheduled demand against pantry lots and return exact one/two-store plans. |
| POST | `/price-sync` | No | Match ingredients; legacy write is opt-in. |
| POST | `/track` | Yes | Track a selected product. |
| GET | `/history/{product_id}` | Yes | Read captured prices. |
| POST | `/refresh` | Yes | Refresh actively tracked products. |

Every response includes `X-Request-ID`. HTTP, adapter, and planner timings are logged without credentials or household ingredient, lot, meal, or requirement text. Upstream exception details are intentionally not returned to clients.

## Source limitations and responsible use

Tesco and Trolley explicitly prohibit automated access without permission, so those providers are disabled by default. This makes Tesco, Asda, and Iceland visibly unavailable until an authorised source is configured. Other integrations use undocumented or changing retailer surfaces and may be blocked by CDNs or server IP policy. See [docs/SOURCE_POLICY.md](docs/SOURCE_POLICY.md) before any production use.

Recorded, sanitised response fragments cover parser formats in tests. Live checks are intentionally minimal and must not target policy-disabled sources.

The latest pantry-aware smoke record is [docs/SMOKE_TEST_2026-08-26.md](docs/SMOKE_TEST_2026-08-26.md).

The copyable consumer integration notes are in [docs/FRONTEND_HANDOFF.md](docs/FRONTEND_HANDOFF.md).

## Docker and low-cost hosting

```bash
docker build -t kitchen-companion-pricing .
docker run --rm -p 8000:8000 kitchen-companion-pricing
```

The container runs as a non-root user and scales to zero cleanly. [Google Cloud Run pricing](https://cloud.google.com/run/pricing) documents monthly free allowances, but the service is pay-as-you-go beyond them and requires a billing account. Set a budget alert and maximum instances before deployment. **This repository is not deployed by this change, and no cost-bearing resource was created.**

For production, put the API behind the Kitchen Companion server. Configure that server with `PRICING_API_URL` and, if enabled here, a server-only `PRICING_API_KEY` whose value matches this service's `BASKET_API_KEY`. Do not call the authenticated endpoint directly from public browser JavaScript.
