import asyncio
import math
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from typing_extensions import TypedDict

from jinja2.sandbox import SandboxedEnvironment
from loguru import logger
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright, Browser, Playwright, TimeoutError as PlaywrightTimeout

from .config import settings
from .util import generate_data_path


class RenderError(Exception):
    status_code = 500

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class RenderLimitError(RenderError):
    status_code = 413


class RenderBusyError(RenderError):
    status_code = 429


class RenderTimeoutError(RenderError):
    status_code = 504


class FloatRect(TypedDict):
    x: float
    y: float
    width: float
    height: float


class ScreenshotOptions(BaseModel):
    """Playwright 截图参数

    详见：https://playwright.dev/python/docs/api/class-page#page-screenshot

    Args:
        timeout (float, optional): 截图超时时间.
        type (Literal["jpeg", "png"], optional): 截图图片类型.
        path (Union[str, Path]], optional): 截图保存路径，如不需要则留空.
        quality (int, optional): 截图质量，仅适用于 JPEG 格式图片.
        omit_background (bool, optional): 是否允许隐藏默认的白色背景，这样就可以截透明图了，仅适用于 PNG 格式.
        full_page (bool, optional): 是否截整个页面而不是仅设置的视口大小，默认为 True.
        clip (FloatRect, optional): 截图后裁切的区域，xy为起点.
        animations: (Literal["allow", "disabled"], optional): 是否允许播放 CSS 动画.
        caret: (Literal["hide", "initial"], optional): 当设置为 `hide` 时，截图时将隐藏文本插入符号，默认为 `hide`.
        scale: (Literal["css", "device"], optional): 页面缩放设置.
            当设置为 `css` 时，则将设备分辨率与 CSS 中的像素一一对应，在高分屏上会使得截图变小.
            当设置为 `device` 时，则根据设备的屏幕缩放设置或当前 Playwright 的 Page/Context 中的
            device_scale_factor 参数来缩放.
        viewport_width: (int, optional): 自定义视口宽度，用于控制截图宽度.
            优先级：
            1. 显式指定此参数；
            2. 从 HTML 的 <meta name="viewport" content="width=..."> 自动解析；
            3. 未指定时默认为 800px.
        viewport_height: (int, optional): 自定义视口高度，用于控制截图高度.
            优先级：
            1. 显式指定此参数；
            2. 从 HTML 的 <meta name="viewport" content="height=..."> 自动解析；
            3. 未指定时默认为 720px.
        device_scale_factor_level: (Literal["normal", "high", "ultra"], optional): 设备像素比等级.
            - normal: 1.0
            - high: 1.3
            - ultra: 1.8

    @author: Redlnn(https://github.com/GraiaCommunity/graiax-text2img-playwright)
    """

    timeout: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    type: Literal["jpeg", "png", None] = None
    quality: int | None = Field(default=None, ge=0, le=100)
    omit_background: bool | None = None
    full_page: bool | None = True
    clip: FloatRect | None = None
    animations: Literal["allow", "disabled", None] = None
    caret: Literal["hide", "initial", None] = None
    scale: Literal["css", "device", None] = None
    viewport_width: int | None = Field(default=None, gt=0)
    viewport_height: int | None = Field(default=None, gt=0)
    device_scale_factor_level: Literal["normal", "high", "ultra", None] = None


class Text2ImgRender:
    SCALE_FACTOR_MAP = {"normal": 1.0, "high": 1.3, "ultra": 1.8}

    def __init__(self, config=None):
        self.config = config or settings
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(self.config.RENDER_CONCURRENCY)
        self._pending = 0
        self._active = 0
        self._renders = 0
        self._started = 0.0
        self._stopping = False
        self._recycle_pending = False

    @property
    def healthy(self):
        # Disconnected/recycled browsers recover on the next request. Readiness must
        # not strand an idle worker after routine rotation.
        return not self._stopping and self.playwright is not None

    @asynccontextmanager
    async def _admit(self):
        # No await between check and increment: admission is atomic on the event loop.
        if self._stopping or self._pending >= self.config.RENDER_CONCURRENCY + self.config.RENDER_QUEUE_SIZE:
            raise RenderBusyError("renderer busy; retry later")
        self._pending += 1
        acquired = False
        try:
            try:
                await asyncio.wait_for(self._slots.acquire(), self.config.RENDER_QUEUE_TIMEOUT)
                acquired = True
            except TimeoutError as exc:
                raise RenderBusyError("render queue wait exceeded") from exc
            yield
        finally:
            if acquired:
                self._slots.release()
            self._pending -= 1

    async def _close_browser_locked(self):
        browser, self.browser = self.browser, None
        if browser is not None:
            try:
                await asyncio.wait_for(browser.close(), 5)
            except Exception as exc:
                logger.warning("Browser close failed; stopping driver: {}", exc)
                await self._stop_driver_locked()
        self._renders = 0
        self._recycle_pending = False

    async def _stop_driver_locked(self):
        driver, self.playwright = self.playwright, None
        if driver is not None:
            try:
                await asyncio.wait_for(driver.stop(), 5)
            except Exception as exc:
                logger.warning("Playwright driver shutdown failed: {}", exc)

    async def _ensure_browser_locked(self):
        if self._stopping:
            raise RenderBusyError("renderer shutting down")
        if self.browser is None or not self.browser.is_connected():
            await self._close_browser_locked()
            if self.playwright is None:
                self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.launch(headless=True)
            except BaseException:
                await self._stop_driver_locked()
                raise
            self._started = time.monotonic()

    async def start(self):
        async with self._lock:
            await self._ensure_browser_locked()

    async def terminate(self):
        async with self._lock:
            self._stopping = True
            await self._close_browser_locked()
            await self._stop_driver_locked()

    def _validate_html(self, html):
        size = len(html.encode("utf-8"))
        if size > self.config.RENDER_MAX_HTML_BYTES:
            raise RenderLimitError("HTML exceeds configured byte limit",
                budget="html_bytes", actual=size, limit=self.config.RENDER_MAX_HTML_BYTES)

    def render_template(self, template: str, data: dict) -> str:
        self._validate_html(template)
        # Stream output so repeated template blocks cannot build an unbounded result.
        chunks, size = [], 0
        for chunk in SandboxedEnvironment().from_string(template).generate(data):
            size += len(chunk.encode("utf-8"))
            if size > self.config.RENDER_MAX_HTML_BYTES:
                raise RenderLimitError("rendered template exceeds HTML byte limit",
                    budget="template_bytes", actual=size, limit=self.config.RENDER_MAX_HTML_BYTES)
            chunks.append(chunk)
        return "".join(chunks)

    async def from_jinja_template(self, template: str, data: dict) -> tuple[str, str]:
        return await self.from_html(self.render_template(template, data))

    async def from_html(self, html: str) -> tuple[str, str]:
        self._validate_html(html)
        path, absolute = generate_data_path(suffix="html", namespace="rendered")
        Path(path).write_text(html, encoding="utf-8")
        return path, absolute

    @staticmethod
    def _resolve_viewport_size(
        html_content: str, screenshot_options: ScreenshotOptions
    ) -> tuple[int | None, int | None]:
        """根据 HTML 内容（字符串）推断 viewport 大小（宽, 高）。

        优先级：
        1. 调用方在 ScreenshotOptions 中显式指定 `viewport_width` / `viewport_height`；
        2. 从 HTML 中的 `<meta name="viewport" content="width=...; height=...">` 自动解析；
        3. 未能解析到时返回对应的 None（调用方可选择使用 Playwright 默认值）。
        """

        viewport_width: int | None = screenshot_options.viewport_width
        viewport_height: int | None = screenshot_options.viewport_height

        # 如果两者都有显式值，直接返回
        if viewport_width is not None and viewport_height is not None:
            return viewport_width, viewport_height

        # 未指定时，尝试从 HTML meta 中解析（只读前 4KB 即可命中 <head> 区域）
        try:
            head_snippet = html_content[:4096]

            # 尝试解析宽度和高度（允许任意顺序出现在 content 中）
            if viewport_width is None:
                pattern = (
                    r'<meta\s+[^>]*name=["\']viewport["\'][^>]*'
                    r'content=["\'][^"\']*width\s*=\s*(\d+)[^"\']*["\'][^>]*>'
                )
                if m := re.search(pattern, head_snippet, re.IGNORECASE):
                    viewport_width = int(m[1])

            if viewport_height is None:
                pattern = (
                    r'<meta\s+[^>]*name=["\']viewport["\'][^>]*'
                    r'content=["\'][^"\']*height\s*=\s*(\d+)[^"\']*["\'][^>]*>'
                )
                if m := re.search(pattern, head_snippet, re.IGNORECASE):
                    viewport_height = int(m[1])
        except (re.error, ValueError) as e:
            logger.debug(f"Adjust viewport from meta tag failed: {e}")

        return viewport_width, viewport_height

    def _validate_dimensions(self, width, height, factor):
        if (not math.isfinite(width) or not math.isfinite(height)
                or width <= 0 or height <= 0):
            raise RenderLimitError("screenshot requires finite positive dimensions",
                budget="invalid_dimensions")
        physical_width, physical_height = math.ceil(width * factor), math.ceil(height * factor)
        details = {"width": width, "height": height, "dpr": factor,
                   "physical_width": physical_width, "physical_height": physical_height}
        if max(physical_width, physical_height) > self.config.RENDER_MAX_DIMENSION:
            raise RenderLimitError("screenshot exceeds configured dimension limit",
                budget="screenshot_dimension", limit=self.config.RENDER_MAX_DIMENSION, **details)
        if physical_width * physical_height > self.config.RENDER_MAX_PIXELS:
            raise RenderLimitError("screenshot exceeds configured pixel limit",
                budget="screenshot_pixels", actual=physical_width * physical_height,
                limit=self.config.RENDER_MAX_PIXELS, **details)

    def _viewport(self, html, options):
        width, height = self._resolve_viewport_size(html, options)
        width, height = width or 800, height or 720
        if width > self.config.RENDER_MAX_VIEWPORT_WIDTH or height > self.config.RENDER_MAX_VIEWPORT_HEIGHT:
            raise RenderLimitError("viewport exceeds configured limit", budget="viewport",
                width=width, height=height, max_width=self.config.RENDER_MAX_VIEWPORT_WIDTH,
                max_height=self.config.RENDER_MAX_VIEWPORT_HEIGHT)
        level = options.device_scale_factor_level or "normal"
        self._validate_dimensions(width, height, self.SCALE_FACTOR_MAP[level])
        if options.clip:
            clip = options.clip
            if any(not math.isfinite(v) for v in clip.values()) or clip["x"] < 0 or clip["y"] < 0:
                raise RenderLimitError("clip must have finite non-negative coordinates", budget="invalid_clip")
            factor = 1 if options.scale == "css" else self.SCALE_FACTOR_MAP[level]
            self._validate_dimensions(clip["width"], clip["height"], factor)
            if clip["x"] + clip["width"] > self.config.RENDER_MAX_DIMENSION or clip["y"] + clip["height"] > self.config.RENDER_MAX_DIMENSION:
                raise RenderLimitError("clip extends beyond configured dimension limit",
                    budget="clip_extent", limit=self.config.RENDER_MAX_DIMENSION, **clip)
        return width, height, level

    async def _capture(self, html, options, viewport):
        width, height, level = viewport
        context = None
        registered = False
        try:
            # All lifecycle mutations are serialized. A fresh context per render isolates
            # cookies/storage and releases page, worker and decoded resource memory.
            async with self._lock:
                if self._recycle_pending:
                    raise RenderBusyError("browser recycling; retry shortly")
                await self._ensure_browser_locked()
                self._active += 1
                registered = True
                context = await self.browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=self.SCALE_FACTOR_MAP[level],
                    java_script_enabled=self.config.RENDER_JAVASCRIPT_ENABLED,
                    service_workers="block",
                    accept_downloads=False,
                )
            requests = 0
            async def route_resource(route):
                nonlocal requests
                requests += 1
                # Keep fonts, CSS, scripts, and images for existing templates; prevent
                # extra documents/media and unbounded request fan-out.
                if (requests > self.config.RENDER_MAX_RESOURCE_REQUESTS
                        or route.request.resource_type in {"media", "websocket"}
                        or route.request.is_navigation_request()
                        or not route.request.url.startswith(("http://", "https://"))):
                    await route.abort()
                else:
                    await route.continue_()
            await context.route("**/*", route_resource)
            page = await context.new_page()
            # DOM readiness is independent of analytics/polling connections. Wait for
            # useful assets for a bounded time instead of requiring network silence.
            await page.set_content(html, wait_until="domcontentloaded", timeout=self.config.RENDER_TIMEOUT * 1000)
            asset_start = time.monotonic()
            try:
                await page.wait_for_load_state("load", timeout=max(1, self.config.RENDER_ASSET_TIMEOUT_MS))
            except PlaywrightTimeout:
                pass
            remaining = max(0, self.config.RENDER_ASSET_TIMEOUT_MS - (time.monotonic() - asset_start) * 1000)
            assets_ready = await page.evaluate("""async (budget) => {
                const images = Array.from(document.images, image => image.complete
                    ? Promise.resolve() : new Promise(resolve => {
                        image.addEventListener('load', resolve, {once:true});
                        image.addEventListener('error', resolve, {once:true});
                    }));
                return await Promise.race([
                    Promise.all([document.fonts.ready, ...images]).then(() => true),
                    new Promise(resolve => setTimeout(() => resolve(false), budget))
                ]);
            }""", remaining)
            if not assets_ready:
                # Abort stalled resources. Fonts can otherwise make screenshot() wait
                # again even after our bounded asset deadline has expired.
                await page.evaluate("window.stop()")
                logger.warning("Asset deadline reached; rendering available content")
            size = await page.evaluate("""() => ({
                width: Math.max(document.documentElement.scrollWidth, document.body?.scrollWidth || 0),
                height: Math.max(document.documentElement.scrollHeight, document.body?.scrollHeight || 0)
            })""")
            factor = 1 if options.scale == "css" else self.SCALE_FACTOR_MAP[level]
            if options.full_page and not options.clip:
                self._validate_dimensions(size["width"], size["height"], factor)
            kwargs = self._screenshot_kwargs(options)
            if options.full_page and not options.clip:
                # Freeze the checked rectangle so late layout growth cannot bypass
                # the pixel budget between measurement and screenshot.
                kwargs["clip"] = {"x": 0, "y": 0, **size}
            result = await page.screenshot(**kwargs)
            if len(result) > self.config.RENDER_MAX_IMAGE_BYTES:
                raise RenderLimitError("encoded image exceeds configured byte limit",
                    budget="image_bytes", actual=len(result), limit=self.config.RENDER_MAX_IMAGE_BYTES)
            return result
        finally:
            # Shield cleanup from a deadline arriving *during* close(). Retain the
            # render slot until cleanup completes, even when the caller cancels.
            cleanup = asyncio.create_task(self._release_context(context, registered))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _release_context(self, context, registered):
        if registered and context is None:
            # Context creation can fail after the browser allocated it.
            self._recycle_pending = True
        if context is not None:
            try:
                await asyncio.wait_for(context.close(), 5)
            except Exception as exc:
                self._recycle_pending = True
                logger.warning("Context cleanup failed: {}", exc)
        if registered:
            async with self._lock:
                self._active -= 1
                self._renders += 1
                expired = (self._renders >= self.config.BROWSER_MAX_RENDERS
                           or time.monotonic() - self._started >= self.config.BROWSER_MAX_AGE)
                self._recycle_pending = self._recycle_pending or expired
                # Once rotation is due, reject new acquisitions until active
                # renders drain; no ongoing screenshot is killed by rotation.
                if self._active == 0 and self._recycle_pending:
                    await self._close_browser_locked()

    async def html2pic_bytes(self, html_content: str, screenshot_options: ScreenshotOptions) -> bytes:
        self._validate_html(html_content)
        viewport = self._viewport(html_content, screenshot_options)
        async with self._admit():
            try:
                async with asyncio.timeout(self.config.RENDER_TIMEOUT):
                    return await self._capture(html_content, screenshot_options, viewport)
            except (TimeoutError, PlaywrightTimeout) as exc:
                raise RenderTimeoutError("render deadline exceeded") from exc

    async def html2pic_file(self, html_content: str, screenshot_options: ScreenshotOptions) -> str:
        image = await self.html2pic_bytes(html_content, screenshot_options)
        path, _ = generate_data_path(suffix=screenshot_options.type or "png", namespace="rendered")
        Path(path).write_bytes(image)
        return path

    async def html2pic(self, html_file_path: str, screenshot_options: ScreenshotOptions) -> str:
        size = Path(html_file_path).stat().st_size
        if size > self.config.RENDER_MAX_HTML_BYTES:
            raise RenderLimitError("HTML file exceeds configured byte limit",
                budget="html_file_bytes", actual=size, limit=self.config.RENDER_MAX_HTML_BYTES)
        return await self.html2pic_file(Path(html_file_path).read_text(encoding="utf-8"), screenshot_options)

    def _screenshot_kwargs(self, opts: ScreenshotOptions) -> dict:
        kwargs = opts.model_dump(exclude_none=True)
        for name in ("viewport_width", "viewport_height", "device_scale_factor_level"):
            kwargs.pop(name, None)
        if opts.type != "jpeg":
            kwargs.pop("quality", None)
        # A client timeout of zero must not disable the server's budget.
        kwargs["timeout"] = min(opts.timeout or self.config.RENDER_SCREENSHOT_TIMEOUT_MS,
                                self.config.RENDER_SCREENSHOT_TIMEOUT_MS)
        kwargs.setdefault("animations", "disabled")
        return kwargs
