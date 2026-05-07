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


class TestPostGenerate:
    """Tests for POST /text2img/generate."""

    def test_generate_from_html_json_mode(self, client, mock_render, mock_storage):
        mock_render.from_html = AsyncMock(return_value=(
            "data/rendered_test.html", "/abs/path/rendered_test.html"
        ))
        mock_render.html2pic = AsyncMock(return_value="data/rendered/test-id.png")

        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["code"] == 0
        assert body["data"]["id"] == "data/rendered/test-id.png"
        mock_storage.upload.assert_called_once()

    def test_generate_from_template(self, client, mock_render, mock_storage):
        mock_render.from_jinja_template = AsyncMock(return_value=(
            "data/rendered_test.html", "/abs/path/rendered_test.html"
        ))
        mock_render.html2pic = AsyncMock(return_value="data/rendered/tpl-id.png")

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
        assert body["data"]["id"] == "data/rendered/tpl-id.png"

    def test_generate_missing_html_and_tmpl(self, client):
        response = client.post("/text2img/generate", json={"json": True})

        assert response.status_code == 400
        body = response.json()
        assert body["code"] == 1

    def test_generate_with_custom_options(self, client, mock_render, mock_storage):
        mock_render.from_html = AsyncMock(return_value=(
            "data/rendered_test.html", "/abs/path/rendered_test.html"
        ))
        mock_render.html2pic = AsyncMock(return_value="data/rendered/opt-id.jpg")

        response = client.post(
            "/text2img/generate",
            json={
                "html": "<h1>Hello</h1>",
                "json": True,
                "options": {"type": "jpeg", "quality": 85, "full_page": True},
            },
        )

        assert response.status_code == 200
        call_args = mock_render.html2pic.call_args
        passed_options = call_args[0][1]
        assert passed_options.type == "jpeg"
        assert passed_options.quality == 85
        assert passed_options.full_page is True

    def test_generate_default_options_when_none(self, client, mock_render, mock_storage):
        mock_render.from_html = AsyncMock(return_value=(
            "data/rendered_test.html", "/abs/path/rendered_test.html"
        ))
        mock_render.html2pic = AsyncMock(return_value="data/rendered/default.png")

        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 200
        call_args = mock_render.html2pic.call_args
        passed_options = call_args[0][1]
        assert passed_options.type == "png"
        assert passed_options.full_page is True
        assert passed_options.scale == "device"

    def test_generate_handles_render_error(self, client, mock_render):
        mock_render.from_html = AsyncMock(side_effect=ValueError("Something broke"))

        response = client.post(
            "/text2img/generate",
            json={"html": "<h1>Hello</h1>", "json": True},
        )

        assert response.status_code == 500
        body = response.json()
        assert body["code"] == 1
