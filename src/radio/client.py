"""Radio login client — connects to BH radios for live status.

Supports Aviat, Cambium CN820, and other vendors via HTTP/SNMP.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from src.config import settings


class RadioClient:
    """Connect to backhaul radios for live status retrieval."""

    def __init__(self) -> None:
        self._username = settings.radio_username
        self._password = settings.radio_password
        self._client = httpx.AsyncClient(
            timeout=10.0,
            verify=False,  # radios use self-signed certs
        )

    async def get_status(self, ip: str, radio_family: str) -> Optional[dict[str, Any]]:
        """Get live status from a radio by IP and family.

        Dispatches to the appropriate vendor handler.
        """
        handlers = {
            "aviat": self._get_aviat_status,
            "cambium_cn820": self._get_cambium_status,
        }
        handler = handlers.get(radio_family)
        if not handler:
            return None
        try:
            return await handler(ip)
        except Exception:
            return None

    async def _get_aviat_status(self, ip: str) -> Optional[dict]:
        """Query Aviat radio via its web API.

        TODO: Implement based on Aviat WTM API docs.
        Aviat radios typically expose a REST API on port 443.
        """
        # Placeholder — will be implemented once we confirm the Aviat API
        return {"vendor": "aviat", "ip": ip, "status": "not_implemented"}

    async def _get_cambium_status(self, ip: str) -> Optional[dict]:
        """Query Cambium CN820 via its web API.

        TODO: Implement based on Cambium CN820 API docs.
        """
        return {"vendor": "cambium_cn820", "ip": ip, "status": "not_implemented"}

    async def close(self) -> None:
        await self._client.aclose()
