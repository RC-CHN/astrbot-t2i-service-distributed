# AstrBot Text2Image Service

中文 | [English](README.md) | [日本語](README_ja.md)

## 功能

一个简单的将 HTML/模板转换为图片的 Web 服务，支持图片生命周期管理。

## 环境变量配置

- `PORT`: 服务端口，默认 8999
- `IMAGE_LIFETIME_HOURS`: 图片生命时间（小时），默认 24 小时。超过此时间的图片文件将被自动清理

## API 接口

### POST /text2img/generate

html 转 img

> html 和 tmpl 任选一个。tmpl 和 tmpldata 一起提供。

- `str` html: html 文本
- `str` tmpl: jinja2 html 模板
- `dict` tmpldata: jinja2 模板 data
- `bool` json: 是否返回 json 格式（返回一个 id）
- `dict` `optional` options
  - timeout (float, optional): 截图超时时间.
  - type (Literal["jpeg", "png"], optional): 截图图片类型.
  - quality (int, optional): 截图质量，仅适用于 JPEG 格式图片.
  - omit_background (bool, optional): 是否允许隐藏默认的白色背景，这样就可以截透明图了，仅适用于 PNG 格式
  - full_page (bool, optional): 是否截整个页面而不是仅设置的视口大小，默认为 True.
  - clip (FloatRect, optional): 截图后裁切的区域，xy为起点.
  - animations: (Literal["allow", "disabled"], optional): 是否允许播放 CSS 动画.
  - caret: (Literal["hide", "initial"], optional): 当设置为 `hide` 时，截图时将隐藏文本插入符号，默认为 `hide`.
    - scale: (Literal["css", "device"], optional): 页面缩放设置. 当设置为 `css` 时，则将设备分辨率与 CSS 中的像素一一对应，在高分屏上会使得截图变小. 当设置为 `device` 时，则根据设备的屏幕缩放设置或当前 Playwright 的 Page/Context 中的 device_scale_factor 参数来缩放.
    - viewport_width (int, optional): 自定义视口宽度，用于控制截图宽度. 优先级顺序：
      1. 在请求 options 中显式指定
      2. 从 HTML 的 `<meta name="viewport" content="width=...">` 自动解析
      3. 未指定时默认为 800px
    - viewport_height (int, optional): 自定义视口高度，用于控制截图高度. 优先级顺序：
      1. 在请求 options 中显式指定
      2. 从 HTML 的 `<meta name="viewport" content="height=...">` 自动解析
      3. 未指定时默认为 720px
    - device_scale_factor_level (Literal["normal", "high", "ultra"], optional): 设备像素比等级，默认为 "normal"。每次渲染使用独立的浏览器上下文。
      - `normal`: 设备像素比 1.0（默认）
      - `high`: 设备像素比 1.3
      - `ultra`: 设备像素比 1.8

### GET /text2img/data/{id}

根据 id 返回对应的图像。


## 渲染资源保护与运维

每个 Pod 只运行一个 Uvicorn worker，横向扩副本。服务在解析请求前限制并发请求数和请求体大小，渲染使用独立浏览器 context，结束、异常、取消时均关闭；浏览器累计渲染达到上限或使用时间到期后，在当前渲染完成后轮换。上传在有界请求内完成，避免大量后台任务持有图片字节。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `RENDER_CONCURRENCY` | `2` | 每个 worker 同时渲染数 |
| `RENDER_QUEUE_SIZE` | `4` | 可等待请求数，满后返回 429/Retry-After |
| `RENDER_QUEUE_TIMEOUT` | `10` | 等待渲染槽位秒数 |
| `RENDER_TIMEOUT` | `30` | 渲染总时限秒数，不包括排队、存储和异常时最多 15 秒清理 |
| `RENDER_ASSET_TIMEOUT_MS` | `5000` | 页面资源、字体和图片等待预算；超时截取已有内容 |
| `RENDER_SCREENSHOT_TIMEOUT_MS` | `10000` | 截图阶段上限；客户端不能用 0 关闭服务端超时 |
| `RENDER_MAX_HTML_BYTES` | `33554432` | HTML/模板输出 UTF-8 字节上限，允许内嵌字体/图片 |
| `RENDER_MAX_REQUEST_BYTES` | `41943040` | JSON 请求体上限，含模板数据和转义开销 |
| `RENDER_MAX_VIEWPORT_WIDTH` / `RENDER_MAX_VIEWPORT_HEIGHT` | `2048` / `4096` | 初始视口宽高 |
| `RENDER_MAX_DIMENSION` | `16384` | 最终图像单边像素上限 |
| `RENDER_MAX_PIXELS` | `16000000` | 最终图像总像素上限，包含 DPR 倍率 |
| `RENDER_MAX_IMAGE_BYTES` | `16777216` | 编码后图片字节上限 |
| `RENDER_MAX_RESOURCE_REQUESTS` | `100` | 每次渲染外部资源请求数上限 |
| `RENDER_JAVASCRIPT_ENABLED` | `true` | 保留图表等模板兼容性；静态模板可设为 false |
| `BROWSER_MAX_RENDERS` / `BROWSER_MAX_AGE` | `100` / `600` | 浏览器轮换请求数/秒数 |
| `S3_UPLOAD_CONCURRENCY` | `2` | 同时上传数 |
| `IMAGE_DOWNLOAD_CONCURRENCY` | `16` | 独立图片下载并发数，不占渲染队列；满后返回 429 |
| `S3_CONNECT_TIMEOUT` / `S3_READ_TIMEOUT` | `2` / `5` | S3 单次连接/读取超时秒数 |
| `S3_RETRY_INTERVAL` | `10` | 存储重新检查间隔秒数 |

超出 HTML、视口或截图预算返回 **413**，过载返回 **429**，渲染超时返回 **504**。原有 `json: true`、二进制返回、模板和截图选项保持兼容；JSON 成功响应现在保证 S3 已接受图片，持久化不可用时返回 **503**，不会返回无法持久保存的图片 ID。直接二进制响应可在存储离线时继续提供图片。前端应对 429/503 按 `Retry-After` 退避，长截图可适当提高像素/边长预算并配套内存限制。

GET 下载也受 `RENDER_MAX_IMAGE_BYTES` 限制，Redis 和 S3 最多读取上限加一字节以检测存量超大图片，超过返回 **413**。正在进行的 S3 上传/下载遇到请求协程取消时，会先等待底层有超时的阻塞线程结束再释放并发槽位，防止后台线程继续持有图片。普通 Uvicorn 客户端断连通常不会自动取消路由协程。

资源拒绝日志使用 `event=request_rejected` 的 JSON 记录，包含状态、预算名称、实际字节/尺寸与上限，不记录 HTML、模板数据或凭据。可据此区分 `request_bytes`、`html_bytes`、`screenshot_dimension` 和 `screenshot_pixels`，避免仅凭 413 猜测原因。

不再等待 `networkidle`：轮询/统计连接不会导致整个请求失败。外部图片、样式、字体有共享等待预算，超时后停止资源加载并截取已有内容；依赖未加载完成资源的内容可能缺失。外部脚本阻塞 DOM 就绪或执行死循环时，总超时仍会返回 504。超大图片解码和任意网页 JavaScript 仍可能消耗较多内存，因此像素保护需要结合 Kubernetes 内存上限使用。

`/healthz` 只检查进程/event loop 存活，不能用于依赖就绪判断。`/readyz` 返回 renderer、S3、Redis 状态；浏览器可用且 S3/Redis 至少一个在线时就绪，缓存可继续服务已有图片。依赖每 10 秒复查，MinIO/Redis 离线不使服务在导入时退出。推荐初始每 Pod 请求 `1Gi`、上限 `3Gi`，将副本按 hostname 分散，观察实际峰值后调整。不要使用 HPA 在同一个节点无限堆积无内存请求的 Chromium Pod。

Docker 镜像锁定 Python 摘要与 `uv.lock` 导出的依赖，以 UID/GID 10001 和 tini 运行。凭据通过环境变量或 Secret 提供。更新依赖后用 `uv export --frozen --no-dev --format requirements-txt --no-hashes --output-file requirements.txt` 重新生成构建依赖。

测试：`uv run --frozen pytest tests -q`。安装锁定浏览器后，运行 `RUN_BROWSER_TESTS=1 uv run --frozen pytest tests -q` 可额外验证真实 Chromium 长图、DPR、资源挂起、并发过载和浏览器恢复。

版本变更与升级说明见 [CHANGELOG](CHANGELOG.md)。正式版本镜像为 `ghcr.io/rc-chn/astrbot-t2i-service-distributed:0.2.0`。
