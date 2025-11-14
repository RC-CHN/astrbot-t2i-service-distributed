from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """
    应用配置类，通过环境变量或 .env 文件加载配置。
    """
    # S3 兼容存储配置 (MinIO 和 S3 通用)
    # 如果使用 AWS S3，可以留空，boto3 会自动使用官方 endpoint
    S3_ENDPOINT_URL: str | None = "http://minio:9000"
    S3_ACCESS_KEY_ID: str = "minioadmin"
    S3_SECRET_ACCESS_KEY: str = "minioadmin"
    S3_BUCKET_NAME: str = "text2img"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"

# 创建一个全局的 settings 实例供其他模块使用
settings = Settings()