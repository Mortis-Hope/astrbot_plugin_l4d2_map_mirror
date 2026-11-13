"""打包与待部署记录测试。"""

from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.archive import make_archive, safe_stem  # noqa: E402
from core.pending import PendingStore  # noqa: E402


class ArchiveTests(unittest.TestCase):
    def test_zip_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "3836808875_shwj.vpk"
            source.write_bytes(b"0" * 4096)
            out = Path(tmp) / "out"
            result = make_archive(source, out, seven_zip="")
            self.assertIn(result.fmt, {"7z", "zip"})
            self.assertTrue(result.path.is_file())
            self.assertGreater(result.size, 0)
            if result.fmt == "zip":
                with zipfile.ZipFile(result.path) as archive:
                    self.assertIn(source.name, archive.namelist())
                self.assertIn("zip", result.note.lower())

    def test_archive_uses_map_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "3836808875_shwj.vpk"
            source.write_bytes(b"0" * 4096)
            out = Path(tmp) / "out"
            result = make_archive(
                source, out, seven_zip="", name="生化危机：爆发夜"
            )
            self.assertEqual(result.path.stem, "生化危机：爆发夜")

    def test_safe_stem(self):
        self.assertEqual(safe_stem(42, "a/b:c", "x.vpk"), "a_b_c")
        self.assertEqual(safe_stem(42, "", "x.vpk"), "x")
        self.assertEqual(safe_stem(42, "", ""), "42")


class PendingTests(unittest.TestCase):
    def test_add_find_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = PendingStore(Path(tmp) / "pending.json", ttl_hours=72)
            archive = Path(tmp) / "42_map.7z"
            archive.write_bytes(b"x")
            store.add(
                group_id="100",
                item_id=42,
                title="Test Map",
                archive_path=archive,
                fmt="7z",
                size=1,
            )
            found = store.find("100")
            self.assertIsNotNone(found)
            self.assertEqual(found.item_id, 42)
            self.assertEqual(store.count("100"), 1)
            store.mark_uploaded(found)
            self.assertTrue(store.find("100").uploaded)
            self.assertIsNone(store.find("999"))

    def test_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pending.json"
            store = PendingStore(path, ttl_hours=72)
            store.add(
                group_id="1",
                item_id=7,
                title="T",
                archive_path=Path(tmp) / "7.7z",
                fmt="7z",
                size=2,
            )
            again = PendingStore(path, ttl_hours=72)
            self.assertEqual(again.count("1"), 1)


if __name__ == "__main__":
    unittest.main()
