"""Fail-open Redis cache with bounded operations and serialized reconnects."""
import asyncio
import time

import redis.asyncio as aioredis
from loguru import logger
from .config import settings


class RedisImageCache:
    def __init__(self):
        self._redis = None
        self._lock = asyncio.Lock()
        self._next_retry = 0.0

    async def connect(self):
        if self._redis is not None or time.monotonic() < self._next_retry:
            return
        async with self._lock:
            if self._redis is not None or time.monotonic() < self._next_retry:
                return
            client = aioredis.Redis.from_url(
                settings.REDIS_URL, decode_responses=False,
                socket_connect_timeout=1, socket_timeout=1,
                socket_keepalive=True, health_check_interval=30,
                retry_on_timeout=False,
                max_connections=(settings.RENDER_CONCURRENCY + settings.RENDER_QUEUE_SIZE
                                 + settings.IMAGE_DOWNLOAD_CONCURRENCY + 4),
            )
            try:
                await client.ping()
                self._redis = client
            except Exception as exc:
                await client.aclose()
                self._next_retry = time.monotonic() + 5
                logger.warning("Redis unavailable; retrying later: {}", exc)

    async def disconnect(self):
        client, self._redis = self._redis, None
        if client is not None:
            await client.aclose()

    async def _operation(self, operation, *args, **kwargs):
        await self.connect()
        client = self._redis
        if client is None:
            return None
        try:
            return await getattr(client, operation)(*args, **kwargs)
        except Exception as exc:
            if self._redis is client:
                self._redis = None
                self._next_retry = time.monotonic() + 5
                await client.aclose()
            logger.warning("Redis {} failed: {}", operation, exc)
            return None

    @property
    def healthy(self):
        return self._redis is not None

    async def check(self):
        return bool(await self._operation("ping"))

    async def get(self, key):
        # Bound the Redis response itself, including historical oversized images.
        # GETRANGE's inclusive end gives one extra byte for the API's 413 check.
        data = await self._operation("getrange", key, 0, settings.RENDER_MAX_IMAGE_BYTES)
        return data or None

    async def set(self, key, data, ttl=None):
        return bool(await self._operation("set", key, data, ex=ttl))
