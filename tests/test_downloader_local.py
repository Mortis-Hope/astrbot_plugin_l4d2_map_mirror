"""缓存下载器测试：用本地 Range 服务器模拟 Steam CDN。

覆盖：完整下载、断点续传、体积上限拦截。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from core.config import Settings  # noqa: E402
from core.downloader import CacheDownloader  # noqa: E402
from core.mirror import ResolvedItem  # noqa: E402
from core.steam import WorkshopItem  # noqa: E402

PAYLOAD = bytes(range(256)) * 2048  # 512 KB
SEEN_RANGES: list[str] = []


class RangeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        range_header = self.headers.get("Range")
        SEEN_RANGES.append(range_header or "")
        if range_header:
            start = int(range_header.split("=")[1].split("-")[0])
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        else:
            body = PAYLOAD
            self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        self.wfile.write(body)


class DownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/file"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def _item(self, size: int = len(PAYLOAD)) -> WorkshopItem:
        return WorkshopItem(item_id=42, ok=True, title="Test", filename="test_map.vpk", file_size=size)

    def _resolved(self, item: WorkshopItem) -> ResolvedItem:
        return ResolvedItem(
            item=item, direct_url=self.url, direct_ok=True, direct_size=item.file_size
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_full_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load({"cache_max_mb": 5})
            item = self._item()
            async def go():
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    downloader = CacheDownloader(settings, client, Path(tmp))
                    return await downloader.download(item, self._resolved(item))

            result = self._run(go())
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.size, len(PAYLOAD))
            self.assertEqual(result.path.read_bytes(), PAYLOAD)

    def test_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load({"cache_max_mb": 5})
            item = self._item()
            target = Path(tmp) / "42_test_map.vpk"
            part = target.with_suffix(target.suffix + ".part")
            part.write_bytes(PAYLOAD[:200_000])
            SEEN_RANGES.clear()

            async def go():
                async with httpx.AsyncClient(follow_redirects=True) as client:
                    downloader = CacheDownloader(settings, client, Path(tmp))
                    return await downloader.download(item, self._resolved(item))

            result = self._run(go())
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.path.read_bytes(), PAYLOAD)
            self.assertTrue(any(value.startswith("bytes=200000-") for value in SEEN_RANGES), SEEN_RANGES)
            self.assertFalse(part.exists())

    def test_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings.load({"cache_max_mb": 1})
            item = self._item(size=50 * 1024 * 1024)
            async def go():
                async with httpx.AsyncClient() as client:
                    downloader = CacheDownloader(settings, client, Path(tmp))
                    return await downloader.download(item, self._resolved(item))

            result = self._run(go())
            self.assertFalse(result.ok)
            self.assertIn("超过上限", result.error)


if __name__ == "__main__":
    unittest.main()
