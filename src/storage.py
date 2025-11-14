import boto3
import logging
from botocore.client import Config
from botocore.exceptions import ClientError
from .config import settings

# 配置日志
logger = logging.getLogger(__name__)

class StorageService:
    """
    S3 兼容存储服务，负责文件的上传和下载。
    """
    def __init__(self):
        try:
            self.client = boto3.client(
                "s3",
                endpoint_url=settings.S3_ENDPOINT_URL,
                aws_access_key_id=settings.S3_ACCESS_KEY_ID,
                aws_secret_access_key=settings.S3_SECRET_ACCESS_KEY,
                config=Config(
                    signature_version="s3v4",
                    region_name='us-east-1',
                    s3={'addressing_style': 'path'}
                ),
            )
            self.bucket_name = settings.S3_BUCKET_NAME
            logger.info(f"已成功连接到 S3 兼容存储: {settings.S3_ENDPOINT_URL}")
            self._ensure_bucket_exists()
        except Exception as e:
            logger.error(f"连接 S3 兼容存储失败: {e}")
            raise

    def _ensure_bucket_exists(self):
        """确保指定的 bucket 存在，如果不存在则创建。"""
        try:
            self.client.head_bucket(Bucket=self.bucket_name)
            logger.info(f"Bucket '{self.bucket_name}' 已存在。")
        except ClientError as e:
            # 如果错误码是 404 (not found)，则尝试创建 bucket
            if e.response['Error']['Code'] == '404':
                try:
                    self.client.create_bucket(Bucket=self.bucket_name)
                    logger.info(f"成功创建 bucket: '{self.bucket_name}'。")
                except ClientError as create_error:
                    logger.error(f"创建 bucket '{self.bucket_name}' 失败: {create_error}")
                    raise
            else:
                logger.error(f"检查 bucket '{self.bucket_name}' 时发生未知错误: {e}")
                raise

    def upload(self, file_path: str, object_name: str, content_type: str = "image/png"):
        """
        将本地文件上传到 S3 兼容存储。

        :param file_path: 本地文件路径
        :param object_name: 存储在 bucket 中的对象名 (key)
        :param content_type: 文件的 MIME 类型
        """
        try:
            self.client.upload_file(
                file_path,
                self.bucket_name,
                object_name,
                ExtraArgs={"ContentType": content_type, "ACL": "public-read"} # 设置为公开读，方便通过代理访问
            )
            logger.info(f"文件 '{file_path}' 已成功上传为对象 '{object_name}'。")
        except Exception as e:
            logger.error(f"上传文件 '{file_path}' 到 '{object_name}' 失败: {e}")
            raise

    def download_stream(self, object_name: str):
        """
        从 S3 兼容存储流式下载对象。

        :param object_name: 要下载的对象名 (key)
        :return: 文件的流式读取对象
        """
        try:
            response = self.client.get_object(Bucket=self.bucket_name, Key=object_name)
            streaming_body = response['Body']
            logger.info(f"开始流式下载对象 '{object_name}'。")
            return streaming_body
        except ClientError as e:
            if e.response['Error']['Code'] == 'NoSuchKey':
                logger.warning(f"对象 '{object_name}' 不存在。")
                return None
            else:
                logger.error(f"下载对象 '{object_name}' 失败: {e}")
                raise
        except Exception as e:
            logger.error(f"下载对象 '{object_name}' 时发生未知错误: {e}")
            raise

# 创建一个全局的 storage_service 实例供其他模块使用
storage_service = StorageService()