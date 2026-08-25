# Retailer source policy

Last reviewed: 25 August 2026. This is an engineering risk assessment, not legal advice.

The service uses only public retailer surfaces and has no paid data API. That does **not** mean automated reuse is permitted. Production use should obtain written permission or replace these adapters with authorised feeds.

| Retailer | Default provider | Default state | Assessment |
|---|---|---|---|
| Tesco | Tesco public grocery pages | Disabled | Tesco's published terms explicitly prohibit automated access without prior written consent. |
| Sainsbury's | Internal endpoint used by its public grocery UI | Enabled for evaluation | No public API licence was found. Permission remains uncertain; do not assume production rights. |
| Morrisons | Public grocery search page | Enabled for evaluation | The grocery sale terms do not clearly grant automated reuse. Permission remains uncertain. |
| Ocado | Internal endpoint used by its public grocery UI | Enabled for evaluation | No public API licence was found. Permission remains uncertain; do not assume production rights. |
| Waitrose | Public search/product pages | Enabled for evaluation | No public API licence was found and automated server traffic is often blocked. Permission remains uncertain. |
| Asda | Trolley.co.uk comparison pages | Disabled | Trolley's terms explicitly prohibit automated access. No authorised replacement is configured. |
| Iceland | Trolley.co.uk comparison pages | Disabled | Trolley's terms explicitly prohibit automated access. No authorised replacement is configured. |

Primary references:

- [Tesco general terms](https://www.tesco.com/shop/en-GB/zone/general-terms-and-conditions)
- [Trolley terms](https://www.trolley.co.uk/terms/)
- [Morrisons grocery terms](https://groceries.morrisons.com/content/terms-and-conditions)

`ENABLE_RESTRICTED_SOURCES=true` exists only for an operator who has obtained the necessary permission. It enables the existing Tesco and Trolley adapters. Do not set it merely to bypass the safe default.

Adapter failures and policy-disabled sources are returned as `errors`, not `not_found`. A provider may be replaced behind `BaseAdapter` without changing the basket endpoint.
