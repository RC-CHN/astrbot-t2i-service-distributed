import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.cache import RedisImageCache


def test_failed_connect_closes_client_and_throttles_reconnect():
    client = MagicMock(ping=AsyncMock(side_effect=OSError("offline")), aclose=AsyncMock())
    async def scenario():
        cache = RedisImageCache()
        with patch("src.cache.aioredis.Redis.from_url", return_value=client) as factory:
            assert await cache.get("missing") is None
            assert await cache.set("key", b"value") is False
            factory.assert_called_once()
            client.aclose.assert_awaited_once()
            assert not cache.healthy
            cache._next_retry = 0
            client.ping.side_effect = None
            client.getrange = AsyncMock(return_value=b"value")
            assert await cache.get("key") == b"value"
            assert cache.healthy
    asyncio.run(scenario())


def test_concurrent_reconnect_constructs_one_client():
    client = MagicMock(ping=AsyncMock(return_value=True), getrange=AsyncMock(return_value=b"value"), aclose=AsyncMock())
    async def scenario():
        cache = RedisImageCache()
        with patch("src.cache.aioredis.Redis.from_url", return_value=client) as factory:
            results = await asyncio.gather(*(cache.get("key") for _ in range(20)))
            assert results == [b"value"] * 20
            factory.assert_called_once()
            await cache.disconnect()
            client.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_runtime_error_closes_failed_connection():
    client = MagicMock(ping=AsyncMock(return_value=True), getrange=AsyncMock(side_effect=OSError("gone")), aclose=AsyncMock())
    async def scenario():
        cache = RedisImageCache()
        with patch("src.cache.aioredis.Redis.from_url", return_value=client):
            assert await cache.get("key") is None
            assert not cache.healthy
            client.aclose.assert_awaited_once()
    asyncio.run(scenario())


def test_download_reads_only_bounded_redis_range_and_preserves_cache_miss():
    from src.config import settings
    client = MagicMock(getrange=AsyncMock(side_effect=[b"123456789", b""]))
    async def scenario():
        cache = RedisImageCache()
        cache._redis = client
        with patch.object(settings, "RENDER_MAX_IMAGE_BYTES", 8):
            assert await cache.get("image") == b"123456789"
            client.getrange.assert_awaited_with("image", 0, 8)
            assert await cache.get("missing") is None
    asyncio.run(scenario())
