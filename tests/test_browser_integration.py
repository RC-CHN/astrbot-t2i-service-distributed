"""Real Chromium regression tests. Enable with RUN_BROWSER_TESTS=1."""
import asyncio
import base64
import os
import struct
import time
import zlib
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.config import Settings
from src.render import Text2ImgRender, ScreenshotOptions, RenderLimitError, RenderBusyError

pytestmark = pytest.mark.skipif(os.getenv("RUN_BROWSER_TESTS") != "1", reason="real Chromium tests are opt-in")


def png_size(image):
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", image[16:24])


def test_actual_screenshot_scale_long_page_and_limit_recovery():
    async def scenario():
        render = Text2ImgRender(Settings(_env_file=None, BROWSER_MAX_RENDERS=3))
        try:
            for level, expected in [("normal", (320, 240)), ("high", (416, 312)), ("ultra", (576, 432))]:
                image = await render.html2pic_bytes("<style>body{margin:0}</style><h1>你好 🌏</h1>",
                    ScreenshotOptions(viewport_width=320, viewport_height=240, device_scale_factor_level=level))
                assert png_size(image) == expected
            assert render.browser is None  # rotation completed only after contexts drained
            assert render.healthy
            image = await render.html2pic_bytes("<style>body{margin:0}</style><div style='height:12000px'>long chat</div>", ScreenshotOptions())
            assert png_size(image) == (800, 12000)
            with pytest.raises(RenderLimitError):
                await render.html2pic_bytes("<div style='height:1000000px'>too large</div>", ScreenshotOptions())
            assert png_size(await render.html2pic_bytes("healthy after rejection", ScreenshotOptions())) == (800, 720)
            assert render.browser is None or len(render.browser.contexts) == 0
        finally:
            await render.terminate()
    asyncio.run(scenario())


def test_stalled_image_and_polling_do_not_require_network_idle():
    async def scenario():
        release = asyncio.Event()
        handlers = set()
        async def serve(reader, writer):
            task = asyncio.current_task()
            handlers.add(task)
            try:
                request = await reader.readuntil(b"\r\n\r\n")
                if b"/stall" in request:
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 999999\r\n\r\n")
                    await writer.drain()
                    await release.wait()
                else:
                    writer.write(b"HTTP/1.1 200 OK\r\nAccess-Control-Allow-Origin: *\r\nContent-Length: 2\r\n\r\nok")
                    await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                handlers.discard(task)
        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        render = Text2ImgRender(Settings(_env_file=None, RENDER_ASSET_TIMEOUT_MS=250, RENDER_TIMEOUT=10))
        try:
            await render.start()
            html = f"""<h1>Visible despite slow external asset</h1><img src='http://127.0.0.1:{port}/stall'>
                <script>setInterval(() => fetch('http://127.0.0.1:{port}/poll'), 30);</script>"""
            started = time.monotonic()
            image = await render.html2pic_bytes(html, ScreenshotOptions())
            assert png_size(image) == (800, 720)
            assert time.monotonic() - started < 5
            assert render.browser is None or len(render.browser.contexts) == 0
        finally:
            release.set()
            server.close()
            await server.wait_closed()
            if handlers:
                await asyncio.gather(*list(handlers))
            await render.terminate()
    asyncio.run(scenario())


def test_browser_crash_recovers_and_burst_is_bounded():
    async def scenario():
        render = Text2ImgRender(Settings(_env_file=None, RENDER_CONCURRENCY=2, RENDER_QUEUE_SIZE=2))
        try:
            await render.start()
            await render.browser.close()
            results = await asyncio.gather(*(render.html2pic_bytes("<h1>burst</h1>", ScreenshotOptions()) for _ in range(10)), return_exceptions=True)
            assert sum(isinstance(result, bytes) for result in results) == 4
            assert sum(isinstance(result, RenderBusyError) for result in results) == 6
            assert render._pending == 0 and render._active == 0
            assert render.browser is None or len(render.browser.contexts) == 0
        finally:
            await render.terminate()
    asyncio.run(scenario())


@pytest.fixture(autouse=True)
def browser_executable_override(monkeypatch):
    """Optional locally cached Chromium override; release images use bundled browser."""
    executable = os.getenv("TEST_BROWSER_EXECUTABLE")
    if executable:
        from playwright.async_api import BrowserType
        original = BrowserType.launch
        async def launch(self, *args, **kwargs):
            kwargs["executable_path"] = executable
            return await original(self, *args, **kwargs)
        monkeypatch.setattr(BrowserType, "launch", launch)


def test_script_hang_times_out_and_next_request_recovers():
    from src.render import RenderTimeoutError
    async def scenario():
        render = Text2ImgRender(Settings(_env_file=None, RENDER_TIMEOUT=1))
        try:
            await render.start()
            with pytest.raises(RenderTimeoutError):
                await render.html2pic_bytes("<script>while(true){}</script>", ScreenshotOptions())
            assert render._active == 0 and render._pending == 0
            image = await render.html2pic_bytes("healthy after script timeout", ScreenshotOptions())
            assert png_size(image) == (800, 720)
        finally:
            await render.terminate()
    asyncio.run(scenario())


def test_http_accepts_large_valid_embedded_data_uri():
    import src.api as api
    def chunk(kind, content):
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", zlib.crc32(kind + content))
    # A valid 1x1 PNG with a large ancillary text chunk reproduces legitimate
    # base64-heavy templates without retaining a multi-megapixel decoded image.
    png = (b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"tEXt", b"Comment\x00" + b"x" * (5 * 1024 * 1024))
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b""))
    html = '<img src="data:image/png;base64,' + base64.b64encode(png).decode() + '">'
    assert len(html) > 6 * 1024 * 1024
    async def scenario():
        render = Text2ImgRender(Settings(_env_file=None))
        try:
            with patch.object(api, "render", render), patch.object(api, "_persist_image", AsyncMock(return_value=True)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                    response = await client.post("/text2img/generate", json={"html": html,
                        "options": {"viewport_width": 320, "viewport_height": 240}})
            assert response.status_code == 200, response.text[:200] if response.status_code != 200 else ""
            assert png_size(response.content) == (320, 240)
        finally:
            await render.terminate()
    asyncio.run(scenario())
