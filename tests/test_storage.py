"""Tests for the S3-compatible storage service.

Relies on the module-level boto3.client mock in conftest.py to prevent
any real network connections.
"""

import pytest
from unittest.mock import MagicMock
from botocore.exceptions import ClientError


def _new_mock_client():
    """Create a fresh mock client and install it as boto3.client.return_value."""
    import boto3

    boto3.client.reset_mock()
    mock = MagicMock()
    boto3.client.return_value = mock
    return mock


def _make_service():
    """Create a StorageService with a fresh mock boto3 client."""
    _new_mock_client()
    import src.storage as _storage

    return _storage.StorageService()


class TestStorageServiceInit:
    """Tests for StorageService initialization."""

    def test_init_creates_client_with_correct_config(self):
        from src.config import settings

        _make_service()
        import boto3

        assert boto3.client.called
        # First positional arg is the service name "s3"
        call_args, call_kwargs = boto3.client.call_args
        assert call_args[0] == "s3"
        assert call_kwargs["endpoint_url"] == settings.S3_ENDPOINT_URL
        assert call_kwargs["aws_access_key_id"] == settings.S3_ACCESS_KEY_ID
        assert call_kwargs["aws_secret_access_key"] == settings.S3_SECRET_ACCESS_KEY

    def test_init_checks_bucket_exists(self):
        svc = _make_service()
        svc.client.head_bucket.assert_called_once_with(Bucket="text2img")

    def test_bucket_created_when_not_found(self):
        mock = _new_mock_client()
        mock.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )

        import src.storage as _storage

        _storage.StorageService()
        mock.create_bucket.assert_called_once_with(Bucket="text2img")

    def test_init_raises_on_bucket_error(self):
        mock = _new_mock_client()
        mock.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket"
        )

        import src.storage as _storage

        with pytest.raises(ClientError):
            _storage.StorageService()


class TestStorageServiceMethods:
    """Tests for StorageService upload/download methods."""

    @pytest.fixture
    def svc(self):
        return _make_service()

    def test_upload_calls_upload_file(self, svc):
        svc.upload("/tmp/test.png", "data/rendered/test.png", content_type="image/png")
        svc.client.upload_file.assert_called_once_with(
            "/tmp/test.png",
            "text2img",
            "data/rendered/test.png",
            ExtraArgs={"ContentType": "image/png", "ACL": "public-read"},
        )

    def test_download_stream_returns_body(self, svc):
        expected_body = MagicMock()
        svc.client.get_object.return_value = {"Body": expected_body}

        result = svc.download_stream("data/rendered/test.png")

        assert result is expected_body
        svc.client.get_object.assert_called_once_with(
            Bucket="text2img", Key="data/rendered/test.png"
        )

    def test_download_stream_returns_none_for_missing_key(self, svc):
        svc.client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"
        )

        result = svc.download_stream("nonexistent.png")
        assert result is None

    def test_download_stream_raises_on_other_error(self, svc):
        svc.client.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Denied"}}, "GetObject"
        )

        with pytest.raises(ClientError):
            svc.download_stream("secret.png")
