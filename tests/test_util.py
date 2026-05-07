"""Tests for the utility module."""

import os
import time
import pytest
from unittest.mock import patch


class TestGenerateDataPath:
    """Tests for generate_data_path function."""

    def test_returns_relative_and_absolute_paths(self, tmp_path):
        from src.util import generate_data_path

        with patch("src.util.os.makedirs"):
            with patch("src.util.time.time", return_value=1678886400):
                with patch("src.util.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value.hex = "abcdef1234567890"
                    rel, abs_path = generate_data_path(
                        suffix="html", namespace="rendered"
                    )

        assert rel.startswith("data/")
        assert "rendered_" in rel
        assert rel.endswith(".html")
        assert os.path.isabs(abs_path)
        # The abs_path should end with the same filename as rel
        assert abs_path.endswith(rel.replace("data/", ""))

    def test_default_namespace(self):
        from src.util import generate_data_path

        with patch("src.util.os.makedirs"):
            with patch("src.util.time.time", return_value=1000):
                with patch("src.util.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value.hex = "deadbeef00000000"
                    rel, _ = generate_data_path()

        assert "default_" in rel

    def test_jpeg_suffix(self):
        from src.util import generate_data_path

        with patch("src.util.os.makedirs"):
            with patch("src.util.time.time", return_value=1000):
                with patch("src.util.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value.hex = "aabbccdd11223344"
                    rel, _ = generate_data_path(suffix="jpeg")

        assert rel.endswith(".jpeg")


class TestGetImageLifetime:
    """Tests for get_image_lifetime function."""

    @pytest.fixture(autouse=True)
    def clean_env(self, monkeypatch):
        monkeypatch.delenv("IMAGE_LIFETIME_HOURS", raising=False)

    def test_default_24_hours(self):
        from src.util import get_image_lifetime

        lifetime = get_image_lifetime()
        assert lifetime == 24 * 3600  # 86400 seconds

    def test_custom_hours(self, monkeypatch):
        monkeypatch.setenv("IMAGE_LIFETIME_HOURS", "48")
        from src.util import get_image_lifetime

        lifetime = get_image_lifetime()
        assert lifetime == 48 * 3600

    def test_invalid_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("IMAGE_LIFETIME_HOURS", "not-a-number")
        from src.util import get_image_lifetime

        lifetime = get_image_lifetime()
        assert lifetime == 24 * 3600

    def test_zero_hours(self, monkeypatch):
        monkeypatch.setenv("IMAGE_LIFETIME_HOURS", "0")
        from src.util import get_image_lifetime

        lifetime = get_image_lifetime()
        assert lifetime == 0


class TestCleanupExpiredFiles:
    """Tests for cleanup_expired_files function."""

    def test_no_files_to_clean(self, tmp_path):
        from src.util import cleanup_expired_files

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        count = cleanup_expired_files(str(data_dir), lifetime_seconds=3600)
        assert count == 0

    def test_cleans_only_expired_files(self, tmp_path):
        from src.util import cleanup_expired_files

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        old_file = data_dir / "old.png"
        old_file.write_text("old")
        # Set mtime to 2 hours ago
        old_mtime = time.time() - 7200
        os.utime(str(old_file), (old_mtime, old_mtime))

        new_file = data_dir / "new.png"
        new_file.write_text("new")
        # new file has current mtime

        count = cleanup_expired_files(str(data_dir), lifetime_seconds=3600)
        assert count == 1
        assert not old_file.exists()
        assert new_file.exists()

    def test_nonexistent_dir_returns_zero(self):
        from src.util import cleanup_expired_files

        count = cleanup_expired_files("/nonexistent/path")
        assert count == 0

    def test_respects_custom_lifetime(self, tmp_path):
        from src.util import cleanup_expired_files

        data_dir = tmp_path / "data"
        data_dir.mkdir()

        file_30min_ago = data_dir / "recent.png"
        file_30min_ago.write_text("recent")
        mtime = time.time() - 1800
        os.utime(str(file_30min_ago), (mtime, mtime))

        # With 1 hour lifetime, this file should NOT be cleaned
        count = cleanup_expired_files(str(data_dir), lifetime_seconds=3600)
        assert count == 0
        assert file_30min_ago.exists()

        # With 10 minute lifetime, this file SHOULD be cleaned
        count = cleanup_expired_files(str(data_dir), lifetime_seconds=600)
        assert count == 1
        assert not file_30min_ago.exists()

    def test_skips_directories(self, tmp_path):
        from src.util import cleanup_expired_files

        data_dir = tmp_path / "data"
        data_dir.mkdir()
        subdir = data_dir / "subdir"
        subdir.mkdir()

        count = cleanup_expired_files(str(data_dir), lifetime_seconds=1)
        assert count == 0  # Directories are not counted
        assert subdir.exists()  # Directory is not deleted
