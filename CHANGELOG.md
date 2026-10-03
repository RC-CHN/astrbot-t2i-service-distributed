# Changelog

## v0.2.0 — 2026-10-03

### 修复与优化

- 在解析 JSON 前限制请求数量和请求体大小；渲染默认并发 2、等待队列 4，避免突发请求无限持有 HTML、浏览器和图片内存。
- 每次渲染创建独立 browser context，异常与取消时可靠清理；浏览器按 100 次渲染或 600 秒轮换，并可在崩溃后恢复。
- 不再依赖 `networkidle`；为页面、字体、图片和截图设置有界等待，修复轮询或挂起资源导致的截图失败。
- S3/Redis 不可用时保持进程存活并自动重试；提供 `/healthz` 和 `/readyz`，区分存活与依赖就绪。
- 在有界请求内完成上传；取消时等待后台存储线程结束再释放槽位。二进制响应无需临时文件。
- 图片下载使用独立的 16 并发预算；Redis 与 S3 读取均受单图字节上限约束。
- 支持 `json: true` 和 `as_json: true`。JSON 返回成功前确认 S3 已接受图片，避免返回无法持久保存的 ID。
- Docker 固定基础镜像和依赖版本，使用非 root 用户、tini 和单 Uvicorn worker。Kubernetes 示例补充资源预算、分散调度和健康探针。
- GitHub Actions 对 Python 3.11/3.13 运行包含真实 Chromium 的回归测试，并为版本 tag 发布 amd64/arm64 镜像。

### 升级说明

- 默认 HTTP JSON 上限 **40 MiB**、HTML/模板输出上限 **32 MiB**，支持内嵌字体和图片；最终图片上限 **16 MiB / 16 MP / 单边 16384 像素**。完整参数见 [资源保护与运维](README_zh-CN.md#渲染资源保护与运维)。
- 超出预算返回 **413**，并发或排队超限返回 **429**，渲染超时返回 **504**；JSON 生成无法写入 S3 时返回 **503**。客户端应对 429/503 按 `Retry-After` 退避。
- 存储故障时直接二进制生成仍可返回图片；JSON 成功语义更严格。应用预算仍需配合容器内存上限，不能完全约束任意网页脚本和外部图片解码。
- 容器现在使用 UID/GID **10001**。挂载自定义 `tmpl` 或其他目录时，确保该用户有相应访问权限。
- 建议每个 Pod 从 requests **250m / 1 GiB**、limits **2 CPU / 3 GiB** 起步，结合实际负载调整。升级既有 Kubernetes 部署时单独更新应用配置，保留既有存储资源与凭据。

### 验证

100 项单元、API 和真实 Chromium 回归覆盖长图与 DPR、约 6.7 MiB 内嵌图片请求、并发限流、超时后恢复、浏览器崩溃恢复、存储取消清理和图片读取边界。

```sh
pip install -r requirements.txt httpx==0.28.1 pytest==9.0.2
python -m playwright install --with-deps --only-shell chromium
RUN_BROWSER_TESTS=1 python -m pytest tests -q
```

镜像：`ghcr.io/rc-chn/astrbot-t2i-service-distributed:0.2.0`。
