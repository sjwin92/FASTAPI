import os

from .base import BaseAdapter, DisabledAdapter
from .tesco import TescoAdapter
from .ocado import OcadoAdapter
from .morrisons import MorrisonsAdapter
from .sainsburys import SainsburysAdapter
from .trolley import TrolleyRetailerAdapter
from .waitrose import WaitroseAdapter

_restricted_sources_enabled = (
    os.getenv("ENABLE_RESTRICTED_SOURCES", "false").casefold() == "true"
)

_ADAPTERS: dict[str, BaseAdapter] = {
    "tesco": TescoAdapter() if _restricted_sources_enabled else DisabledAdapter("tesco"),
    "ocado": OcadoAdapter(),
    "morrisons": MorrisonsAdapter(),
    "sainsburys": SainsburysAdapter(),
    "waitrose": WaitroseAdapter(),
}

# Trolley's published terms prohibit automated access. Keep the provider behind
# the adapter interface for authorised deployments, but disable it by default.
for _key in ("asda", "iceland"):
    _ADAPTERS[_key] = (
        TrolleyRetailerAdapter(_key)
        if _restricted_sources_enabled
        else DisabledAdapter(_key)
    )

RETAILER_NAMES: dict[str, str] = {
    "tesco": "Tesco",
    "ocado": "Ocado",
    "morrisons": "Morrisons",
    "sainsburys": "Sainsbury's",
    "waitrose": "Waitrose",
    "asda": "Asda",
    "iceland": "Iceland",
}


def get_adapter(retailer: str) -> BaseAdapter | None:
    return _ADAPTERS.get(retailer)


def all_adapters() -> list[BaseAdapter]:
    return list(_ADAPTERS.values())


def retailer_metadata() -> list[dict]:
    metadata = []
    for key, name in RETAILER_NAMES.items():
        adapter = _ADAPTERS[key]
        enabled = not isinstance(adapter, DisabledAdapter)
        metadata.append(
            {
                "key": key,
                "name": name,
                "enabled": enabled,
                "disabled_reason": (
                    None
                    if enabled
                    else "Automated source disabled pending permission."
                ),
                "capabilities": (
                    [
                        "search",
                        "basket_compare",
                        "quantity_aware",
                        "shopping_plan",
                        "split_plan",
                    ]
                    if enabled else []
                ),
            }
        )
    return metadata
