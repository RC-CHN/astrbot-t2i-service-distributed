import fastapi
import os
import asyncio
from pydantic import BaseModel, Field
from jinja2.exceptions import SecurityError
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi import BackgroundTasks
from loguru import logger
from .render import Text2ImgRender, ScreenshotOptions
from .config import settings
from .storage import storage_service

app = fastapi.FastAPI()
render = Text2ImgRender()


class GenerateRequest(BaseModel):
    html: str | None = None
    tmpl: str | None = None
    tmplname: str | None = None
    tmpldata: dict | None = None
    options: ScreenshotOptions | None = None
    as_json: bool = Field(default=False, alias="json")


@app.get("/text2img/data/{image_path:path}")
async def text2img_image(image_path: str):
    """
    从对象存储获取图片并流式返回给客户端。
    """
    try:
        # The full path is /text2img/data/{image_path}, where image_path is "data/rendered/..."
        # We need to construct the full object key from the path parameter.
        object_key = f"data/{image_path}"
        stream = storage_service.download_stream(object_key)
        if stream:
            # 根据路径推断 media_type
            media_type = "image/png" if image_path.endswith(".png") else "image/jpeg"
            return StreamingResponse(stream, media_type=media_type)
        else:
            return JSONResponse(
                status_code=404,
                content={"code": 1, "message": "file not found", "data": {}},
            )
    except Exception as e:
        logger.error(f"Error fetching image {object_key} from storage: {e}")
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": "internal server error", "data": {}},
        )


@app.post("/text2img/generate")
async def text2img(request: GenerateRequest):
    """
    生成图片，上传到对象存储，并根据请求类型返回结果。
    为了保持兼容性，当 json=false 时，仍然返回本地临时文件的 FileResponse。
    """
    is_json_return = request.as_json or False
    html_file_path = None
    pic_path = None

    try:
        if request.tmpl or request.tmplname:
            if request.tmpl:
                tmpl = request.tmpl
            else:
                tmpl = open(f"tmpl/{request.tmplname}.html", "r", encoding="utf-8").read()
            try:
                html_file_path, abs_path = await render.from_jinja_template(tmpl, request.tmpldata or {})
            except SecurityError as e:
                return JSONResponse(
                    status_code=400,
                    content={"code": 1, "message": f"security error: {str(e)}", "data": {}},
                )
            except Exception as e:
                return JSONResponse(
                    status_code=500,
                    content={
                        "code": 1,
                        "message": f"template render error: {str(e)}",
                        "data": {},
                    },
                )
        elif request.html:
            html = request.html
            html_file_path, abs_path = await render.from_html(html)
        else:
            return JSONResponse(
                status_code=400,
                content={"code": 1, "message": "html or tmpl not found", "data": {}},
            )
        
        options = (
            request.options
            if request.options
            else ScreenshotOptions(
                timeout=None,
                type="png",
                quality=None,
                omit_background=None,
                full_page=True,
                clip=None,
                animations=None,
                caret=None,
                scale="device",
            )
        )

        pic_path = await render.html2pic(abs_path, options)
        media_type = "image/png" if pic_path.endswith(".png") else "image/jpeg"

        # 构造对象存储的 key，保持与旧 ID 格式一致
        # 例如: data/rendered/xxxx-xxxx-xxxx.png
        object_key = pic_path.replace("\\", "/")

        # 上传到对象存储
        storage_service.upload(pic_path, object_key, content_type=media_type)
        logger.info(f"Successfully uploaded {pic_path} to storage as {object_key}")

        if is_json_return:
            # 清理本地临时文件，因为客户端只需要 ID
            if os.path.exists(abs_path): os.remove(abs_path)
            if os.path.exists(pic_path): os.remove(pic_path)
            return JSONResponse(
                content={
                    "code": 0,
                    "message": "success",
                    "data": {"id": object_key},
                },
            )
        else:
            # 返回本地临时文件的 FileResponse 以保持兼容性
            # 使用 BackgroundTasks 在响应发送后清理文件
            background_tasks = BackgroundTasks()
            background_tasks.add_task(os.remove, abs_path)
            background_tasks.add_task(os.remove, pic_path)
            return FileResponse(pic_path, media_type=media_type, background=background_tasks)

    except Exception as e:
        logger.error(f"Error during image generation or upload: {e}")
        # 清理可能已创建的临时文件
        if html_file_path and os.path.exists(html_file_path): os.remove(html_file_path)
        if abs_path and os.path.exists(abs_path): os.remove(abs_path)
        if pic_path and os.path.exists(pic_path): os.remove(pic_path)
        return JSONResponse(
            status_code=500,
            content={"code": 1, "message": f"internal server error: {str(e)}", "data": {}},
        )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8999)))
