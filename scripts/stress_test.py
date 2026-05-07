#!/usr/bin/env python3
"""
Stress test for astrbot-t2i-service.

Generates concurrent requests against POST /text2img/generate and
GET /text2img/data/{id}, measures latency distribution and throughput.

Usage:
    python scripts/stress_test.py --url http://localhost:8999 --concurrency 10 --requests 100
    python scripts/stress_test.py --url http://localhost:8999 -c 20 -n 500 --json-mode
"""

import argparse
import asyncio
import os
import ssl
import sys
import time
from dataclasses import dataclass, field

try:
    import aiohttp
    import certifi
except ImportError:
    print("Missing dependencies. Install with: pip install aiohttp certifi")
    sys.exit(1)

# ── Payload templates ────────────────────────────────────────────────

SIMPLE_HTML = """<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=600"></head>
<body><h1>Stress Test</h1><p>Hello World!</p></body>
</html>"""

CDN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=800">
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  body { font-family: sans-serif; padding: 20px; }
  h1 { color: #333; }
</style>
</head>
<body>
<h1>Markdown Rendering Test</h1>
<div id="content"># Hello from CDN

This paragraph is rendered by **marked.js** loaded from jsDelivr CDN.

- Item one
- Item two
- Item three 🚀</div>
<script>
  document.getElementById('content').innerHTML =
    marked.parse(document.getElementById('content').textContent);
</script>
</body>
</html>"""

TEMPLATE_PAYLOAD = {
    "tmpl": """<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=700"></head>
<body><h1>{{ title }}</h1><p>{{ body }}</p></body>
</html>""",
    "tmpldata": {"title": "模板渲染压测", "body": "这是通过 Jinja2 模板渲染的中文内容。"},
}

# ── Data structures ──────────────────────────────────────────────────


@dataclass
class Stats:
    total: int = 0
    success: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)
    error_details: list[str] = field(default_factory=list)

    @property
    def rps(self) -> float:
        if not self.latencies:
            return 0.0
        elapsed = max(self.latencies) - min(self.latencies)
        return self.success / elapsed if elapsed > 0 else float(self.success)

    def percentile(self, p: float) -> float:
        if not self.latencies:
            return 0.0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * p / 100)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]


# ── Worker ───────────────────────────────────────────────────────────


async def worker(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict,
    stats: Stats,
    sem: asyncio.Semaphore,
    json_mode: bool,
    timeout: int,
):
    """Single request cycle: POST generate → optionally GET image."""
    async with sem:
        start = time.monotonic()
        try:
            async with session.post(
                f"{url}/text2img/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    stats.errors += 1
                    stats.error_details.append(f"POST {resp.status}: {body[:200]}")
                    stats.total += 1
                    return

                if json_mode:
                    data = await resp.json()
                    img_id = data.get("data", {}).get("id", "")
                else:
                    # File response — drain the body
                    await resp.read()
                    img_id = None

            # If json_mode, also fetch the image to exercise the GET path
            if json_mode and img_id:
                clean_id = img_id.replace("data/", "", 1)
                async with session.get(
                    f"{url}/text2img/data/{clean_id}",
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as get_resp:
                    if get_resp.status == 200:
                        await get_resp.read()
                    else:
                        stats.errors += 1
                        stats.error_details.append(
                            f"GET {get_resp.status} for {clean_id}"
                        )
                        stats.total += 1
                        return

            elapsed = time.monotonic() - start
            stats.success += 1
            stats.latencies.append(elapsed)

        except asyncio.TimeoutError:
            stats.errors += 1
            stats.error_details.append("timeout")
        except aiohttp.ClientError as e:
            stats.errors += 1
            stats.error_details.append(f"client error: {e}")
        except Exception as e:
            stats.errors += 1
            stats.error_details.append(f"unexpected: {type(e).__name__}: {e}")
        finally:
            stats.total += 1


# ── Reporter ─────────────────────────────────────────────────────────


async def reporter(stats: Stats, stop: asyncio.Event, total: int):
    """Periodic progress reporter."""
    while not stop.is_set():
        await asyncio.sleep(2)
        done = stats.success + stats.errors
        pct = done / total * 100 if total else 0
        print(
            f"\r  progress: {done}/{total} ({pct:.0f}%)  "
            f"ok={stats.success}  err={stats.errors}",
            end="",
            flush=True,
        )
    print()  # final newline


# ── Main ─────────────────────────────────────────────────────────────


async def run(args):
    print(f"🎯  Target:   {args.url}")
    print(f"🔧  Mode:     {'json (POST+GET)' if args.json_mode else 'file (POST only)'}")
    print(f"📦  Payload:  {args.payload}")
    print(f"⚡  Concurrency: {args.concurrency}")
    print(f"🔢  Requests:    {args.requests}")
    print(f"⏱️   Timeout:    {args.timeout}s")
    print()

    # Build payload
    if args.payload == "simple":
        payload = {"html": SIMPLE_HTML, "json": args.json_mode}
    elif args.payload == "cdn":
        payload = {"html": CDN_HTML, "json": args.json_mode}
    elif args.payload == "template":
        payload = {**TEMPLATE_PAYLOAD, "json": args.json_mode}
    else:
        print(f"Unknown payload: {args.payload}")
        sys.exit(1)

    if not args.json_mode:
        payload.pop("json", None)
        # For file mode, add minimal options
        payload["options"] = {"type": "png", "full_page": True}

    stats = Stats()
    sem = asyncio.Semaphore(args.concurrency)
    stop = asyncio.Event()

    ssl_context = ssl.create_default_context(cafile=certifi.where())
    connector = aiohttp.TCPConnector(
        ssl=ssl_context,
        limit=args.concurrency * 2,
        limit_per_host=args.concurrency * 2,
    )

    t0 = time.monotonic()

    async with aiohttp.ClientSession(connector=connector) as session:
        reporter_task = asyncio.create_task(reporter(stats, stop, args.requests))

        tasks = [
            worker(session, args.url, payload, stats, sem, args.json_mode, args.timeout)
            for _ in range(args.requests)
        ]
        await asyncio.gather(*tasks)

        stop.set()
        await reporter_task

    elapsed_total = time.monotonic() - t0

    # ── Results ──────────────────────────────────────────────────
    print()
    print("═" * 56)
    print("  Results")
    print("═" * 56)
    print(f"  Total requests:     {stats.total}")
    print(f"  Successful:         {stats.success}")
    print(f"  Errors:             {stats.errors}")
    print(f"  Duration:           {elapsed_total:.2f}s")
    print(f"  Throughput (RPS):   {stats.success / elapsed_total:.1f}")
    print("─" * 56)
    if stats.latencies:
        sorted_lat = sorted(stats.latencies)
        print(f"  Latency (ms):")
        print(f"    min:  {sorted_lat[0]*1000:7.1f}")
        print(f"    avg:  {sum(sorted_lat)/len(sorted_lat)*1000:7.1f}")
        print(f"    p50:  {stats.percentile(50)*1000:7.1f}")
        print(f"    p95:  {stats.percentile(95)*1000:7.1f}")
        print(f"    p99:  {stats.percentile(99)*1000:7.1f}")
        print(f"    max:  {sorted_lat[-1]*1000:7.1f}")
    else:
        print("  (no successful requests)")
    print("─" * 56)

    if stats.error_details:
        # Show up to 5 distinct error types
        from collections import Counter

        error_counts = Counter(stats.error_details)
        print(f"  Error types ({len(error_counts)} unique):")
        for msg, count in error_counts.most_common(5):
            print(f"    [{count:4d}] {msg[:120]}")
        if len(error_counts) > 5:
            print(f"    ... and {len(error_counts) - 5} more")
    print("═" * 56)

    return 0 if stats.errors == 0 else 1


def main():
    parser = argparse.ArgumentParser(
        description="Stress test for astrbot-t2i-service",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --url http://localhost:8999 -c 10 -n 100
  %(prog)s --url http://my-cluster:8999 -c 50 -n 1000 --json-mode --payload cdn
        """,
    )
    parser.add_argument(
        "--url", default="http://localhost:8999", help="Target service URL"
    )
    parser.add_argument(
        "-c", "--concurrency", type=int, default=10, help="Concurrent workers (default: 10)"
    )
    parser.add_argument(
        "-n", "--requests", type=int, default=100, help="Total requests (default: 100)"
    )
    parser.add_argument(
        "--timeout", type=int, default=60, help="Per-request timeout in seconds (default: 60)"
    )
    parser.add_argument(
        "--json-mode",
        action="store_true",
        help="Use json=true mode and also exercise the GET /data/{id} path",
    )
    parser.add_argument(
        "--payload",
        choices=["simple", "cdn", "template"],
        default="simple",
        help="Payload type: simple (plain HTML), cdn (with CDN JS), template (Jinja2) (default: simple)",
    )
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
