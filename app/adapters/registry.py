from .base import BaseAdapter
from .tesco import TescoAdapter
from .ocado import OcadoAdapter
from .morrisons import MorrisonsAdapter
from .waitrose import WaitroseAdapter
from .sainsburys import SainsburysAdapter

_ADAPTERS: dict[str, BaseAdapter] = {
    a.retailer_key: a()
    for a in [TescoAdapter, OcadoAdapter, MorrisonsAdapter, WaitroseAdapter, SainsburysAdapter]
}

RETAILER_NAMES: dict[str, str] = {
    "tesco": "Tesco",
    "ocado": "Ocado",
    "morrisons": "Morrisons",
    "waitrose": "Waitrose",
    "sainsburys": "Sainsbury's",
}


def get_adapter(retailer: str) -> BaseAdapter | None:
    return _ADAPTERS.get(retailer)


def all_adapters() -> list[BaseAdapter]:
    return list(_ADAPTERS.values())
