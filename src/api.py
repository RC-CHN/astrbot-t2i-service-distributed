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
from .util import get_image_lifetime, generate_data_path


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


# ── Background S3 upload helper ────────────────────────────────────

async def _bg_put_bytes(object_key: str, data: bytes, media_type: str) -> None:
    """Background upload from bytes.  Used by json=true path (zero-file)."""
    ok = await storage_service.aio_put_bytes(object_key, data, media_type)
    if not ok:
        logger.error(
            "Background S3 put_bytes FAILED: {}.  Cached in Redis (TTL {:d}s).",
            object_key, get_image_lifetime(),
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
                tmpl = open(f"tmpl/{request.tmplname}.html", encoding="utf-8").read()
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

        # ── Render ─────────────────────────────────────────────────
        if is_json_return:
            # Zero-file I/O path: render → bytes → cache → bg S3
            image_bytes = await render.html2pic_bytes(html_str, options)
            object_key, _ = generate_data_path(suffix=suffix, namespace="rendered")
            object_key = object_key.replace("\\", "/")

            await cache.set(object_key, image_bytes, ttl=get_image_lifetime())
            logger.info("Cached {} in Redis", object_key)

            asyncio.create_task(_bg_put_bytes(object_key, image_bytes, media_type))

            return JSONResponse(
                content={"code": 0, "message": "success", "data": {"id": object_key}},
            )
        else:
            # File path for FileResponse
            pic_path = await render.html2pic_file(html_str, options)
            object_key = pic_path.replace("\\", "/")

            with open(pic_path, "rb") as f:
                image_bytes = f.read()
            await cache.set(object_key, image_bytes, ttl=get_image_lifetime())

            bg = BackgroundTasks()
            # Upload from bytes we already have in memory — no file race.
            bg.add_task(_bg_put_bytes, object_key, image_bytes, media_type)
            bg.add_task(os.remove, pic_path)

            return FileResponse(pic_path, media_type=media_type, background=bg)

    except Exception as e:
        logger.error("Error during image generation: {}", e)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": f"internal server error: {e}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8999)))
