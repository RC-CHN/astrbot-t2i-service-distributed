import asyncio
import io
import os
from contextlib import asynccontextmanager

import fastapi
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from jinja2.exceptions import SecurityError
from loguru import logger
from pydantic import BaseModel, Field

from .cache import RedisImageCache
from .config import settings
from .render import ScreenshotOptions, Text2ImgRender
from .storage import storage_service
from .util import get_image_lifetime


@asynccontextmanager
async def lifespan(app: fastapi.FastAPI):
    await cache.connect()
    yield
    await cache.disconnect()


app = fastapi.FastAPI(lifespan=lifespan)
render = Text2ImgRender()
cache = RedisImageCache()


class GenerateRequest(BaseModel):
    html: str | None = None
    tmpl: str | None = None
    tmplname: str | None = None
    tmpldata: dict | None = None
    options: ScreenshotOptions | None = None
    as_json: bool = Field(default=False, alias="json")


# ── Background S3 upload helper ─────────────────────────────────────

async def _bg_upload_to_s3(pic_path: str, object_key: str, media_type: str) -> None:
    """Fire-and-forget: upload to S3 in the background.

    Uses a semaphore and retry logic (built into StorageService.aio_upload)
    so that a transient S3 outage does not lose the image permanently.
    The local file is NOT deleted here — the caller is responsible for cleanup.
    """
    ok = await storage_service.aio_upload(pic_path, object_key, media_type)
    if not ok:
        logger.error(
            "Background S3 upload FAILED after retries: {} → {}."
            " Image still cached in Redis (TTL {:d}s);"
            " will be lost when TTL expires unless manually recovered.",
            pic_path, object_key, get_image_lifetime(),
        )


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
            return StreamingResponse(io.BytesIO(cached), media_type=media_type)

        # 2) S3 fallback
        stream = storage_service.download_stream(object_key)
        if stream is None:
            return JSONResponse(
                status_code=404,
                content={"code": 1, "message": "file not found", "data": {}},
            )
        data = stream.read()
        # Populate cache for next request (best-effort)
        await cache.set(object_key, data, ttl=get_image_lifetime())
        return StreamingResponse(io.BytesIO(data), media_type=media_type)

    except Exception as e:
        logger.error("Error fetching {}: {}", object_key, e)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": "internal server error", "data": {}},
        )


@app.post("/text2img/generate")
async def text2img(request: GenerateRequest):
    """
    Render HTML → image, cache in Redis, schedule background S3 upload.
    """
    is_json_return = request.as_json or False
    html_file_path = None
    abs_path = None
    pic_path = None

    try:
        # ── Render phase ──────────────────────────────────────────
        if request.tmpl or request.tmplname:
            if request.tmpl:
                tmpl = request.tmpl
            else:
                tmpl = open(f"tmpl/{request.tmplname}.html", encoding="utf-8").read()
            try:
                html_file_path, abs_path = await render.from_jinja_template(
                    tmpl, request.tmpldata or {}
                )
            except SecurityError as e:
                return JSONResponse(
                    status_code=400,
                    content={"code": 1, "message": f"security error: {e}", "data": {}},
                )
            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={"code": 1, "message": f"template render error: {e}", "data": {}},
                )
        elif request.html:
            html_file_path, abs_path = await render.from_html(request.html)
        else:
            return JSONResponse(
                status_code=400,
                content={"code": 1, "message": "html or tmpl not found", "data": {}},
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

        pic_path = await render.html2pic(abs_path, options)
        media_type = "image/png" if pic_path.endswith(".png") else "image/jpeg"
        object_key = pic_path.replace("\\", "/")

        # ── Cache + background upload ─────────────────────────────
        # Read rendered image into memory for Redis cache.
        with open(pic_path, "rb") as f:
            image_bytes = f.read()

        # Write to Redis *first* so the ID is instantly available.
        await cache.set(object_key, image_bytes, ttl=get_image_lifetime())
        logger.info("Cached {} in Redis", object_key)

        # Schedule non-blocking S3 upload in the background.
        asyncio.create_task(_bg_upload_to_s3(pic_path, object_key, media_type))

        # ── Response ──────────────────────────────────────────────
        if is_json_return:
            # Clean up local files; image lives in Redis + (soon) S3.
            if os.path.exists(abs_path):
                os.remove(abs_path)
            if os.path.exists(pic_path):
                os.remove(pic_path)
            return JSONResponse(
                content={
                    "code": 0,
                    "message": "success",
                    "data": {"id": object_key},
                },
            )
        else:
            # FileResponse: serve the local file, clean up afterwards.
            bg = BackgroundTasks()
            bg.add_task(os.remove, abs_path)
            bg.add_task(os.remove, pic_path)
            # Also fire S3 upload via background tasks so we don't lose it.
            return FileResponse(pic_path, media_type=media_type, background=bg)

    except Exception as e:
        logger.error("Error during image generation: {}", e)
        if html_file_path and os.path.exists(html_file_path):
            os.remove(html_file_path)
        if abs_path and os.path.exists(abs_path):
            os.remove(abs_path)
        if pic_path and os.path.exists(pic_path):
            os.remove(pic_path)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": f"internal server error: {e}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8999)))
