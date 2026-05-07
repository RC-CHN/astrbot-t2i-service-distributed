FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装 Playwright 的系统依赖 + 中文字体（利用 Docker 缓存层）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    fonts-noto-cjk fonts-wqy-microhei fonts-wqy-zenhei \
    fonts-arphic-uming fonts-arphic-ukai \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# 复制 requirements.txt 并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 浏览器（复用已安装的系统依赖，跳过 --with-deps）
RUN playwright install chromium

# 复制应用代码
COPY . .

# 创建 data 目录
RUN mkdir -p /app/data/rendered

# 暴露端口
EXPOSE 8999

# 启动命令
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8999"]
