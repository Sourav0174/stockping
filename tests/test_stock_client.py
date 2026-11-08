from unittest.mock import AsyncMock, patch
import httpx
import pytest

from app.services.stock_client import (
    StockAPIConnectionError,
    StockAPIResponseError,
    StockClient,
)


@pytest.mark.asyncio
async def test_stock_client_catalogue_list():
    client = StockClient(base_url="http://mock-stock.test", api_key="secret-token")

    mock_resp = httpx.Response(
        status_code=200,
        json=[{"sku": "HOOD-1", "quantity": 5}, {"sku": "TEE-2", "quantity": 0}],
        request=httpx.Request("GET", "http://mock-stock.test"),
    )

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        catalogue = await client.get_catalogue()
        assert len(catalogue) == 2
        assert await client.sku_exists("HOOD-1") is True
        assert await client.sku_exists("UNKNOWN-SKU") is False


@pytest.mark.asyncio
async def test_stock_client_catalogue_wrapped_dict():
    client = StockClient(base_url="http://mock-stock.test", api_key="secret-token")

    mock_resp = httpx.Response(
        status_code=200,
        json={"items": [{"sku": "BAG-99", "in_stock": True}]},
        request=httpx.Request("GET", "http://mock-stock.test"),
    )

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        assert await client.sku_exists("BAG-99") is True


@pytest.mark.asyncio
async def test_stock_client_sku_stock_query():
    client = StockClient(base_url="http://mock-stock.test", api_key="secret-token")

    mock_resp = httpx.Response(
        status_code=200,
        json={"sku": "CAP-01", "quantity": 12, "in_stock": True},
        request=httpx.Request("GET", "http://mock-stock.test/CAP-01"),
    )

    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_resp)):
        stock_info = await client.get_sku_stock("CAP-01")
        assert stock_info["quantity"] == 12
        assert stock_info["in_stock"] is True


@pytest.mark.asyncio
async def test_stock_client_error_handling():
    client = StockClient(base_url="http://mock-stock.test", api_key="secret-token")

    # Connection failure
    with patch("httpx.AsyncClient.get", new=AsyncMock(side_effect=httpx.ConnectError("Network down"))):
        with pytest.raises(StockAPIConnectionError):
            await client.get_catalogue()

    # Upstream 500 error
    mock_500 = httpx.Response(
        status_code=500,
        text="Internal Server Error",
        request=httpx.Request("GET", "http://mock-stock.test"),
    )
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=mock_500)):
        with pytest.raises(StockAPIResponseError) as exc_info:
            await client.get_catalogue()
        assert exc_info.value.status_code == 500
