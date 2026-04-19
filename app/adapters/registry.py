from .base import BaseAdapter
from .tesco import TescoAdapter
from .ocado import OcadoAdapter
from .morrisons import MorrisonsAdapter
from .sainsburys import SainsburysAdapter
from .trolley import TrolleyRetailerAdapter

_ADAPTERS: dict[str, BaseAdapter] = {
    a.retailer_key: a()
    for a in [TescoAdapter, OcadoAdapter, MorrisonsAdapter, SainsburysAdapter]
}

# Waitrose TCP-drops server-side requests via Akamai CDN — use trolley.co.uk as source.
# Asda and Iceland have no direct public API; trolley covers them too.
for _key in ("waitrose", "asda", "iceland"):
    _ADAPTERS[_key] = TrolleyRetailerAdapter(_key)

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
