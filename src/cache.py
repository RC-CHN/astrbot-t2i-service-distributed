"""Redis-backed image cache for astrbot-t2i-service.

Provides an async get/set interface over Redis.  The cache sits in front
of S3/MinIO so that recently-generated images are served from memory
without hitting object storage on every GET request.

Resilience
----------
- Startup: non-fatal — worker starts even if Redis is down (cache degrades
  to S3-only until Redis recovers).
- Runtime: connection errors trigger an auto-reconnect attempt on the next
  get/set call so the cache self-heals.
"""

import redis.asyncio as aioredis
from loguru import logger

from .config import settings


class RedisImageCache:
    """Async Redis cache for rendered images.  Fail-open at all times."""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        """Initialise Redis connection (non-fatal).

        If Redis is unreachable the worker starts anyway; cache operations
        silently degrade to S3-only and auto-reconnect when Redis returns.
        """
        try:
            self._redis = aioredis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=False,
                socket_connect_timeout=3,
                socket_keepalive=True,
                retry_on_timeout=True,
                health_check_interval=30,
            )
            await self._redis.ping()
            logger.info("Redis cache connected: {}", settings.REDIS_URL)
        except Exception as e:
            logger.warning(
                "Redis unavailable ({}).  Cache disabled — serving from S3 only.  "
                "Will auto-reconnect on next cache operation.", e
            )
            self._redis = None

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ── fail-open helpers ─────────────────────────────────────────────

    async def _ensure_connected(self) -> bool:
        """Try to (re)connect if the client is missing.  Returns True if ready."""
        if self._redis is not None:
            return True
        # Single-shot reconnect attempt
        await self.connect()
        return self._redis is not None

    async def get(self, key: str) -> bytes | None:
        """Return cached image bytes, or None (cache miss / Redis down)."""
        if not await self._ensure_connected():
            return None
        try:
            return await self._redis.get(key)
        except Exception as e:
            logger.warning("Redis get failed ({}).  Disconnecting.", e)
            self._redis = None
            return None

    async def set(self, key: str, data: bytes, ttl: int | None = None) -> bool:
        """Cache image bytes.  Returns True on success, False = skipped."""
        if not await self._ensure_connected():
            return False
        try:
            await self._redis.set(key, data, ex=ttl)
            return True
        except Exception as e:
            logger.warning("Redis set failed ({}).  Disconnecting.", e)
            self._redis = None
            return False
