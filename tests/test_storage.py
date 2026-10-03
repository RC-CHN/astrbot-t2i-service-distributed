"""Tests for the S3-compatible storage service.

Relies on the module-level boto3.client mock in conftest.py to prevent
any real network connections.
"""

import pytest
import asyncio
import threading
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

    def test_init_does_not_contact_storage(self):
        svc = _make_service()
        svc.client.head_bucket.assert_not_called()
        assert asyncio.run(svc.ensure_ready())
        svc.client.head_bucket.assert_called_once_with(Bucket="text2img")

    def test_bucket_created_when_not_found(self):
        mock = _new_mock_client()
        mock.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadBucket"
        )

        import src.storage as _storage

        svc = _storage.StorageService()
        assert asyncio.run(svc.ensure_ready())
        mock.create_bucket.assert_called_once_with(Bucket="text2img")

    def test_forbidden_bucket_does_not_crash_worker(self):
        mock = _new_mock_client()
        mock.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadBucket"
        )

        import src.storage as _storage

        svc = _storage.StorageService()
        assert asyncio.run(svc.ensure_ready()) is False
        assert svc.ready is False


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


def test_offline_storage_recovers_without_restart():
    from botocore.exceptions import EndpointConnectionError
    svc = _make_service()
    svc.client.head_bucket.side_effect = EndpointConnectionError(endpoint_url="http://offline")
    async def scenario():
        assert not await svc.ensure_ready()
        assert not await svc.ensure_ready()  # cooldown prevents a request storm
        assert svc.client.head_bucket.call_count == 1
        svc.client.head_bucket.side_effect = None
        svc._next_check = 0
        assert await svc.ensure_ready()
        assert await svc.aio_put_bytes("test.png", b"png")
    asyncio.run(scenario())


def test_concurrent_storage_initialization_is_serialized():
    svc = _make_service()
    async def scenario():
        results = await asyncio.gather(*(svc.ensure_ready() for _ in range(20)))
        assert all(results)
        assert svc.client.head_bucket.call_count == 1
    asyncio.run(scenario())


def test_failed_upload_marks_storage_unavailable():
    svc = _make_service()
    svc.client.put_object.side_effect = RuntimeError("storage down")
    async def scenario():
        assert not await svc.aio_put_bytes("test.png", b"png")
        assert not svc.ready
    asyncio.run(scenario())


def test_cancelled_upload_retains_slot_until_physical_thread_finishes():
    svc = _make_service()
    svc.ready = True
    svc._uploads = asyncio.Semaphore(1)
    release = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        calls = []

        def put_object(**kwargs):
            calls.append(kwargs["Key"])
            loop.call_soon_threadsafe(started.set)
            if kwargs["Key"] == "first.png":
                assert release.wait(5), "test did not release upload thread"

        svc.client.put_object.side_effect = put_object
        first = asyncio.create_task(svc.aio_put_bytes("first.png", b"first image"))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 2)
            first.cancel()
            await asyncio.sleep(0)
            first.cancel()  # A second cancellation must not cancel the transfer.
            second = asyncio.create_task(svc.aio_put_bytes("second.png", b"second image"))
            await asyncio.sleep(0.02)
            assert not first.done()
            assert calls == ["first.png"]
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert await second
            assert calls == ["first.png", "second.png"]
        finally:
            release.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)

    asyncio.run(scenario())


def test_upload_failure_after_cancellation_still_marks_storage_unavailable():
    svc = _make_service()
    svc.ready = True
    release = threading.Event()

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def put_object(**kwargs):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "test did not release upload thread"
            raise RuntimeError("storage failed while caller was cancelled")

        svc.client.put_object.side_effect = put_object
        task = asyncio.create_task(svc.aio_put_bytes("first.png", b"first image"))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not svc.ready
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
