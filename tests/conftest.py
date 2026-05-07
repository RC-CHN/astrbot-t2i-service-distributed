"""Shared fixtures for the test suite.

IMPORTANT: Patches are applied at module level so they are active before
any src.* module is ever imported. This prevents StorageService from
trying to connect to a real boto3/S3 endpoint during test collection.
"""

import os
import sys

# ── Module-level pre-patches (before any src module import) ──
# These must be active before any test file imports src.*
from unittest.mock import MagicMock, patch

# Patch boto3.client globally so StorageService.__init__ doesn't try to
# connect to a real S3 endpoint.
_patch_boto3 = patch("boto3.client")
_mock_boto3_client = _patch_boto3.start()


# Ensure the src package is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest


@pytest.fixture
def mock_storage():
    """A fully mocked StorageService for API tests."""
    mock = MagicMock()
    return mock


@pytest.fixture
def mock_render():
    """A fully mocked Text2ImgRender for API tests."""
    mock = MagicMock()
    return mock


@pytest.fixture
def client(mock_storage, mock_render):
    """FastAPI TestClient with render and storage fully mocked."""
    with patch("src.api.storage_service", mock_storage):
        with patch("src.api.render", mock_render):
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
