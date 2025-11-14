FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制 requirements.txt 并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright、Chromium 浏览器及其所有系统依赖
# 使用 --with-deps 标志来自动安装缺失的库
RUN playwright install --with-deps chromium

# 复制应用代码
COPY . .

# 创建 data 目录
RUN mkdir -p /app/data/rendered

# 暴露端口
EXPOSE 8999

# 启动命令
CMD ["uvicorn", "src.api:app", "--host", "0.0.0.0", "--port", "8999"]
