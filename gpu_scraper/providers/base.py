"""Abstract base provider with shared HTTP client and error isolation."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

import httpx

from ..models import GPUOffer

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


class BaseProvider(ABC):
    name: str = ""
    timeout: float = 30.0

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=DEFAULT_HEADERS,
                timeout=self.timeout,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    @abstractmethod
    async def _scrape(self) -> list[GPUOffer]:
        """Provider-specific implementation. Must not swallow exceptions."""
        ...

    async def fetch(self) -> list[GPUOffer]:
        """Public entry point — isolates failures so other providers keep running."""
        import sys as _sys
        try:
            offers = await self._scrape()
            logger.info("[%s] fetched %d offers", self.name, len(offers))
            return offers
        except httpx.TimeoutException:
            msg = f"[{self.name}] request timed out"
            logger.warning(msg)
            print(msg, file=_sys.stderr)
        except httpx.HTTPStatusError as exc:
            msg = f"[{self.name}] HTTP {exc.response.status_code}: {exc.request.url}"
            logger.warning(msg)
            print(msg, file=_sys.stderr)
        except Exception as exc:  # noqa: BLE001
            msg = f"[{self.name}] unexpected error: {type(exc).__name__}: {exc}"
            logger.warning(msg)
            print(msg, file=_sys.stderr)
        return []
