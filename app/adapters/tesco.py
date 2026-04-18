from __future__ import annotations

from urllib.parse import urlparse

from app.adapters.base import RetailerInfo


class TescoAdapter:
    info = RetailerInfo(key="tesco", name="Tesco", scraping_implemented=False)

    def is_supported_url(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("tesco.com")
