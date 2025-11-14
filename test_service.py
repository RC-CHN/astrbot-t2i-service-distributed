import asyncio
import argparse
import sys
import os
import ssl
import tempfile
import uuid

# --- 依赖检查 ---
try:
    import aiohttp
    import certifi
except ImportError as e:
    missing_module = str(e).split("'")[-2]
    print(f"错误：缺少必要的网络请求库 '{missing_module}'。")
    print("请运行以下命令来安装它:")
    print(f"pip install aiohttp certifi")
    sys.exit(1)

# --- 核心逻辑 (模拟 AstrBot 框架的默认异步行为) ---

DEFAULT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {
            width: 800px;
            background-color: white;
            padding: 20px;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif, "Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji";
        }
        .text-container {
            white-space: pre-wrap;
            word-wrap: break-word;
            font-size: 24px;
        }
        .footer {
            margin-top: 20px;
            text-align: center;
            font-size: 16px;
            color: #888;
        }
    </style>
</head>
<body>
    <div class="text-container">{{text}}</div>
    <div class="footer">Powered by AstrBot {{version}}</div>
</body>
</html>
"""

async def download_image_from_response(response: aiohttp.ClientResponse) -> str:
    """从 aiohttp 响应中读取图片数据并保存到临时文件。"""
    if response.status != 200:
        body = await response.text()
        raise Exception(f"下载图片失败，HTTP 状态码: {response.status}, 响应: {body}")

    content_type = response.headers.get("Content-Type", "image/png")
    extension = content_type.split("/")[-1] or "png"
    
    temp_dir = tempfile.gettempdir()
    file_path = os.path.join(temp_dir, f"t2i-async-test-{uuid.uuid4()}.{extension}")

    with open(file_path, "wb") as f:
        while True:
            chunk = await response.content.read(1024)
            if not chunk:
                break
            f.write(chunk)
    
    return file_path


class AsyncEndpointTester:
    def __init__(self, base_url: str):
        self.endpoint_url = self._clean_url(base_url)
        print(f"[*] 准备向此地址发送请求: {self.endpoint_url}")

    def _clean_url(self, url: str):
        """清理并格式化 URL"""
        url = url.removesuffix("/")
        if not url.endswith("text2img"):
            url += "/text2img"
        return url

    async def render(self, text: str) -> str:
        """
        执行异步渲染请求，获取ID，然后下载图片。
        返回保存的图片路径。
        """
        post_data = {
            "tmpl": DEFAULT_TEMPLATE,
            "json": True,  # <--- 关键：设置为 True 以启用异步模式
            "tmpldata": {
                "text": text.replace("`", "\\`"),
                "version": "async-standalone-test"
            },
            "options": {"full_page": True, "type": "jpeg", "quality": 80},
        }

        ssl_context = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        try:
            # --- 步骤 1: 发送 POST 请求获取任务 ID ---
            print("[*] 步骤 1/2: 发送 POST 请求获取任务 ID...")
            async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
                # --- 步骤 1: 发送 POST 请求获取任务 ID ---
                print("[*] 步骤 1/2: 发送 POST 请求获取任务 ID...")
                async with session.post(
                    f"{self.endpoint_url}/generate", json=post_data, timeout=30
                ) as resp:
                    if resp.status != 200:
                        # 尝试解析为 JSON 以获取更详细的错误信息
                        try:
                            error_json = await resp.json()
                            error_message = error_json.get("message", await resp.text())
                        except Exception:
                            error_message = await resp.text()
                        raise Exception(f"获取任务ID失败，HTTP {resp.status}, 响应: {error_message}")
                    
                    result_json = await resp.json()
                    task_id = result_json.get("data", {}).get("id")
                    if not task_id:
                        raise Exception(f"响应中未找到任务 ID (data.id)。收到的 JSON: {result_json}")
                
                print(f"[*] 成功获取任务 ID: {task_id}")

                # --- 步骤 2: 使用任务 ID 发送 GET 请求下载图片 ---
                image_url = f"{self.endpoint_url}/data/{task_id.replace('data/', '', 1)}"
                print(f"[*] 步骤 2/2: 发送 GET 请求到 {image_url} 下载图片...")
                async with session.get(image_url, timeout=60) as resp: # 下载允许更长时间
                    return await download_image_from_response(resp)

        except (aiohttp.ClientConnectorSSLError, aiohttp.ClientConnectorCertificateError) as e:
            raise Exception(f"SSL 证书验证失败。请检查您的服务证书。错误: {e}")
        except asyncio.TimeoutError:
            raise Exception("请求超时。请检查您的服务是否响应缓慢或网络连接有问题。")
        except aiohttp.ClientConnectorError as e:
            raise Exception(f"连接错误。无法连接到 {self.endpoint_url}。错误: {e}")


async def main():
    parser = argparse.ArgumentParser(
        description="AstrBot T2I Endpoint 异步模式测试脚本",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--endpoint", type=str, required=True,
        help="要测试的 T2I endpoint URL。\n例如: http://127.0.0.1:8080"
    )
    parser.add_argument(
        "--text", type=str,
        default="Hello, AstrBot! This is a test for the ASYNC endpoint script.",
        help="要渲染的文本内容。"
    )
    args = parser.parse_args()

    print(f"[*] 正在初始化异步测试器，目标 Endpoint: {args.endpoint}")
    tester = AsyncEndpointTester(base_url=args.endpoint)

    try:
        image_path = await tester.render(text=args.text)
        
        print("\n" + "="*30)
        print("✅ 异步模式测试成功！")
        print(f"图片已成功下载并保存到: {image_path}")
        print("="*30)

    except Exception as e:
        print("\n" + "="*30)
        print("❌ 异步模式测试失败！")
        print(f"在测试过程中发生错误: {e}")
        print("="*30)
        print("\n请检查以下几点:")
        print("1. 确保您的 T2I 服务正在运行。")
        print(f"2. 确认 Endpoint URL '{args.endpoint}' 是否正确且可以访问。")
        print("3. 确认您的服务能正确处理 'json: true' 的请求并返回任务 ID。")
        print("4. 确认使用任务 ID 可以成功获取到图片。")
        print("5. 查看您的 T2I 服务日志以获取更详细的错误信息。")

if __name__ == "__main__":
    print("="*50)
    print(" AstrBot Text-to-Image (T2I) Endpoint 异步模式测试工具")
    print("="*50)
    print("此脚本模拟框架的默认行为，通过两步操作测试 T2I 服务：")
    print("1. 发送 POST 请求获取任务 ID。")
    print("2. 发送 GET 请求下载图片。")
    print(f"用法: python {os.path.basename(__file__)} --endpoint <您的服务URL>")
    print("-"*50)
    
    asyncio.run(main())