import asyncio
import os
from contextlib import asynccontextmanager, suppress

import fastapi
from fastapi.responses import Response, JSONResponse
from jinja2.exceptions import SecurityError
from loguru import logger
from pydantic import BaseModel, Field, ConfigDict

from . import __version__
from .cache import RedisImageCache
from .config import settings
from .concurrency import complete_before_cancel
from .render import ScreenshotOptions, Text2ImgRender, RenderError, RenderLimitError
from .limits import RequestBudgetMiddleware, log_rejection
from .storage import storage_service
from .util import get_image_lifetime, generate_data_path


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    await render.start()
    await asyncio.gather(cache.connect(), storage_service.ensure_ready())
    monitor = asyncio.create_task(_monitor_dependencies())
    try:
        yield
    finally:
        monitor.cancel()
        with suppress(asyncio.CancelledError):
            await monitor
        await asyncio.gather(render.terminate(), cache.disconnect(), storage_service.close())


app = fastapi.FastAPI(lifespan=lifespan, version=__version__)
app.add_middleware(RequestBudgetMiddleware)
render = Text2ImgRender()
cache = RedisImageCache()


class GenerateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    html: str | None = None
    tmpl: str | None = None
    tmplname: str | None = None
    tmpldata: dict | None = None
    options: ScreenshotOptions | None = None
    as_json: bool = Field(default=False, alias="json")


async def _persist_image(object_key: str, data: bytes, media_type: str) -> bool:
    cached, uploaded = await asyncio.gather(
        cache.set(object_key, data, ttl=get_image_lifetime()),
        storage_service.aio_put_bytes(object_key, data, media_type),
    )
    if not uploaded:
        logger.warning("S3 upload failed for {}; cached={}", object_key, cached)
    return bool(uploaded)


def _download_image(object_key):
    stream = storage_service.download_stream(object_key)
    if stream is None:
        return None
    try:
        data = stream.read(settings.RENDER_MAX_IMAGE_BYTES + 1)
        if len(data) > settings.RENDER_MAX_IMAGE_BYTES:
            raise RenderLimitError("stored image exceeds byte limit",
                budget="s3_image_bytes", actual=len(data), limit=settings.RENDER_MAX_IMAGE_BYTES)
        return data
    finally:
        stream.close()


async def _monitor_dependencies():
    while True:
        await asyncio.sleep(settings.S3_RETRY_INTERVAL)
        # Recover the driver proactively: an unready worker may receive no user
        # traffic, so recovery cannot depend on the next render request.
        results = await asyncio.gather(render.start(),
            storage_service.ensure_ready(force=True), cache.check(), return_exceptions=True)
        for component, result in zip(("renderer", "s3", "redis"), results):
            if isinstance(result, Exception):
                logger.warning("Dependency {} check failed: {}", component, result)


@app.get("/healthz")
async def healthz():
    return {"status": "live"}


@app.get("/readyz")
async def readyz():
    # Cache-only workers can still serve existing images. New JSON generations
    # explicitly return 503 until durable storage recovers.
    available = render.healthy and (storage_service.ready or cache.healthy)
    return JSONResponse(status_code=200 if available else 503,
        content={"status": "ready" if available else "unavailable",
                 "renderer": bool(render.healthy), "s3": bool(storage_service.ready),
                 "redis": bool(cache.healthy)})


# ═══════════════════════════════════════════════════════════════════════
#  Routes
# ═══════════════════════════════════════════════════════════════════════

@app.get("/text2img/data/{image_path:path}")
async def text2img_image(image_path: str):
    """
    Serve an image — cache-first, S3 fallback.
    """
    normalized = image_path.removeprefix("data/")
    object_key = f"data/{normalized}"
    media_type = "image/png" if image_path.endswith(".png") else "image/jpeg"

    try:
        # 1) Redis cache
        cached = await cache.get(object_key)
        if cached is not None:
            if len(cached) > settings.RENDER_MAX_IMAGE_BYTES:
                raise RenderLimitError("cached image exceeds byte limit",
                    budget="redis_image_bytes", actual=len(cached), limit=settings.RENDER_MAX_IMAGE_BYTES)
            return Response(cached, media_type=media_type)

        # 2) S3 fallback
        data = await complete_before_cancel(asyncio.to_thread(_download_image, object_key))
        if data is None:
            return JSONResponse(
                status_code=404,
                content={"code": 1, "message": "file not found", "data": {}},
            )
        # Populate cache for next request (best-effort)
        await cache.set(object_key, data, ttl=get_image_lifetime())
        return Response(data, media_type=media_type)

    except RenderLimitError as exc:
        log_rejection("download", exc.status_code, str(exc), **exc.details)
        return JSONResponse(status_code=413,
            content={"code": 1, "message": str(exc), "data": {}})
    except Exception as e:
        logger.error("Error fetching {}: {}", object_key, e)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": "internal server error", "data": {}},
        )


@app.post("/text2img/generate")
async def text2img(request: GenerateRequest):
    """
    Render HTML → image, cache in Redis and upload within bounded admission.
    """
    is_json_return = request.as_json or False

    try:
        # ── Resolve HTML content ──────────────────────────────────
        if request.html:
            html_str = request.html
        elif request.tmpl:
            try:
                html_str = render.render_template(
                    request.tmpl, request.tmpldata or {}
                )
            except SecurityError as e:
                return JSONResponse(
                    status_code=400,
                    content={"code": 1, "message": f"security error: {e}", "data": {}},
                )
        elif request.tmplname:
            try:
                from pathlib import Path
                template_root = Path("tmpl").resolve()
                template_path = (template_root / f"{request.tmplname}.html").resolve()
                if not template_path.is_relative_to(template_root):
                    return JSONResponse(status_code=400, content={"code": 1, "message": "invalid template name", "data": {}})
                tmpl = template_path.read_text(encoding="utf-8")
                html_str = render.render_template(tmpl, request.tmpldata or {})
            except SecurityError as e:
                return JSONResponse(
                    status_code=400,
                    content={"code": 1, "message": f"security error: {e}", "data": {}},
                )
            except FileNotFoundError:
                return JSONResponse(
                    status_code=404,
                    content={"code": 1, "message": f"template '{request.tmplname}' not found", "data": {}},
                )
        else:
            return JSONResponse(
                status_code=400,
                content={"code": 1, "message": "html, tmpl, or tmplname required", "data": {}},
            )

        options = request.options or ScreenshotOptions(
            timeout=None,
            type="png",
            quality=None,
            omit_background=None,
            full_page=True,
            clip=None,
            animations=None,
            caret=None,
            scale="device",
            viewport_width=None,
            viewport_height=None,
            device_scale_factor_level=None,
        )

        media_type = "image/png" if options.type != "jpeg" else "image/jpeg"
        suffix = options.type if options.type else "png"

        # The HTTP path stays in memory for both response modes, so a client
        # disconnect cannot strand a temporary image file before background cleanup.
        image_bytes = await render.html2pic_bytes(html_str, options)
        object_key, _ = generate_data_path(suffix=suffix, namespace="rendered")
        object_key = object_key.replace("\\", "/")
        persisted = await _persist_image(object_key, image_bytes, media_type)
        if is_json_return:
            if not persisted:
                return JSONResponse(status_code=503, content={"code": 1, "message": "image storage unavailable", "data": {}}, headers={"Retry-After": "5"})
            return JSONResponse(
                content={"code": 0, "message": "success", "data": {"id": object_key}},
            )
        return Response(content=image_bytes, media_type=media_type)

    except RenderError as e:
        log_rejection("generate", e.status_code, str(e), **e.details)
        return JSONResponse(status_code=e.status_code,
            content={"code": 1, "message": str(e), "data": {}},
            headers={"Retry-After": "2"} if e.status_code == 429 else None)

    except Exception as e:
        logger.error("Error during image generation: {}", e)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": f"internal server error: {e}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8999)))
