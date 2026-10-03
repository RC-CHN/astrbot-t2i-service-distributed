from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-configurable per-worker resource budgets."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    S3_ENDPOINT_URL: str | None = "http://minio:9000"
    S3_ACCESS_KEY_ID: str = "minioadmin"
    S3_SECRET_ACCESS_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "text2img"
    S3_CONNECT_TIMEOUT: float = Field(default=2, gt=0)
    S3_READ_TIMEOUT: float = Field(default=5, gt=0)
    S3_RETRY_INTERVAL: float = Field(default=10, gt=0)
    S3_UPLOAD_CONCURRENCY: int = Field(default=2, ge=1)
    REDIS_URL: str = "redis://localhost:6379/0"
    IMAGE_DOWNLOAD_CONCURRENCY: int = Field(default=16, ge=1, le=128)

    RENDER_CONCURRENCY: int = Field(default=2, ge=1, le=16)
    RENDER_QUEUE_SIZE: int = Field(default=4, ge=0, le=100)
    RENDER_QUEUE_TIMEOUT: float = Field(default=10, gt=0)
    RENDER_TIMEOUT: float = Field(default=30, gt=0)
    RENDER_ASSET_TIMEOUT_MS: int = Field(default=5000, ge=0)
    RENDER_SCREENSHOT_TIMEOUT_MS: int = Field(default=10000, gt=0)
    RENDER_MAX_HTML_BYTES: int = Field(default=33554432, gt=0)
    RENDER_MAX_REQUEST_BYTES: int = Field(default=41943040, gt=0)
    RENDER_MAX_VIEWPORT_WIDTH: int = Field(default=2048, gt=0)
    RENDER_MAX_VIEWPORT_HEIGHT: int = Field(default=4096, gt=0)
    RENDER_MAX_DIMENSION: int = Field(default=16384, gt=0)
    RENDER_MAX_PIXELS: int = Field(default=16000000, gt=0)
    RENDER_MAX_IMAGE_BYTES: int = Field(default=16777216, gt=0)
    RENDER_MAX_RESOURCE_REQUESTS: int = Field(default=100, ge=0)
    RENDER_JAVASCRIPT_ENABLED: bool = True
    BROWSER_MAX_RENDERS: int = Field(default=100, ge=1)
    BROWSER_MAX_AGE: float = Field(default=600, gt=0)


settings = Settings()
