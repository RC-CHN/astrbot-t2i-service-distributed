"""Tests for the rendering engine (Text2ImgRender and ScreenshotOptions)."""

import os
import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock


class TestScreenshotOptions:
    """Tests for the ScreenshotOptions Pydantic model."""

    def test_default_values(self):
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        assert opts.timeout is None
        assert opts.type is None
        assert opts.quality is None
        assert opts.omit_background is None
        assert opts.full_page is True
        assert opts.clip is None
        assert opts.animations is None
        assert opts.caret is None
        assert opts.scale is None
        assert opts.viewport_width is None
        assert opts.device_scale_factor_level is None

    def test_partial_override(self):
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(type="jpeg", quality=80, full_page=False)
        assert opts.type == "jpeg"
        assert opts.quality == 80
        assert opts.full_page is False
        # Other fields remain default
        assert opts.timeout is None

    def test_model_dump_exclude_none(self):
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(type="png", full_page=True)
        dumped = opts.model_dump(exclude_none=True)
        assert "type" in dumped
        assert "full_page" in dumped
        assert "timeout" not in dumped  # None → excluded
        assert "quality" not in dumped  # None → excluded

    def test_clip_field(self):
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(clip={"x": 0, "y": 0, "width": 100, "height": 200})
        assert opts.clip == {"x": 0, "y": 0, "width": 100, "height": 200}

    def test_device_scale_factor_level(self):
        from src.render import ScreenshotOptions

        for level in ("normal", "high", "ultra"):
            opts = ScreenshotOptions(device_scale_factor_level=level)
            assert opts.device_scale_factor_level == level


class TestRenderResolveViewport:
    """Tests for the _resolve_viewport_size method (returns (width, height) tuple)."""

    @pytest.fixture
    def render(self):
        from src.render import Text2ImgRender

        return Text2ImgRender()

    def test_explicit_viewport_takes_priority(self, render, tmp_path):
        """When viewport_width is explicitly set, use it directly."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(viewport_width=1024)
        html_file = tmp_path / "test.html"
        html_file.write_text("<html><head></head><body></body></html>")

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 1024
        assert height is None

    def test_auto_detect_from_meta_tag(self, render, tmp_path):
        """Should parse width from <meta name="viewport" content="width=XXX">."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="width=600"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 600
        assert height is None

    def test_auto_detect_with_extra_content(self, render, tmp_path):
        """Should parse width even with extra viewport content."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="width=800, initial-scale=1.0"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 800
        assert height is None

    def test_returns_none_when_no_meta_tag(self, render, tmp_path):
        """Should return None when no viewport meta tag is found."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text("<html><head></head><body></body></html>")

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width is None
        assert height is None

    def test_explicit_width_supersedes_meta(self, render, tmp_path):
        """Explicit viewport_width should be used even when meta tag exists."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(viewport_width=1200)
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="width=600"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 1200
        assert height is None

    def test_malformed_html_no_crash(self, render, tmp_path):
        """Should not crash on malformed HTML."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text("not even html")

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width is None
        assert height is None

    def test_file_not_found_no_crash(self, render):
        """Should not crash when file does not exist."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        width, height = render._resolve_viewport_size("/nonexistent/file.html", opts)
        assert width is None
        assert height is None

    def test_auto_detect_height_from_meta(self, render, tmp_path):
        """Should parse height from meta viewport content."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="height=900"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width is None
        assert height == 900

    def test_auto_detect_both_dimensions(self, render, tmp_path):
        """Should parse both width and height from meta viewport."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions()
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="width=640, height=480"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 640
        assert height == 480

    def test_explicit_both_skips_meta_parsing(self, render, tmp_path):
        """When both dimensions are explicit, skip meta parsing entirely."""
        from src.render import ScreenshotOptions

        opts = ScreenshotOptions(viewport_width=1920, viewport_height=1080)
        html_file = tmp_path / "test.html"
        html_file.write_text(
            '<html><head><meta name="viewport" content="width=640, height=480"></head><body></body></html>'
        )

        width, height = render._resolve_viewport_size(str(html_file), opts)
        assert width == 1920
        assert height == 1080


class TestRenderFromHtml:
    """Tests for the from_html method."""

    @pytest.fixture
    def render(self):
        from src.render import Text2ImgRender

        return Text2ImgRender()

    def test_generates_html_file(self, render, tmp_path):
        """Should write HTML to a file and return its paths."""
        # Override generate_data_path to use our temp dir
        with patch("src.render.generate_data_path") as mock_gen:
            rel = os.path.join(str(tmp_path), "data", "rendered_test.html")
            abs_path = os.path.abspath(rel)
            mock_gen.return_value = (rel, abs_path)

            # Ensure parent directory exists (generate_data_path is mocked)
            os.makedirs(os.path.dirname(rel), exist_ok=True)

            html_content = "<html><body>Test</body></html>"
            html_file_path, abs_out = asyncio.run(render.from_html(html_content))

            assert html_file_path == rel
            assert abs_out == abs_path
            assert os.path.exists(rel)
            with open(rel) as f:
                assert f.read() == html_content

    def test_overwrites_existing_file(self, render, tmp_path):
        """Should overwrite if file already exists."""
        with patch("src.render.generate_data_path") as mock_gen:
            rel = os.path.join(str(tmp_path), "data", "rendered_test.html")
            abs_path = os.path.abspath(rel)
            mock_gen.return_value = (rel, abs_path)

            # Write initial content
            os.makedirs(os.path.dirname(rel), exist_ok=True)
            with open(rel, "w") as f:
                f.write("old content")

            new_content = "<html><body>New</body></html>"
            asyncio.run(render.from_html(new_content))

            with open(rel) as f:
                assert f.read() == new_content


class TestRenderFromJinjaTemplate:
    """Tests for from_jinja_template method."""

    @pytest.fixture
    def render(self):
        from src.render import Text2ImgRender

        return Text2ImgRender()

    def test_renders_template_with_data(self, render, sample_template, tmp_path):
        """Should render Jinja2 template with provided data."""
        with patch("src.render.generate_data_path") as mock_gen:
            rel = os.path.join(str(tmp_path), "data", "rendered_test.html")
            abs_path = os.path.abspath(rel)
            mock_gen.return_value = (rel, abs_path)

            # Ensure parent directory exists (generate_data_path is mocked)
            os.makedirs(os.path.dirname(rel), exist_ok=True)

            data = {"title": "Test Title", "body": "Hello World"}
            html_file_path, abs_out = asyncio.run(
                render.from_jinja_template(sample_template, data)
            )

            with open(rel) as f:
                content = f.read()
            assert "<h1>Test Title</h1>" in content
            assert "<p>Hello World</p>" in content
            assert "{{ title }}" not in content
            assert "{{ body }}" not in content

    def test_handles_missing_data_keys(self, render, tmp_path):
        """Template rendering with missing keys should raise."""
        with patch("src.render.generate_data_path") as mock_gen:
            rel = os.path.join(str(tmp_path), "data", "rendered_test.html")
            mock_gen.return_value = (rel, os.path.abspath(rel))

            template = "<html><body>{{ missing_key }}</body></html>"
            with pytest.raises(Exception):  # Jinja2 UndefinedError
                asyncio.run(render.from_jinja_template(template, {}))


class TestRenderScaleFactorMap:
    """Tests for the scale factor mapping."""

    def test_scale_factor_mapping(self):
        from src.render import Text2ImgRender

        assert Text2ImgRender.SCALE_FACTOR_MAP["normal"] == 1.0
        assert Text2ImgRender.SCALE_FACTOR_MAP["high"] == 1.3
        assert Text2ImgRender.SCALE_FACTOR_MAP["ultra"] == 1.8
        assert len(Text2ImgRender.SCALE_FACTOR_MAP) == 3
