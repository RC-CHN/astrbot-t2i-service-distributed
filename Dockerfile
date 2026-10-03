FROM python:3.13.13-slim-bookworm@sha256:e4fa1f978c539608a10cdf74700ac32a3f719dfc6e8b6b6001da82deb36302a2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
WORKDIR /app

COPY requirements.txt .
# Optional build-only wheel cache; unset builds use the official PyPI index.
ARG PIP_FIND_LINKS
ARG PIP_NO_INDEX
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install-deps chromium \
    && apt-get install -y --no-install-recommends fonts-noto-cjk fonts-noto-color-emoji tini \
    && rm -rf /var/lib/apt/lists/*
# Optional build-only cache of the exact official Playwright archives.
ARG PLAYWRIGHT_DOWNLOAD_HOST
RUN playwright install --only-shell chromium \
    && rm -rf /root/.cache \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app/data/rendered \
    && chown -R app:app /app /opt/playwright

COPY --chown=app:app src ./src
COPY --chown=app:app main.py ./main.py
USER 10001:10001
EXPOSE 8999
ENTRYPOINT ["/usr/bin/tini", "--"]
# One process per Pod: each worker has its own independent render budgets/browser.
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8999", "--workers", "1", "--timeout-keep-alive", "5", "--timeout-graceful-shutdown", "45"]
