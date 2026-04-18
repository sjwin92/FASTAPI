from __future__ import annotations

from app.adapters.base import RetailerAdapter, RetailerInfo
from app.adapters.morrisons import MorrisonsAdapter
from app.adapters.ocado import OcadoAdapter
from app.adapters.tesco import TescoAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, RetailerAdapter] = {}

    def register(self, adapter: RetailerAdapter) -> None:
        self._adapters[adapter.info.key] = adapter

    def get(self, key: str) -> RetailerAdapter | None:
        return self._adapters.get(key)

    def list_retailers(self) -> list[RetailerInfo]:
        return [adapter.info for adapter in self._adapters.values()]


registry = AdapterRegistry()
registry.register(TescoAdapter())
registry.register(MorrisonsAdapter())
registry.register(OcadoAdapter())
