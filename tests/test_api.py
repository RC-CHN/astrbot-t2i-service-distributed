"""Tests for the FastAPI endpoints."""

import os
import io
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestGetImage:
    """Tests for GET /text2img/data/{image_path}."""

    def test_returns_image_stream_when_found(self, client, mock_storage):
        fake_stream = io.BytesIO(b"fake-png-data")
        mock_storage.download_stream.return_value = fake_stream

        response = client.get("/text2img/data/rendered/test-id.png")

        assert response.status_code == 200
        assert response.content == b"fake-png-data"
        assert response.headers["content-type"] == "image/png"
        mock_storage.download_stream.assert_called_once_with(
            "data/rendered/test-id.png"
        )

    def test_returns_404_when_not_found(self, client, mock_storage):
        mock_storage.download_stream.return_value = None

        response = client.get("/text2img/data/rendered/missing.png")

        assert response.status_code == 404
        body = response.json()
        assert body["code"] == 1

    def test_normalizes_data_prefix(self, client, mock_storage):
        """Should strip duplicate 'data/' prefix from path."""
        fake_stream = io.BytesIO(b"fake-png-data")
        mock_storage.download_stream.return_value = fake_stream

        response = client.get("/text2img/data/data/rendered/test-id.png")

        assert response.status_code == 200
        mock_storage.download_stream.assert_called_once_with(
            "data/rendered/test-id.png"
        )

    def test_jpeg_media_type(self, client, mock_storage):
        fake_stream = io.BytesIO(b"fake-jpeg-data")
        mock_storage.download_stream.return_value = fake_stream

        response = client.get("/text2img/data/rendered/photo.jpg")

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"

    def test_handles_storage_error(self, client, mock_storage):
        mock_storage.download_stream.side_effect = RuntimeError("Storage down")

        response = client.get("/text2img/data/rendered/error.png")

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == 1

    @pytest.mark.parametrize("backend", ["redis", "s3"])
    def test_rejects_oversized_stored_images(self, client, mock_storage, mock_cache, backend):
        from src.api import settings
        stream = io.BytesIO(b"123456789")
        if backend == "redis":
            mock_cache.get.return_value = b"123456789"
        else:
            mock_storage.download_stream.return_value = stream
        with patch.object(settings, "RENDER_MAX_IMAGE_BYTES", 8):
            response = client.get("/text2img/data/rendered/large.png")
        assert response.status_code == 413
        if backend == "s3":
            assert stream.closed

    def test_returns_exactly_budget_sized_cached_image(self, client, mock_cache):
        from src.api import settings
        mock_cache.get.return_value = b"12345678"
        with patch.object(settings, "RENDER_MAX_IMAGE_BYTES", 8):
            response = client.get("/text2img/data/rendered/image.png")
        assert response.status_code == 200
        assert response.content == b"12345678"


class TestPostGenerate:
    """Tests for POST /text2img/generate."""

    def test_generate_from_html_json_mode(self, client, mock_render, mock_storage):
        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0
        assert "id" in body["data"]
        mock_render.html2pic_bytes.assert_called_once()

    def test_generate_from_template(self, client, mock_render, mock_storage):
        response = client.post(
            "/text2img/generate",
            json={
                "tmpl": "<html>{{ name }}</html>",
                "tmpldata": {"name": "World"},
                "json": True,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0
        assert "id" in body["data"]

    def test_generate_missing_html_and_tmpl(self, client):
        response = client.post("/text2img/generate", json={"json": True})
        assert response.status_code == 400
        assert response.json()["code"] == 1

    def test_generate_with_custom_options(self, client, mock_render, mock_storage):
        response = client.post(
            "/text2img/generate",
            json={
                "html": "<h1>Hello</h1>",
                "json": True,
                "options": {"type": "jpeg", "quality": 85, "full_page": True},
            },
        )

        assert response.status_code == 200
        call_args = mock_render.html2pic_bytes.call_args
        passed_options = call_args[0][1]
        assert passed_options.type == "jpeg"
        assert passed_options.quality == 85
        assert passed_options.full_page is True

    def test_generate_default_options_when_none(self, client, mock_render, mock_storage):
        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 200
        call_args = mock_render.html2pic_bytes.call_args
        passed_options = call_args[0][1]
        assert passed_options.type == "png"
        assert passed_options.full_page is True
        assert passed_options.scale == "device"

    def test_generate_handles_render_error(self, client, mock_render):
        mock_render.html2pic_bytes = AsyncMock(side_effect=ValueError("Boom"))

        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 500
        assert response.json()["code"] == 1


@pytest.mark.parametrize("error,status", [
    ("RenderLimitError", 413), ("RenderBusyError", 429), ("RenderTimeoutError", 504),
])
def test_expected_render_failures_have_actionable_status(client, mock_render, error, status):
    import src.render
    mock_render.html2pic_bytes.side_effect = getattr(src.render, error)("budget exceeded")
    response = client.post("/text2img/generate", json={"html": "hi", "json": True})
    assert response.status_code == status
    assert response.json()["code"] == 1


def test_rejection_log_reports_safe_budget_without_html(client, mock_render):
    import json
    from src.render import RenderLimitError
    mock_render.html2pic_bytes.side_effect = RenderLimitError("HTML exceeds configured byte limit",
        budget="html_bytes", actual=100, limit=50)
    with patch("src.limits.logger.warning") as log:
        assert client.post("/text2img/generate", json={"html": "private-template-text"}).status_code == 413
    record = json.loads(log.call_args.args[0])
    assert record["budget"] == "html_bytes"
    assert record["actual"] == 100 and record["limit"] == 50
    assert "private-template-text" not in log.call_args.args[0]


def test_json_does_not_return_id_when_durable_storage_fails(client, mock_storage, mock_cache):
    mock_storage.aio_put_bytes.return_value = False
    mock_cache.set.return_value = True
    response = client.post("/text2img/generate", json={"html": "hi", "json": True})
    assert response.status_code == 503
    assert "id" not in response.json()["data"]


def test_binary_response_never_creates_temporary_file(client, mock_render):
    response = client.post("/text2img/generate", json={"html": "hi"})
    assert response.status_code == 200
    assert response.content.startswith(b"\x89PNG")
    assert response.headers["content-type"] == "image/png"
    mock_render.html2pic_file.assert_not_called()


def test_health_is_live_even_without_dependencies(client, mock_render, mock_storage, mock_cache):
    mock_render.healthy = True
    mock_storage.ready = False
    mock_cache.healthy = False
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 503
    mock_cache.healthy = True
    assert client.get("/readyz").status_code == 200


def test_generate_accepts_as_json_field_name(client):
    response = client.post("/text2img/generate", json={"html": "hello", "as_json": True})
    assert response.status_code == 200
    assert response.json()["data"]["id"]


def test_dependency_monitor_recovers_renderer_without_incoming_requests(mock_render, mock_storage, mock_cache):
    import asyncio
    from src.api import _monitor_dependencies
    mock_render.start = AsyncMock(side_effect=[RuntimeError("driver unavailable"), None])
    mock_storage.ensure_ready = AsyncMock(return_value=True)
    mock_cache.check = AsyncMock(return_value=True)
    async def scenario():
        with patch("src.api.render", mock_render), patch("src.api.storage_service", mock_storage), \
             patch("src.api.cache", mock_cache), \
             patch("src.api.asyncio.sleep", AsyncMock(side_effect=[None, None, asyncio.CancelledError])):
            with pytest.raises(asyncio.CancelledError):
                await _monitor_dependencies()
        assert mock_render.start.await_count == 2
        assert mock_cache.check.await_count == 2
    asyncio.run(scenario())
