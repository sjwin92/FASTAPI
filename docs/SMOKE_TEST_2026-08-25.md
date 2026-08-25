# Live smoke test — 25 August 2026

Environment: Python 3.12.14, macOS arm64. Queries were minimal and no credentials, account flows, baskets, or checkout surfaces were used.

## Policy gate

Tesco and Trolley were not queried after their published terms were reviewed. The default registry returned `disabled_by_policy` for Tesco and for the Trolley-backed Asda and Iceland adapters.

## Milk

| Retailer | Outcome | Strong match |
|---|---|---|
| Ocado | Available | M&S Select Farms British Semi Skimmed Milk 1 Pint, £0.85 |
| Morrisons | Available | Morrisons British Semi Skimmed Milk 2 Pint, £1.20 |
| Sainsbury's | Available | Dairy Pride Semi-Skimmed Longer Lasting UHT Milk 1 Litre, £0.69 |
| Waitrose | Unavailable | `source_unavailable` after the bounded timeout |
| Tesco | Unavailable | `disabled_by_policy` |
| Asda | Unavailable | `disabled_by_policy` |
| Iceland | Unavailable | `disabled_by_policy` |

The Morrisons match linked to `/morrisons-british-semi-skimmed-milk-2-pint/113239376`; the displayed product name and URL slug agree. The recorded Morrisons parser test separately rejects a milk record paired with a ham card.

## Representative basket

Request ingredients were `milk`, duplicate `Milk`, and `pasta`; normalisation produced two requested ingredients.

| Rank | Retailer | Availability | Matches | Total | Comparable |
|---:|---|---|---:|---:|---:|
| 1 | Sainsbury's | available | 2/2 | £1.10 | yes |
| 2 | Ocado | available | 2/2 | £1.60 | yes |
| 3 | Morrisons | partial | 1/2 | £1.20 subtotal | no |
| 4–7 | Asda, Iceland, Tesco, Waitrose | unavailable | 0/2 | £0 subtotal | no |

The run completed in 8.22 seconds. It demonstrates that complete baskets rank before a cheaper partial subtotal and that unavailable £0 subtotals are explicitly non-comparable and placed last.

Prices are point-in-time smoke-test observations, not promised prices or durable fixtures. Consumers must follow the returned retailer link to verify current price and availability.
