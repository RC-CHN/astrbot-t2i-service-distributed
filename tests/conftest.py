"""Shared fixtures for the test suite.

IMPORTANT: Patches are applied at module level so they are active before
any src.* module is ever imported. This prevents StorageService from
trying to connect to a real boto3/S3 endpoint during test collection.
"""

import os
import sys

# ── Module-level pre-patches (before any src module import) ──
# These must be active before any test file imports src.*
from unittest.mock import MagicMock, patch, AsyncMock

# Patch boto3.client globally so StorageService.__init__ doesn't try to
# connect to a real S3 endpoint.
_patch_boto3 = patch("boto3.client")
_mock_boto3_client = _patch_boto3.start()


# Ensure the src package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import tempfile


@pytest.fixture
def mock_cache():
    """A fully mocked RedisImageCache for API tests."""
    mock = MagicMock()
    mock.get = AsyncMock(return_value=None)  # cache miss by default
    mock.set = AsyncMock(return_value=True)
    return mock


@pytest.fixture
def mock_storage():
    """A fully mocked StorageService for API tests."""
    mock = MagicMock()
    mock.download_stream = MagicMock(return_value=None)
    mock.aio_upload = AsyncMock(return_value=True)
    return mock


def _make_fake_render():
    """Return a mock render engine that creates actual temp files.

    The POST handler now reads rendered images back from disk (for Redis
    caching), so the paths returned by html2pic / from_html must point to
    real files.
    """
    mock = MagicMock()

    async def _from_html(html: str):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False) as f:
            f.write(html)
            rel = f.name
        return rel, rel

    async def _from_jinja(tmpl: str, data: dict):
        from jinja2.sandbox import SandboxedEnvironment
        html = SandboxedEnvironment().from_string(tmpl).render(data)
        return await _from_html(html)

    async def _html2pic(html_path: str, options):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")
            return f.name

    mock.from_html = AsyncMock(side_effect=_from_html)
    mock.from_jinja_template = AsyncMock(side_effect=_from_jinja)
    mock.html2pic = AsyncMock(side_effect=_html2pic)
    return mock


@pytest.fixture
def mock_render():
    """A fully mocked Text2ImgRender that creates real temp files."""
    return _make_fake_render()


@pytest.fixture
def client(mock_storage, mock_render, mock_cache):
    """FastAPI TestClient with render, storage, and cache fully mocked."""
    with patch("src.api.storage_service", mock_storage), \
         patch("src.api.render", mock_render), \
         patch("src.api.cache", mock_cache):
        from src.api import app
        from fastapi.testclient import TestClient

        yield TestClient(app)


@pytest.fixture
def sample_template():
    return """<html>
<head><meta name="viewport" content="width=600"></head>
<body><h1>{{ title }}</h1><p>{{ body }}</p></body>
</html>"""


@pytest.fixture
def sample_html():
    return """<html>
<head><meta name="viewport" content="width=800"></head>
<body><h1>Hello</h1></body>
</html>"""
