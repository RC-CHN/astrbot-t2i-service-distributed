# AstrBot Text2Image Service (无状态版)

一个将 HTML/模板转换为图片的无状态 Web 服务。服务本身不存储任何持久化数据，所有生成的图片都保存在外部的 S3 兼容对象存储中，使其非常适合在 Kubernetes 等分布式环境中进行水平扩展。

## 架构

-   **应用服务 (`app`)**: 负责接收请求、使用 Playwright 渲染图片、将图片上传到对象存储，并通过流式传输返回图片数据。
-   **对象存储 (`minio`)**: 作为持久化层，存储所有生成的图片。

为了 100% 保持对现有客户端的 API 兼容性，本服务直接处理所有数据请求，不依赖额外的缓存或重定向层。

## 快速开始 (使用 Docker Compose)

1.  **启动服务**:
    ```bash
    docker-compose up -d --build
    ```
    服务将在 `http://localhost:8999` 启动，MinIO 控制台在 `http://localhost:9001`。

2.  **测试 API**:
    ```bash
    # 生成图片并直接获取
    curl -X POST "http://localhost:8999/text2img/generate" \
    -H "Content-Type: application/json" \
    -d '{
      "html": "<h1>Hello, World!</h1>",
      "options": { "type": "png" }
    }' \
    --output hello.png

    # 生成图片并获取 ID
    curl -X POST "http://localhost:8999/text2img/generate" \
    -H "Content-Type: application/json" \
    -d '{
      "html": "<h1>Hello, JSON!</h1>",
      "as_json": true
    }'
    # 假设返回: {"code":0,"message":"success","data":{"id":"data/rendered_..."}}

    # 使用 ID 获取图片
    # curl "http://localhost:8999/text2img/data/{id}" --output fetched.png
    ```

A simple web service that converts HTML/templates to images, with image lifecycle management support.

服务通过环境变量进行配置，支持从 `.env` 文件加载。

### S3 兼容存储配置

| 变量名 | 描述 | 默认值 | 示例 |
|---|---|---|---|
| `S3_ENDPOINT_URL` | S3 兼容服务的地址。如果使用 AWS S3，请留空。 | `http://minio:9000` | `http://minio:9000` 或 `https://s3.amazonaws.com` |
| `S3_ACCESS_KEY_ID` | 访问密钥 ID | `minioadmin` | `YOUR_AWS_ACCESS_KEY` |
| `S3_SECRET_ACCESS_KEY` | 访问密钥 Secret | `minioadmin` | `YOUR_AWS_SECRET_KEY` |
| `S3_BUCKET_NAME` | 用于存储图片的 Bucket 名称 | `text2img` | `my-image-bucket` |

### 其他配置

| 变量名 | 描述 | 默认值 |
|---|---|---|
| `PORT` | 服务端口 | `8999` |

- `PORT`: Service port, default is 8999
- `IMAGE_LIFETIME_HOURS`: Image lifetime in hours, default is 24 hours. Images older than this will be automatically cleaned up

## API Endpoints

### POST /text2img/generate

将 HTML 或 Jinja2 模板转换为图片。

**请求体**:
-   `str` `html`: HTML 文本。
-   `str` `tmpl`: Jinja2 HTML 模板字符串。
-   `str` `tmplname`: Jinja2 HTML 模板文件名（需挂载 `tmpl` 目录）。
-   `dict` `tmpldata`: 渲染模板所需的数据。
-   `bool` `as_json`: 是否返回 JSON 格式（返回一个图片 ID）。默认为 `false`。
-   `dict` `options`: 截图选项（可选）。

- `str` html: HTML text
- `str` tmpl: Jinja2 HTML template
- `dict` tmpldata: Jinja2 template data
- `bool` json: Whether to return JSON format (returns an id)
- `dict` `optional` options
  - timeout (float, optional): Screenshot timeout.
  - type (Literal["jpeg", "png"], optional): Screenshot image type.
  - quality (int, optional): Screenshot quality, only applicable to JPEG format.
  - omit_background (bool, optional): Whether to hide the default white background, allowing transparent screenshots (PNG only).
  - full_page (bool, optional): Whether to capture the entire page instead of just the viewport, default is True.
  - clip (FloatRect, optional): Area to clip after screenshot, xy is the starting point.
  - animations: (Literal["allow", "disabled"], optional): Whether to allow CSS animations.
  - caret: (Literal["hide", "initial"], optional): When set to `hide`, the text caret will be hidden during screenshot, default is `hide`.
  - scale: (Literal["css", "device"], optional): Page scaling settings. When set to `css`, device resolution maps 1:1 with CSS pixels, making screenshots smaller on high-DPI screens. When set to `device`, scales according to device screen scaling or the device_scale_factor parameter in the current Playwright Page/Context.
  - viewport_width (int, optional): Custom viewport width to control screenshot width. Resolved in priority order:
    1. Explicitly set in request options
    2. Auto-parsed from `<meta name="viewport" content="width=...">` in HTML
    3. Defaults to 800px if not specified and no meta tag found
  - viewport_height (int, optional): Custom viewport height to control screenshot height. Resolved in priority order:
    1. Explicitly set in request options
    2. Auto-parsed from `<meta name="viewport" content="height=...">` in HTML
    3. Defaults to 600px if not specified and no meta tag found
  - device_scale_factor_level (Literal["normal", "high", "ultra"], optional): Device pixel ratio level, default is "normal". Different levels use independent browser context pools for better performance and resource isolation.
    - `normal`: Device pixel ratio 1.0 (default)
    - `high`: Device pixel ratio 1.3
    - `ultra`: Device pixel ratio 1.8

### GET /text2img/data/{id}

根据图片 ID 从对象存储中获取图片。

**路径参数**:
-   `id`: 图片 ID，由 `POST /text2img/generate` 返回。

**响应**:
-   成功时：返回图片文件的二进制流。
-   失败时：返回 404 Not Found 或 500 Internal Server Error 的 JSON。
