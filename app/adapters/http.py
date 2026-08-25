"""Shared bounded HTTP policy for retailer adapters."""

from __future__ import annotations

import os

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


CONNECT_TIMEOUT = float(os.getenv("ADAPTER_CONNECT_TIMEOUT_SECONDS", "3.05"))
READ_TIMEOUT = float(os.getenv("ADAPTER_READ_TIMEOUT_SECONDS", "8"))
REQUEST_TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)
MAX_RETRIES = max(0, min(int(os.getenv("ADAPTER_MAX_RETRIES", "1")), 2))


def create_session(headers: dict[str, str]) -> requests.Session:
    retry = Retry(
        total=MAX_RETRIES,
        connect=MAX_RETRIES,
        # Retrying a completed connection can double slow-source latency and is
        # less likely to help than retrying connection/status failures.
        read=0,
        status=MAX_RETRIES,
        backoff_factor=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    session = requests.Session()
    session.headers.update(headers)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
