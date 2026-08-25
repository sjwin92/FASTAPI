# Pantry-to-purchase live smoke test — 26 August 2026

Environment: Python 3.12.14, macOS arm64. The check used no credentials, account flows, baskets, checkout surfaces, database, or Supabase connection.

## Policy gate

`ENABLE_RESTRICTED_SOURCES` was asserted false before the run. The request explicitly allowed only Ocado, Morrisons, Sainsbury's, and Waitrose. Tesco and the Trolley-backed Asda and Iceland providers received no network requests.

## Pantry-adjusted demand

The plan required 1 litre of fresh milk and 500 g of pasta on 27 August. A pantry lot supplied 250 ml of milk with an inclusive 27 August expiry.

| Ingredient | Required | Pantry allocated | Priced shortage |
|---|---:|---:|---:|
| Fresh milk | 1,000 ml | 250 ml | 750 ml |
| Pasta | 500 g | 0 g | 500 g |

The pantry allocation was conserved, and only the two remaining shortages were sent to retailer adapters.

## Retailer outcomes

| Retailer | Source status | Coverage outcome |
|---|---|---|
| Ocado | Available | Covered both shortages |
| Morrisons | Available | Covered milk; pasta returned genuine `not_found` |
| Sainsbury's | Available | Covered both shortages |
| Waitrose | Unavailable | `source_unavailable` after the bounded timeout; the retailer circuit breaker prevented the second external search |

Observed complete single-store merchandise totals were £1.61 at Sainsbury's and £1.95 at Ocado. The Sainsbury's manifest selected British Whole Milk 1.13L (2 pint) and Stamford Street Co. Penne Pasta 500g. The Ocado manifest selected M&S Select Farms British Semi Skimmed Milk 2 Pints and Ocado Whole Wheat Fusilli Pasta.

Zero fixed costs and zero minimum spends were supplied explicitly for smoke-test mechanics; they are not claims about real journey or delivery costs. The response correctly returned `source_uncertain`, suppressed decision-eligible Pareto labels because Waitrose was unavailable, and kept the complete observed options as non-recommended evidence.

The run completed in 8.19 seconds. A diagnostic regression found during the check was fixed so a retailer pair lacking usable lines is no longer misreported as a minimum-spend failure.

Prices are point-in-time observations. Consumers must confirm the actual product, pack count, price, and availability before adding the provisional purchase manifest to Kitchen Companion inventory.
