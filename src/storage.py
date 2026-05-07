import asyncio
import boto3
import logging
from botocore.client import Config
from botocore.exceptions import ClientError
from .config import settings

logger = logging.getLogger(__name__)

# Limit concurrent S3 uploads to avoid saturating the endpoint
# and to bound memory usage from background upload tasks.
_UPLOAD_SEMAPHORE = asyncio.Semaphore(20)
_UPLOAD_RETRIES = 3


class StorageService:
    """S3-compatible storage service with async-safe upload."""

    def __init__(self):
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT_URL,
                aws_access_key_id=settings.S3_ACCESS_KEY_ID,
                aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
                config=Config(
                    signature_version="s3v4",
                    region_name="us-east-1",
                    s3={"addressing_style": "path"},
                    max_pool_connections=50,
                ),
            )
            self.bucket_name = settings.S3_BUCKET_NAME
            logger.info("Connected to S3: %s", settings.S3_ENDPOINT_URL)
            self._ensure_bucket_exists()
        except Exception as e:
            logger.error("S3 connection failed: %s", e)
            raise

    def _ensure_bucket_exists(self):
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            logger.info("Bucket '%s' exists.", self.bucket_name)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                try:
                    self.client.create_bucket(Bucket=self.bucket_name)
                    logger.info("Created bucket '%s'.", self.bucket_name)
                except ClientError as create_error:
                    logger.error("Create bucket '%s' failed: %s", self.bucket_name, create_error)
                    raise
            else:
                raise

    # ── Sync (legacy, kept for internal use) ────────────────────────

    def upload(self, file_path: str, object_name: str, content_type: str = "image/png"):
        self.client.upload_file(
            file_path,
            self.bucket_name,
            object_name,
            ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
        )
        logger.info("Uploaded %s → %s", file_path, object_name)

    def download_stream(self, object_name: str):
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=object_name)
            logger.info("Streaming download: %s", object_name)
            return response["Body"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.warning("Key not found: %s", object_name)
                return None
            raise

    # ── Async upload (non-blocking, with retry + semaphore) ────────

    async def aio_upload(self, file_path: str, object_name: str, content_type: str = "image/png") -> bool:
        """Upload to S3 in a thread pool, with concurrency limit and retries.

        Returns True on success, False after exhausting retries.
        """
        async with _UPLOAD_SEMAPHORE:
            for attempt in range(1, _UPLOAD_RETRIES + 1):
                try:
                    await asyncio.to_thread(
                        self.client.upload_file,
                        file_path,
                        self.bucket_name,
                        object_name,
                        ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
                    )
                    logger.info("Uploaded %s → %s (attempt %d)", file_path, object_name, attempt)
                    return True
                except Exception as e:
                    delay = 2 ** (attempt - 1)  # 1s, 2s, 4s
                    logger.warning(
                        "Upload failed %s → %s (attempt %d/%d): %s.  Retry in %ds...",
                        file_path, object_name, attempt, _UPLOAD_RETRIES, e, delay,
                    )
                    if attempt < _UPLOAD_RETRIES:
                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            "Upload FAILED after %d retries: %s → %s: %s",
                            _UPLOAD_RETRIES, file_path, object_name, e,
                        )
                        return False
        return False


storage_service = StorageService()
