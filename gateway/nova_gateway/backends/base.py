"""
base.py — Abstract base class for all AI backends.

Author: Jordan Koch
"""

from abc import ABC, abstractmethod
from typing import Optional, Any
import httpx
import time
import logging

logger = logging.getLogger(__name__)


class BaseBackend(ABC):
    name: str = "base"

    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self):
        await self._client.aclose()

    @abstractmethod
    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        """Send a query and return a dict with at least 'response' key."""
        ...

    async def health_check(self) -> tuple[bool, float]:
        """Returns (is_available, latency_ms). Override per backend."""
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code < 500, latency
        except Exception as e:
            logger.debug(f"{self.name} health check failed: {e}")
            return False, 0.0

    async def _post(self, path: str, data: dict) -> dict:
        r = await self._client.post(f"{self.url}{path}", json=data)
        r.raise_for_status()
        return r.json()

    async def _get(self, path: str) -> dict:
        r = await self._client.get(f"{self.url}{path}")
        r.raise_for_status()
        return r.json()
