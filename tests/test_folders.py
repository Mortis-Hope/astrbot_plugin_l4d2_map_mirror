"""群文件夹解析与持久化测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.folders import FolderResolver, FolderStore  # noqa: E402


class FakeBot:
    def __init__(self, folders=None):
        self.folders = folders or []
        self.calls = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_group_root_files":
            return {"files": [], "folders": self.folders}
        if action == "create_group_file_folder":
            self.folders.append(
                {"folder_id": "/created", "folder_name": kwargs.get("name", "")}
            )
            return {}
        return {}


class FolderTests(unittest.TestCase):
    def test_store_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "folders.json"
            store = FolderStore(path)
            store.set("123", "L4D2地图", "/abc")
            again = FolderStore(path)
            self.assertEqual(again.get("123")["id"], "/abc")
            again.reset("123")
            self.assertIsNone(again.get("123"))

    def test_resolve_existing(self):
        store = FolderStore(Path(tempfile.gettempdir()) / "none.json")
        resolver = FolderResolver(store)
        bot = FakeBot([{"folder_id": "/f1", "folder_name": "L4D2"}])
        folder_id = asyncio.run(resolver.resolve(bot, "123", "l4d2"))
        self.assertEqual(folder_id, "/f1")
        self.assertEqual(bot.calls[0][0], "get_group_root_files")

    def test_resolve_creates_missing(self):
        store = FolderStore(Path(tempfile.gettempdir()) / "none2.json")
        resolver = FolderResolver(store, auto_create=True)
        bot = FakeBot([])
        folder_id = asyncio.run(resolver.resolve(bot, "123", "新文件夹"))
        self.assertEqual(folder_id, "/created")
        actions = [call[0] for call in bot.calls]
        self.assertIn("create_group_file_folder", actions)

    def test_root_aliases(self):
        store = FolderStore(Path(tempfile.gettempdir()) / "none3.json")
        resolver = FolderResolver(store)
        bot = FakeBot([])
        for name in ("", "/", "根目录"):
            self.assertEqual(asyncio.run(resolver.resolve(bot, "1", name)), "/")


if __name__ == "__main__":
    unittest.main()
