from typing import Any
import httpx

from app.config import settings


class StockAPIException(Exception):
    """Base exception for upstream Stock API errors."""
    pass


class StockAPIConnectionError(StockAPIException):
    """Raised on connection, timeout, or DNS failures."""
    pass


class StockAPIResponseError(StockAPIException):
    """Raised when the Stock API returns an unexpected HTTP status."""
    def __init__(self, status_code: int, message: str):
        super().__init__(f"Stock API error {status_code}: {message}")
        self.status_code = status_code


class StockClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 5.0,
    ):
        self.base_url = (base_url or settings.STOCK_API_URL).rstrip("/")
        self.api_key = api_key or settings.STOCK_API_KEY
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def get_catalogue(self) -> list[Any]:
        """
        Retrieves the full product catalogue from GET {STOCK_API_URL}.
        """
        url = self.base_url
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url, headers=self._headers())
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
                raise StockAPIConnectionError(f"Failed to connect to stock service: {exc}") from exc

            if response.status_code != 200:
                raise StockAPIResponseError(
                    response.status_code,
                    f"Catalogue request failed with status {response.status_code}"
                )

            data = response.json()
            if isinstance(data, list):
                return data
            elif isinstance(data, dict):
                # Common collection wrappers
                for key in ("items", "products", "catalogue", "data"):
                    if key in data and isinstance(data[key], list):
                        return data[key]
                # If dict maps SKUs to details
                return list(data.values())
            return []

    async def get_sku_stock(self, sku: str) -> dict[str, Any]:
        """
        Queries individual SKU stock: GET {STOCK_API_URL}/{sku}.
        Returns dict with quantity, in_stock, etc.
        """
        url = f"{self.base_url}/{sku}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            try:
                response = await client.get(url, headers=self._headers())
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError) as exc:
                raise StockAPIConnectionError(f"Failed to connect to stock service for SKU '{sku}': {exc}") from exc

            if response.status_code == 404:
                return {}
            if response.status_code != 200:
                raise StockAPIResponseError(
                    response.status_code,
                    f"Stock check failed for SKU '{sku}' with status {response.status_code}"
                )

            return response.json()

    async def sku_exists(self, sku: str) -> bool:
        """
        Verifies that a SKU exists in the upstream catalogue.
        Checks GET {STOCK_API_URL} catalogue.
        If catalogue is empty, falls back to checking GET {STOCK_API_URL}/{sku}.
        """
        catalogue = await self.get_catalogue()
        sku_normalized = sku.strip()

        if catalogue:
            for item in catalogue:
                if isinstance(item, dict):
                    item_sku = str(item.get("sku") or item.get("id") or item.get("code") or "").strip()
                    if item_sku == sku_normalized:
                        return True
                elif isinstance(item, str):
                    if item.strip() == sku_normalized:
                        return True
            return False

        # Fallback if catalogue is empty
        stock_info = await self.get_sku_stock(sku_normalized)
        return bool(stock_info)


stock_client = StockClient()
