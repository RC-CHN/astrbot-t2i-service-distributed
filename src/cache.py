"""Redis-backed image cache for astrbot-t2i-service.

Provides an async get/set interface over Redis.  The cache sits in front
of S3/MinIO so that recently-generated images are served from memory
without hitting object storage on every GET request.

Images are stored as raw bytes with a TTL matching IMAGE_LIFETIME_HOURS.
"""

import redis.asyncio as aioredis
from loguru import logger
from .config import settings


class RedisImageCache:
    """Async Redis cache for rendered images."""

    def __init__(self) -> None:
        self._redis: aioredis.Redis | None = None

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisImageCache not connected")
        return self._redis

    async def connect(self) -> None:
        """Initialise the async Redis connection."""
        self._redis = aioredis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,  # raw bytes
        )
        await self._redis.ping()
        logger.info(f"Redis cache connected: {settings.REDIS_URL}")

    async def disconnect(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    async def get(self, key: str) -> bytes | None:
        """Return cached image bytes, or None on miss."""
        try:
            return await self.redis.get(key)
        except Exception as e:
            logger.warning(f"Redis get failed for {key}: {e}")
            return None

    async def set(self, key: str, data: bytes, ttl: int | None = None) -> bool:
        """Cache image bytes.  Returns True on success."""
        try:
            await self.redis.set(key, data, ex=ttl)
            return True
        except Exception as e:
            logger.warning(f"Redis set failed for {key}: {e}")
            return False  # fail-open: proceed without cache
