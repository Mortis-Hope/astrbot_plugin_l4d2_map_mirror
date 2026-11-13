"""群文件上传测试：用假 bot / 假下载器验证参数与去重，不联网。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402
from core.downloader import DownloadResult  # noqa: E402
from core.group_upload import GroupFileUploader  # noqa: E402
from core.mirror import ResolvedItem  # noqa: E402
from core.steam import WorkshopItem  # noqa: E402


class FakeDownloader:
    def __init__(self, result: DownloadResult) -> None:
        self.result = result
        self.calls: list[tuple[int, int | None]] = []

    async def download(self, item, resolved, limit_mb=None):
        self.calls.append((item.item_id, limit_mb))
        return self.result


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return {"status": "ok"}


class UploadTests(unittest.TestCase):
    def test_upload_success_and_dedupe(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "42_test.vpk"
            path.write_bytes(b"\x34\x12\xaa\x55" + b"0" * 128)
            settings = Settings.load(
                {"send_file_to_group": True, "send_file_max_mb": 10, "group_file_folder": "L4D2"}
            )
            downloader = FakeDownloader(DownloadResult(ok=True, path=path, size=path.stat().st_size))
            uploader = GroupFileUploader(settings, downloader)
            bot = FakeBot()
            messages: list[str] = []

            async def notify(umo: str, text: str) -> None:
                messages.append(text)

            item = WorkshopItem(item_id=42, ok=True, title="Test", filename="test.vpk", file_size=path.stat().st_size)
            resolved = ResolvedItem(item=item, direct_url="https://example.com/x", direct_ok=True)

            async def go():
                task = uploader.spawn(
                    bot=bot,
                    group_id="123456",
                    umo="aiocqhttp:GroupMessage:123456",
                    item=item,
                    resolved=resolved,
                    notify=notify,
                )
                assert task is not None
                await task
                return uploader.spawn(
                    bot=bot,
                    group_id="123456",
                    umo="aiocqhttp:GroupMessage:123456",
                    item=item,
                    resolved=resolved,
                    notify=notify,
                )

            second = asyncio.run(go())
            self.assertIsNone(second, "同一张图不应重复上传")
            self.assertEqual(len(bot.calls), 1)
            action, kwargs = bot.calls[0]
            self.assertEqual(action, "upload_group_file")
            self.assertEqual(kwargs["group_id"], 123456)
            self.assertEqual(kwargs["folder"], "L4D2")
            self.assertTrue(kwargs["name"].endswith(".vpk"))
            self.assertTrue(messages and messages[0].startswith("📦"))
            self.assertIn("123456", str(bot.calls[0][1]))

    def test_private_upload_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.7z"
            path.write_bytes(b"x")
            settings = Settings.load({})
            uploader = GroupFileUploader(settings, FakeDownloader(DownloadResult(ok=True, path=path)))
            bot = FakeBot()

            ok, error = asyncio.run(
                uploader.send_path(bot=bot, path=path, name="map.7z", user_id="10001")
            )
            self.assertTrue(ok, error)
            action, kwargs = bot.calls[0]
            self.assertEqual(action, "upload_private_file")
            self.assertEqual(kwargs["user_id"], 10001)
            self.assertEqual(kwargs["name"], "map.7z")

    def test_download_failure_notifies(self):
        settings = Settings.load({"send_file_to_group": True})
        downloader = FakeDownloader(DownloadResult(ok=False, error="文件 769MB 超过上限 300MB"))
        uploader = GroupFileUploader(settings, downloader)
        bot = FakeBot()
        messages: list[str] = []

        async def notify(umo: str, text: str) -> None:
            messages.append(text)

        item = WorkshopItem(item_id=7, ok=True, title="Big Map", file_size=1)
        resolved = ResolvedItem(item=item, direct_url="https://example.com/x", direct_ok=True)

        async def go():
            task = uploader.spawn(
                bot=bot,
                group_id="1",
                umo="u",
                item=item,
                resolved=resolved,
                notify=notify,
            )
            await task

        asyncio.run(go())
        self.assertEqual(bot.calls, [])
        self.assertEqual(len(messages), 1)
        self.assertIn("超过上限", messages[0])


if __name__ == "__main__":
    unittest.main()
