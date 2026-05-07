import re

from .util import generate_data_path
from playwright.async_api import async_playwright
from jinja2.sandbox import SandboxedEnvironment
from pydantic import BaseModel
from typing_extensions import TypedDict
from typing import Literal
from loguru import logger
from playwright.async_api import BrowserContext, Browser, Playwright
from playwright._impl._errors import TargetClosedError


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

    timeout: float | None = None
    type: Literal["jpeg", "png", None] = None
    quality: int | None = None
    omit_background: bool | None = None
    full_page: bool | None = True
    clip: FloatRect | None = None
    animations: Literal["allow", "disabled", None] = None
    caret: Literal["hide", "initial", None] = None
    scale: Literal["css", "device", None] = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    device_scale_factor_level: Literal["normal", "high", "ultra", None] = None


class Text2ImgRender:
    # Mapping from device_scale_factor_level to actual device_scale_factor
    SCALE_FACTOR_MAP = {
        "normal": 1.0,
        "high": 1.3,
        "ultra": 1.8,
    }

    def __init__(self):
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        # Context pool: {"normal": context, "high": context, "ultra": context}
        self.contexts: dict[str, BrowserContext] = {}

    async def _ensure_context(self, level: str = "normal") -> BrowserContext:
        """Ensure that Playwright, Browser and BrowserContext are initialized.

        Args:
            level: Device scale factor level ("normal", "high", or "ultra").
                   Defaults to "normal" if not specified.

        Returns:
            The BrowserContext for the specified level.
        """
        if self.playwright is None:
            self.playwright = await async_playwright().start()

        # ensure browser launched
        if self.browser is None or not self.browser.is_connected():
            if self.browser is not None:
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.debug(f"Close old browser failed: {e}")
            self.browser = await self.playwright.chromium.launch(headless=True)

        # ensure context available for the specified level
        if level not in self.contexts:
            scale_factor = self.SCALE_FACTOR_MAP.get(level, 1.0)
            self.contexts[level] = await self.browser.new_context(
                device_scale_factor=scale_factor,
            )
            logger.info(
                f"Created context for level '{level}' with device_scale_factor={scale_factor}"
            )

        return self.contexts[level]

    async def from_jinja_template(self, template: str, data: dict) -> tuple[str, str]:
        env = SandboxedEnvironment()
        html = env.from_string(template).render(data)
        return await self.from_html(html)

    def render_template(self, template: str, data: dict) -> str:
        """Render Jinja2 template to HTML string.  Zero file I/O.

        Unlike from_jinja_template(), this does NOT write to disk —
        it returns the raw HTML string suitable for use with
        html2pic_bytes() / html2pic_file().
        """
        env = SandboxedEnvironment()
        return env.from_string(template).render(data)

    async def from_html(self, html: str) -> tuple[str, str]:
        html_file_path, abs_path = generate_data_path(
            suffix="html", namespace="rendered"
        )
        with open(html_file_path, "w", encoding="utf-8") as f:
            f.write(html)
        return html_file_path, abs_path

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

    async def terminate(self) -> None:
        """Terminate Playwright and close browser."""
        # Close all contexts in the pool
        for level, context in list(self.contexts.items()):
            try:
                await context.close()
                logger.debug(f"Closed context for level '{level}'")
            except Exception as e:
                logger.debug(f"Close context for level '{level}' failed: {e}")
        self.contexts.clear()

        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception as e:
                logger.debug(f"Close browser failed: {e}")
            self.browser = None

        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception as e:
                logger.debug(f"Stop Playwright failed: {e}")
            self.playwright = None

    async def html2pic(
            self, html_file_path: str, screenshot_options: ScreenshotOptions
    ) -> str:
        # Determine which context to use based on device_scale_factor_level
        level = screenshot_options.device_scale_factor_level or "normal"
        context = await self._ensure_context(level)

        suffix = screenshot_options.type if screenshot_options.type else "png"
        result_path, _ = generate_data_path(suffix=suffix, namespace="rendered")

        try:
            page = await context.new_page()
        except TargetClosedError as e:
            logger.warning(
                f"html2pic: Failed to create new page, restarting browser context: {e}"
            )
            if level in self.contexts:
                try:
                    await self.contexts[level].close()
                except Exception:
                    pass
                del self.contexts[level]
            context = await self._ensure_context(level)
            page = await context.new_page()

        try:
            # Read HTML content once — used for both viewport detection
            # and page.set_content() (zero extra I/O).
            with open(html_file_path, "r", encoding="utf-8") as f:
                html_content = f.read()
        except Exception as e:
            await page.close()
            raise

        vp_w, vp_h = self._resolve_viewport_size(html_content, screenshot_options)
        width = vp_w if vp_w is not None else 800
        height = vp_h if vp_h is not None else 720
        if vp_w is not None or vp_h is not None:
            await page.set_viewport_size({"width": width, "height": height})
            logger.info(f"html2pic: set viewport size to {width}x{height}")

        try:
            await page.set_content(html_content)
            await page.wait_for_load_state("networkidle", timeout=5000)
            screenshot_kwargs = self._screenshot_kwargs(screenshot_options)
            await page.screenshot(path=result_path, **screenshot_kwargs)
        finally:
            await page.close()

        logger.info(f"Rendered {html_file_path} to {result_path}")
        return result_path

    # ── Zero-file-I/O render (for json=true path) ─────────────────────

    async def html2pic_bytes(
        self, html_content: str, screenshot_options: ScreenshotOptions
    ) -> bytes:
        """Render HTML string directly to image bytes.  Zero disk I/O.

        Caller receives the raw bytes and handles caching/upload itself.
        """
        level = screenshot_options.device_scale_factor_level or "normal"
        context = await self._ensure_context(level)

        try:
            page = await context.new_page()
        except TargetClosedError as e:
            logger.warning(f"html2pic_bytes: restarting browser context: {e}")
            if level in self.contexts:
                try:
                    await self.contexts[level].close()
                except Exception:
                    pass
                del self.contexts[level]
            context = await self._ensure_context(level)
            page = await context.new_page()

        vp_w, vp_h = self._resolve_viewport_size(html_content, screenshot_options)
        width = vp_w if vp_w is not None else 800
        height = vp_h if vp_h is not None else 720
        if vp_w is not None or vp_h is not None:
            await page.set_viewport_size({"width": width, "height": height})
            logger.info(f"html2pic_bytes: viewport {width}x{height}")

        try:
            await page.set_content(html_content)
            await page.wait_for_load_state("networkidle", timeout=5000)
            screenshot_kwargs = self._screenshot_kwargs(screenshot_options)
            return await page.screenshot(**screenshot_kwargs)
        finally:
            await page.close()

    # ── File render from HTML string (for json=false path) ────────────

    async def html2pic_file(
        self, html_content: str, screenshot_options: ScreenshotOptions
    ) -> str:
        """Render HTML string to a file on disk.  Returns the file path.

        Used when a physical file is required (e.g. FileResponse).
        """
        level = screenshot_options.device_scale_factor_level or "normal"
        context = await self._ensure_context(level)

        suffix = screenshot_options.type if screenshot_options.type else "png"
        result_path, _ = generate_data_path(suffix=suffix, namespace="rendered")

        try:
            page = await context.new_page()
        except TargetClosedError as e:
            logger.warning(f"html2pic_file: restarting browser context: {e}")
            if level in self.contexts:
                try:
                    await self.contexts[level].close()
                except Exception:
                    pass
                del self.contexts[level]
            context = await self._ensure_context(level)
            page = await context.new_page()

        vp_w, vp_h = self._resolve_viewport_size(html_content, screenshot_options)
        width = vp_w if vp_w is not None else 800
        height = vp_h if vp_h is not None else 720
        if vp_w is not None or vp_h is not None:
            await page.set_viewport_size({"width": width, "height": height})
            logger.info(f"html2pic_file: viewport {width}x{height}")

        try:
            await page.set_content(html_content)
            await page.wait_for_load_state("networkidle", timeout=5000)
            screenshot_kwargs = self._screenshot_kwargs(screenshot_options)
            await page.screenshot(path=result_path, **screenshot_kwargs)
        finally:
            await page.close()

        logger.info(f"Rendered to {result_path}")
        return result_path

    @staticmethod
    def _screenshot_kwargs(opts: ScreenshotOptions) -> dict:
        """Build playwright screenshot kwargs from ScreenshotOptions."""
        kwargs = opts.model_dump(exclude_none=True)
        kwargs.pop("viewport_width", None)
        kwargs.pop("viewport_height", None)
        kwargs.pop("device_scale_factor_level", None)
        if opts.type == "png":
            kwargs.pop("quality", None)
        return kwargs
