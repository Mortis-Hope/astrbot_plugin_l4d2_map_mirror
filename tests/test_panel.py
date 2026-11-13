"""管理端客户端测试：用 httpx MockTransport 验证分片上传与部署流程。"""

from __future__ import annotations

import asyncio
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.panel as panel_module  # noqa: E402
import httpx  # noqa: E402
from core.config import Settings  # noqa: E402
from core.downloader import DownloadResult  # noqa: E402
from core.mirror import ResolvedItem  # noqa: E402
from core.panel import PanelClient, PanelDeployer, PanelError  # noqa: E402
from core.steam import WorkshopItem  # noqa: E402


def make_vpk(path: Path, map_code: str = "test_m1") -> Path:
    """构造一个只含目录树的最小 VPK v1。"""
    tree = f"bsp\x00maps\x00{map_code}\x00".encode() + struct.pack(
        "<IHHIIH", 0, 0, 0x7FFF, 0, 0, 0xFFFF
    ) + b"\x00\x00\x00"
    header = struct.pack("<III", 0x55AA1234, 1, len(tree))
    path.write_bytes(header + tree)
    return path


class FakeDownloader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[int | None] = []

    async def download(self, item, resolved, limit_mb=None):
        self.calls.append(limit_mb)
        return DownloadResult(ok=True, path=self.path, size=self.path.stat().st_size)


class PanelClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_chunk = panel_module.PANEL_CHUNK
        panel_module.PANEL_CHUNK = 1024
        self.calls: list[tuple[str, str | None]] = []

    def tearDown(self) -> None:
        panel_module.PANEL_CHUNK = self._old_chunk

    def _client(self, status: int = 200) -> tuple[PanelClient, httpx.AsyncClient]:
        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append((request.url.path, request.headers.get("authorization")))
            if status != 200:
                return httpx.Response(status, text="密码错误或授权码已失效")
            if request.url.path == "/upload/init":
                return httpx.Response(200, json={"uploadId": "u1"})
            return httpx.Response(200, json={"ok": True})

        settings = Settings.load(
            {
                "panel_enabled": True,
                "panel_url": "http://panel.test",
                "panel_token": "tok",
                "panel_concurrency": 2,
                "panel_timeout": 5,
            }
        )
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return PanelClient(settings, client), client

    def test_upload_chunks_and_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.vpk"
            path.write_bytes(b"x" * 2500)
            client, http = self._client()
            progress: list[tuple[int, int]] = []

            async def go():
                async with http:
                    await client.upload_map(path, lambda done, total: _record(progress, done, total))

            asyncio.run(go())
            paths = [path for path, _ in self.calls]
            self.assertEqual(paths.count("/upload/init"), 1)
            self.assertEqual(paths.count("/upload/chunk"), 3)
            self.assertEqual(paths.count("/upload/merge"), 1)
            self.assertEqual(progress[-1], (3, 3))
            self.assertTrue(all(auth == "Bearer tok" for _, auth in self.calls))

    def test_401_raises(self):
        client, http = self._client(status=401)

        async def go():
            async with http:
                await client.role()

        with self.assertRaises(PanelError) as ctx:
            asyncio.run(go())
        self.assertEqual(ctx.exception.status, 401)
        self.assertIn("token", str(ctx.exception))


class PanelDeployerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old_chunk = panel_module.PANEL_CHUNK
        panel_module.PANEL_CHUNK = 1024
        self.paths: list[str] = []

    def tearDown(self) -> None:
        panel_module.PANEL_CHUNK = self._old_chunk

    def test_queue_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpk = make_vpk(Path(tmp) / "map.vpk")
            item = WorkshopItem(item_id=42, ok=True, title="Test", filename="map.vpk", file_size=vpk.stat().st_size)
            resolved = ResolvedItem(item=item, direct_url="https://cdn.test/x", direct_ok=True)

            def handler(request: httpx.Request) -> httpx.Response:
                self.paths.append(request.url.path)
                if request.url.path == "/upload/init":
                    return httpx.Response(200, json={"uploadId": "u1"})
                return httpx.Response(200, json={"ok": True})

            settings = Settings.load(
                {
                    "panel_enabled": True,
                    "panel_url": "http://panel.test",
                    "panel_token": "tok",
                    "panel_after_upload": "queue",
                    "panel_queue_position": "front",
                    "panel_queue_mode": "now",
                }
            )

            async def go():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    deployer = PanelDeployer(settings, http, FakeDownloader(vpk))
                    return await deployer.run(item, resolved)

            result = asyncio.run(go())
            self.assertTrue(result.ok, result.error)
            self.assertEqual(result.map_codes, ["test_m1"])
            self.assertEqual(result.map_changed, "test_m1")
            self.assertIn("/upload/merge", self.paths)
            self.assertIn("/maps/hot-reload", self.paths)
            self.assertIn("/maps/queue/add", self.paths)
            self.assertIn("/maps/queue/start", self.paths)

    def test_upload_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            vpk = make_vpk(Path(tmp) / "map.vpk")
            item = WorkshopItem(item_id=43, ok=True, title="Test", file_size=vpk.stat().st_size)
            resolved = ResolvedItem(item=item, direct_url="https://cdn.test/x", direct_ok=True)

            def handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(401, text="bad token")

            settings = Settings.load(
                {
                    "panel_enabled": True,
                    "panel_url": "http://panel.test",
                    "panel_token": "bad",
                }
            )

            async def go():
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                    deployer = PanelDeployer(settings, http, FakeDownloader(vpk))
                    return await deployer.run(item, resolved)

            result = asyncio.run(go())
            self.assertFalse(result.ok)
            self.assertIn("token", result.error)


async def _record(target: list, done: int, total: int) -> None:
    target.append((done, total))


if __name__ == "__main__":
    unittest.main()
