# Frontend handoff — basket API 1.2.0

Kitchen Companion can keep sending the legacy request unchanged:

```json
{"ingredients":["milk","pasta"]}
```

Use the structured request when the app knows the required quantities:

```json
{
  "items": [
    {"name":"milk","quantity":1,"unit":"l"},
    {"name":"pasta","quantity":500,"unit":"g"}
  ]
}
```

Send exactly one of `ingredients` or `items`. Valid structured units are `g`, `kg`, `ml`, `cl`, `l`, and `each`; `each` must be integral. Invalid quantities, unsupported cooking measures, or duplicate names with incompatible dimensions return HTTP 422.

## Rendering rules

- Treat a retailer as price-comparable only when `total_is_comparable` is `true`.
- Display `total` as a subtotal with an incomplete warning otherwise.
- In `quantity_aware` mode, show `price` as the one-pack price and `line_total` as the selected line's checkout cost.
- `packs_needed`, `supplied_quantity`, and `excess_quantity` explain package rounding. Supplied and excess values use `requested_unit`; package values use canonical `g`, `ml`, or `each`.
- Render `not_found`, `coverage_issues`, and `errors` separately. Coverage issue codes are `no_acceptable_variant`, `package_size_unknown`, and `unit_incompatible`.
- Continue linking each item to its retailer `url` so the user can verify current price and availability.
- `/retailers` exposes `enabled`, `disabled_reason`, and `capabilities`. Use it for configured availability, not live source health.

All new response properties are additive. Legacy requests return `calculation_mode: "one_pack"`; their one-package totals are unchanged.

## Server configuration

The Kitchen Companion server should set:

- `PRICING_API_URL` to the deployed API base URL when one exists.
- `PRICING_API_KEY` only if the pricing service has `BASKET_API_KEY` configured.

Keep the key server-side and send it as `Authorization: Bearer …`. No deployment URL was created by this change. The pricing API does not require Supabase for basket comparison.

## Known limitations

Retailer sources are public, fragile surfaces rather than a paid pricing API. A source may be unavailable or expose no reliable package size. Coverage therefore fails closed, location-specific prices may differ, and every returned price should be verified at the retailer link. Tesco and the Trolley-backed Asda and Iceland sources remain disabled by policy unless written permission is obtained.
