"""Lazy, retryable S3 access: an unavailable MinIO must not crash a worker."""
import asyncio
import logging
import time

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import settings
from .concurrency import complete_before_cancel

logger = logging.getLogger(__name__)


class StorageService:
    def __init__(self):
        # Client construction does no network I/O with explicit credentials.
        self.client = boto3.client(
            "s3", endpoint_url=settings.S3_ENDPOINT_URL,
            aws_access_key_id=settings.S3_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            config=Config(
                signature_version="s3v4", region_name="us-east-1",
                s3={"addressing_style": "path"},
                max_pool_connections=settings.S3_UPLOAD_CONCURRENCY + 4,
                connect_timeout=settings.S3_CONNECT_TIMEOUT,
                read_timeout=settings.S3_READ_TIMEOUT,
                retries={"total_max_attempts": 2, "mode": "standard"},
            ),
        )
        self.bucket_name = settings.S3_BUCKET_NAME
        self.ready = False
        self._next_check = 0.0
        self._check_lock = asyncio.Lock()
        self._uploads = asyncio.Semaphore(settings.S3_UPLOAD_CONCURRENCY)

    def _ensure_bucket_exists(self):
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            try:
                self.client.create_bucket(Bucket=self.bucket_name)
            except ClientError as create_exc:
                # Another replica may create the bucket between HEAD and CREATE.
                if create_exc.response["Error"]["Code"] != "BucketAlreadyOwnedByYou":
                    raise

    async def ensure_ready(self, force=False):
        if self.ready and not force:
            return True
        if time.monotonic() < self._next_check:
            return self.ready
        async with self._check_lock:
            if self.ready and not force:
                return True
            if time.monotonic() < self._next_check:
                return self.ready
            try:
                await asyncio.to_thread(self._ensure_bucket_exists)
                self.ready = True
                self._next_check = time.monotonic() + settings.S3_RETRY_INTERVAL
                logger.info("S3 bucket is available")
            except Exception as exc:
                self.ready = False
                self._next_check = time.monotonic() + settings.S3_RETRY_INTERVAL
                logger.warning("S3 unavailable; worker stays live and will retry: %s", exc)
            return self.ready

    def upload(self, file_path: str, object_name: str, content_type: str = "image/png"):
        self.client.upload_file(file_path, self.bucket_name, object_name,
                                ExtraArgs={"ContentType": content_type, "ACL": "public-read"})

    def download_stream(self, object_name: str):
        try:
            return self.client.get_object(Bucket=self.bucket_name, Key=object_name)["Body"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
                return None
            raise

    async def _upload(self, operation, *args, **kwargs):
        # Requests retain admission until upload completes; there is no unbounded
        # population of background tasks retaining image bytes behind this semaphore.
        async with self._uploads:
            if not await self.ensure_ready():
                return False

            async def transfer():
                try:
                    await asyncio.to_thread(operation, *args, **kwargs)
                    return True
                except Exception as exc:
                    self.ready = False
                    self._next_check = time.monotonic() + settings.S3_RETRY_INTERVAL
                    logger.warning("S3 upload failed; bucket check will retry: %s", exc)
                    return False

            return await complete_before_cancel(transfer())

    async def aio_upload(self, file_path: str, object_name: str, content_type: str = "image/png") -> bool:
        return await self._upload(self.upload, file_path, object_name, content_type)

    async def aio_put_bytes(self, key: str, data: bytes, content_type: str = "image/png") -> bool:
        return await self._upload(self.client.put_object, Bucket=self.bucket_name,
                                  Key=key, Body=data, ContentType=content_type, ACL="public-read")

    async def close(self):
        await asyncio.to_thread(self.client.close)


storage_service = StorageService()
