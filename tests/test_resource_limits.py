import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.config import Settings
from src.render import (Text2ImgRender, ScreenshotOptions, RenderLimitError,
                        RenderBusyError, RenderTimeoutError)


def config(**kwargs):
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("html,options", [
    ("hello" * 30, ScreenshotOptions()),
    ("<meta name='viewport' content='width=9000'>", ScreenshotOptions()),
    ("hi", ScreenshotOptions(viewport_width=2049)),
    ("hi", ScreenshotOptions(clip={"x": 0, "y": 0, "width": 4000, "height": 5000})),
    ("hi", ScreenshotOptions(clip={"x": -1, "y": 0, "width": 100, "height": 100})),
])
def test_rejects_unsafe_input_before_browser_launch(html, options):
    render = Text2ImgRender(config(RENDER_MAX_HTML_BYTES=100))
    with pytest.raises(RenderLimitError):
        asyncio.run(render.html2pic_bytes(html, options))
    assert render.browser is None


def test_template_output_is_bounded():
    render = Text2ImgRender(config(RENDER_MAX_HTML_BYTES=100))
    with pytest.raises(RenderLimitError):
        render.render_template("{% for i in range(1000) %}long output{% endfor %}", {})


def test_device_scale_counts_against_pixel_budget():
    render = Text2ImgRender(config(RENDER_MAX_PIXELS=1000000))
    render._validate_dimensions(800, 1000, 1)
    with pytest.raises(RenderLimitError):
        render._validate_dimensions(800, 1000, 1.8)


@pytest.mark.parametrize("width,height,budget", [
    (800, 20000, "screenshot_dimension"),
    (2000, 10000, "screenshot_pixels"),
])
def test_dimension_errors_identify_budget_and_safe_numeric_details(width, height, budget):
    render = Text2ImgRender(config())
    with pytest.raises(RenderLimitError) as exc:
        render._validate_dimensions(width, height, 1)
    assert exc.value.details["budget"] == budget
    assert exc.value.details["physical_width"] == width
    assert exc.value.details["physical_height"] == height


def test_client_cannot_disable_timeout():
    render = Text2ImgRender(config())
    assert render._screenshot_kwargs(ScreenshotOptions(timeout=0))["timeout"] == 10000
    assert render._screenshot_kwargs(ScreenshotOptions(timeout=999999))["timeout"] == 10000
    assert render._screenshot_kwargs(ScreenshotOptions(timeout=100))["timeout"] == 100


def test_queue_is_bounded_and_recovers_after_cancellation():
    async def scenario():
        render = Text2ImgRender(config(RENDER_CONCURRENCY=1, RENDER_QUEUE_SIZE=1))
        entered, release = asyncio.Event(), asyncio.Event()
        async def capture(*args):
            entered.set()
            await release.wait()
            return b"png"
        render._capture = capture
        first = asyncio.create_task(render.html2pic_bytes("hi", ScreenshotOptions()))
        await entered.wait()
        second = asyncio.create_task(render.html2pic_bytes("hi", ScreenshotOptions()))
        await asyncio.sleep(0)
        with pytest.raises(RenderBusyError):
            await render.html2pic_bytes("hi", ScreenshotOptions())
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        assert await first == b"png"
        assert render._pending == 0
        assert await render.html2pic_bytes("hi", ScreenshotOptions()) == b"png"
    asyncio.run(scenario())


def test_queue_wait_has_deadline():
    async def scenario():
        render = Text2ImgRender(config(RENDER_CONCURRENCY=1, RENDER_QUEUE_TIMEOUT=0.01))
        await render._slots.acquire()
        with pytest.raises(RenderBusyError):
            await render.html2pic_bytes("hi", ScreenshotOptions())
        assert render._pending == 0
        render._slots.release()
    asyncio.run(scenario())


def fake_driver():
    page = MagicMock()
    page.set_content = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.evaluate = AsyncMock(side_effect=lambda script, *args:
        {"width": 800, "height": 720} if "scrollWidth" in script else True)
    page.screenshot = AsyncMock(return_value=b"png")
    context = MagicMock()
    context.route = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.is_connected.return_value = True
    browser.close = AsyncMock()
    driver = MagicMock()
    driver.chromium.launch = AsyncMock(return_value=browser)
    driver.stop = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=driver)
    return starter, driver, browser, context, page


def test_concurrent_first_renders_launch_one_browser_and_close_all_contexts():
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        render = Text2ImgRender(config())
        with patch("src.render.async_playwright", return_value=starter):
            result = await asyncio.gather(*(render.html2pic_bytes("hi", ScreenshotOptions()) for _ in range(4)))
        assert result == [b"png"] * 4
        driver.chromium.launch.assert_awaited_once()
        assert browser.new_context.await_count == 4
        assert context.close.await_count == 4
        assert render._active == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("failure_stage", ["new_page", "set_content", "screenshot"])
def test_context_cleanup_covers_setup_and_capture_errors(failure_stage):
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        target = context if failure_stage == "new_page" else page
        getattr(target, failure_stage).side_effect = RuntimeError("injected failure")
        render = Text2ImgRender(config())
        with patch("src.render.async_playwright", return_value=starter):
            with pytest.raises(RuntimeError):
                await render.html2pic_bytes("hi", ScreenshotOptions())
        context.close.assert_awaited_once()
        assert render._active == 0 and render._pending == 0
    asyncio.run(scenario())


def test_total_deadline_cleans_context():
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        async def stuck(**kwargs):
            await asyncio.sleep(100)
        page.screenshot.side_effect = stuck
        render = Text2ImgRender(config(RENDER_TIMEOUT=0.02))
        with patch("src.render.async_playwright", return_value=starter):
            with pytest.raises(RenderTimeoutError):
                await render.html2pic_bytes("hi", ScreenshotOptions())
        context.close.assert_awaited_once()
        assert render._active == 0 and render._pending == 0
    asyncio.run(scenario())


def test_browser_rotation_preserves_readiness_and_restarts():
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        render = Text2ImgRender(config(BROWSER_MAX_RENDERS=1))
        with patch("src.render.async_playwright", return_value=starter):
            await render.html2pic_bytes("hi", ScreenshotOptions())
            assert render.browser is None
            assert render.healthy
            await render.html2pic_bytes("hi", ScreenshotOptions())
        assert driver.chromium.launch.await_count == 2
        assert browser.close.await_count == 2
    asyncio.run(scenario())


def test_http_admission_precedes_body_parsing_and_bounds_memory():
    from src.limits import RequestBudgetMiddleware
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def downstream(scope, receive, send):
            entered.set()
            await release.wait()
            from starlette.responses import Response
            await Response("ok")(scope, receive, send)
        app = RequestBudgetMiddleware(downstream, config(RENDER_CONCURRENCY=1, RENDER_QUEUE_SIZE=0, RENDER_MAX_REQUEST_BYTES=10))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/text2img/generate", content=b"x" * 11)
            assert response.status_code == 413
            first = asyncio.create_task(client.post("/text2img/generate", content=b"hi"))
            await entered.wait()
            response = await client.post("/text2img/generate", content=b"hi")
            assert response.status_code == 429
            assert response.headers["retry-after"] == "2"
            release.set()
            assert (await first).status_code == 200
            assert app.active == 0
    asyncio.run(scenario())


def test_deadline_during_cleanup_does_not_leak_active_slot():
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        closed = asyncio.Event()
        async def slow_close():
            await asyncio.sleep(0.04)
            closed.set()
        context.close.side_effect = slow_close
        render = Text2ImgRender(config(RENDER_TIMEOUT=0.02))
        with patch("src.render.async_playwright", return_value=starter):
            with pytest.raises(RenderTimeoutError):
                await render.html2pic_bytes("hi", ScreenshotOptions())
        assert closed.is_set()
        assert render._active == 0 and render._pending == 0
    asyncio.run(scenario())


def test_failed_browser_close_stops_driver_before_recovery():
    async def scenario():
        starter, driver, browser, context, page = fake_driver()
        browser.close.side_effect = RuntimeError("browser stuck")
        render = Text2ImgRender(config(BROWSER_MAX_RENDERS=1))
        with patch("src.render.async_playwright", return_value=starter):
            await render.html2pic_bytes("hi", ScreenshotOptions())
            assert render.playwright is None and render.browser is None
            driver.stop.assert_awaited_once()
            await render.html2pic_bytes("hi", ScreenshotOptions())
            assert starter.start.await_count == 2
    asyncio.run(scenario())


def test_download_budget_is_independent_from_render_and_health_requests():
    from src.limits import RequestBudgetMiddleware
    from starlette.responses import Response
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def downstream(scope, receive, send):
            if scope["path"].startswith("/text2img/data/"):
                entered.set()
                await release.wait()
            await Response("ok")(scope, receive, send)
        app = RequestBudgetMiddleware(downstream, config(IMAGE_DOWNLOAD_CONCURRENCY=1))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.get("/text2img/data/one.png"))
            await entered.wait()
            response = await client.get("/text2img/data/two.png")
            assert response.status_code == 429
            assert response.headers["retry-after"] == "2"
            assert (await client.get("/healthz")).status_code == 200
            assert (await client.post("/text2img/generate", content=b"hi")).status_code == 200
            release.set()
            assert (await first).status_code == 200
            assert (await client.get("/text2img/data/two.png")).status_code == 200
            assert app.downloads == 0
    asyncio.run(scenario())


def test_cancelled_download_keeps_admission_until_reader_closes():
    import threading
    import src.api as api
    from src.limits import RequestBudgetMiddleware
    from starlette.responses import Response
    release = threading.Event()
    async def scenario():
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()
        def download(key):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "test did not release download thread"
            return b"image"
        async def downstream(scope, receive, send):
            response = await api.text2img_image("one.png")
            await response(scope, receive, send)
        app = RequestBudgetMiddleware(downstream, config(IMAGE_DOWNLOAD_CONCURRENCY=1))
        with patch.object(api.cache, "get", AsyncMock(return_value=None)), \
             patch.object(api, "_download_image", side_effect=download):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                first = asyncio.create_task(client.get("/text2img/data/one.png"))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    first.cancel()
                    await asyncio.sleep(0)
                    first.cancel()
                    await asyncio.sleep(0)
                    assert not first.done()
                    assert (await client.get("/text2img/data/two.png")).status_code == 429
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await first
                    assert app.downloads == 0
                finally:
                    release.set()
                    await asyncio.gather(first, return_exceptions=True)
    asyncio.run(scenario())
