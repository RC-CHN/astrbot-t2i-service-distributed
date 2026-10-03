"""Bound HTTP admission *before* Starlette buffers/parses a request body."""
import asyncio
import json
from fastapi.responses import JSONResponse
from loguru import logger

from .config import settings


def log_rejection(operation, status, reason, **details):
    # Only caller-supplied numeric budgets and fixed diagnostic messages belong
    # here. Never include HTML, template data, request URLs or credentials.
    logger.warning(json.dumps({"event": "request_rejected", "operation": operation,
        "status": status, "reason": reason, **details}, ensure_ascii=False))


class RequestBudgetMiddleware:
    def __init__(self, app, config=None):
        self.app = app
        self.config = config or settings
        self.active = 0
        self.downloads = 0

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope["method"] == "GET" and scope["path"].startswith("/text2img/data/"):
            if self.downloads >= self.config.IMAGE_DOWNLOAD_CONCURRENCY:
                return await self._error(scope, receive, send, 429, "image downloads busy; retry later",
                    budget="download_concurrency", active=self.downloads, limit=self.config.IMAGE_DOWNLOAD_CONCURRENCY)
            self.downloads += 1
            try:
                return await self.app(scope, receive, send)
            finally:
                self.downloads -= 1
        if scope["method"] != "POST" or scope["path"].rstrip("/") != "/text2img/generate":
            return await self.app(scope, receive, send)
        capacity = self.config.RENDER_CONCURRENCY + self.config.RENDER_QUEUE_SIZE
        if self.active >= capacity:
            return await self._error(scope, receive, send, 429, "renderer busy; retry later",
                budget="request_concurrency", active=self.active, limit=capacity)
        self.active += 1
        try:
            body = bytearray()
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(body) + len(chunk) > self.config.RENDER_MAX_REQUEST_BYTES:
                            return await self._error(scope, receive, send, 413, "request body exceeds configured byte limit",
                                budget="request_bytes", actual=len(body) + len(chunk), limit=self.config.RENDER_MAX_REQUEST_BYTES)
                        body.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await self._error(scope, receive, send, 408, "request body read timed out")
            delivered = False
            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()
            await self.app(scope, buffered_receive, send)
        finally:
            self.active -= 1

    @staticmethod
    async def _error(scope, receive, send, status, message, **details):
        log_rejection("download" if scope["method"] == "GET" else "generate", status, message, **details)
        response = JSONResponse(status_code=status,
            content={"code": 1, "message": message, "data": {}},
            headers={"Retry-After": "2"} if status == 429 else None)
        await response(scope, receive, send)
