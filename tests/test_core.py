"""离线单元测试（不联网）。

运行：``python -m unittest discover -s tests -v``
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import Settings  # noqa: E402
from core.dedupe import DedupeStore  # noqa: E402
from core.links import (  # noqa: E402
    analyze_message,
    detect_platform,
    extract_workshop_ids,
    find_urls,
)
from core.mirror import ResolvedItem  # noqa: E402
from core.render import human_count, human_size, render_item  # noqa: E402
from core.steam import WorkshopItem  # noqa: E402


class Json:
    """冒充 AstrBot 的 Json 消息段（按类名分发）。"""

    def __init__(self, data):
        self.data = data


class Plain:
    def __init__(self, text):
        self.text = text


class Reply:
    """冒充 AstrBot 的 Reply 消息段。"""

    def __init__(self, message_str="", chain=None):
        self.message_str = message_str
        self.chain = chain or []


class LinkTests(unittest.TestCase):
    def test_find_urls_strips_chinese_punctuation(self):
        text = "看这个 https://b23.tv/abc123，简介里有链接（https://steamcommunity.com/sharedfiles/filedetails/?id=123456789）。"
        urls = find_urls(text)
        self.assertIn("https://b23.tv/abc123", urls)
        self.assertIn("https://steamcommunity.com/sharedfiles/filedetails/?id=123456789", urls)

    def test_extract_workshop_ids(self):
        cases = {
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3836808875": [3836808875],
            "https://steamcommunity.com/workshop/filedetails/?id=3564279976&searchtext=x": [3564279976],
            "https://steamcommunity.com/sharedfiles/filedetails/?searchtext=x&id=121971655": [121971655],
            "steam://url/CommunityFilePage/121971655": [121971655],
            "没有链接": [],
        }
        for text, expected in cases.items():
            self.assertEqual(extract_workshop_ids(text), expected, text)

        both = (
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3836808875 "
            "https://steamcommunity.com/sharedfiles/filedetails/?id=3564279976"
        )
        self.assertEqual(extract_workshop_ids(both), [3836808875, 3564279976])

    def test_detect_platform(self):
        self.assertEqual(detect_platform("https://b23.tv/abcdef"), "bilibili")
        self.assertEqual(detect_platform("https://www.bilibili.com/video/BV1xx411c7mD"), "bilibili")
        self.assertEqual(detect_platform("https://v.douyin.com/abcDEF/"), "douyin")
        self.assertEqual(detect_platform("https://youtu.be/dQw4w9WgXcQ"), "youtube")
        self.assertIsNone(detect_platform("https://example.com/a"))

    def test_analyze_message_merges_card(self):
        card = {
            "app": "com.tencent.structmsg",
            "meta": {
                "news": {
                    "title": "求生之路2 新图",
                    "desc": "创意工坊链接：https://steamcommunity.com/sharedfiles/filedetails/?id=3836808875",
                    "jumpUrl": "https://b23.tv/xyz789",
                }
            },
        }
        ctx = analyze_message([Json(card), Plain("")], "")
        self.assertEqual(ctx.video_urls, ["https://b23.tv/xyz789"])
        self.assertEqual(ctx.workshop_ids, [3836808875])

    def test_analyze_message_plain_only(self):
        ctx = analyze_message([], "【新图】https://b23.tv/xyz789")
        self.assertEqual(ctx.video_urls, ["https://b23.tv/xyz789"])
        self.assertEqual(ctx.workshop_ids, [])

    def test_analyze_message_quoted_video(self):
        ctx = analyze_message(
            [Reply(message_str="【求生之路2】新图 https://b23.tv/abc123"), Plain("")], ""
        )
        self.assertEqual(ctx.video_urls, ["https://b23.tv/abc123"])

    def test_analyze_message_quoted_json_card(self):
        card = {"meta": {"news": {"jumpUrl": "https://b23.tv/xyz789"}}}
        ctx = analyze_message([Reply(chain=[Json(card)]), Plain("")], "")
        self.assertEqual(ctx.video_urls, ["https://b23.tv/xyz789"])

    def test_analyze_miniprogram_card(self):
        card = {
            "app": "com.tencent.miniapp_01",
            "meta": {
                "detail_1": {
                    "title": "【求生之路2】11月新图",
                    "desc": "创意工坊地图推荐",
                    "qqdocurl": "https://b23.tv/mini123",
                }
            },
            "source": "哔哩哔哩",
        }
        ctx = analyze_message([Json(card)], "")
        self.assertEqual(ctx.video_urls, ["https://b23.tv/mini123"])
        self.assertIn("求生之路", ctx.card_text)
        self.assertIn("哔哩哔哩", ctx.card_text)


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        settings = Settings.load({})
        self.assertTrue(settings.group_only)
        self.assertEqual(settings.max_items_per_message, 2)
        self.assertTrue(settings.send_file_to_group)
        self.assertTrue(settings.require_at)
        self.assertEqual(settings.upload_permission, "owner_admin")
        self.assertTrue(settings.miniprogram_enabled)
        self.assertTrue(settings.miniprogram_require_keyword)
        self.assertIn("求生之路", settings.miniprogram_keywords)
        self.assertEqual(settings.send_file_max_mb, 1024)
        self.assertEqual(settings.upload_timeout, 900)

    def test_custom_mirrors(self):
        settings = Settings.load(
            {
                "custom_mirrors": [
                    "我的反代|https://m.example.com/{id}",
                    "坏的没有分隔符",
                ]
            }
        )
        self.assertEqual(len(settings.custom_mirrors), 1)
        self.assertEqual(settings.custom_mirrors[0].name, "我的反代")
        self.assertEqual(
            settings.custom_mirrors[0].build({"id": "42"}),
            "https://m.example.com/42",
        )


class SteamModelTests(unittest.TestCase):
    def test_from_api(self):
        item = WorkshopItem.from_api(
            {
                "publishedfileid": "121971655",
                "result": 1,
                "title": "Death Row",
                "consumer_app_id": 550,
                "file_size": "168555956",
                "filename": "myl4d2addons/deathrow.vpk",
                "file_url": "https://cdn.steamusercontent.com/ugc/x/",
                "tags": [{"tag": "Campaigns"}],
            }
        )
        self.assertTrue(item.ok)
        self.assertTrue(item.is_l4d2)
        self.assertEqual(item.tags, ["Campaigns"])
        self.assertIn("workshop_download_item 550 121971655", item.steamcmd)

    def test_missing_item(self):
        item = WorkshopItem.from_api({"publishedfileid": "1", "result": 9})
        self.assertFalse(item.ok)
        self.assertFalse(item.is_l4d2)


class RenderTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(human_size(0), "未知大小")
        self.assertEqual(human_size(1024), "1.0 KB")
        self.assertEqual(human_size(146608378), "139.8 MB")

    def test_human_count(self):
        self.assertEqual(human_count(1234), "1,234")
        self.assertEqual(human_count(173284), "17.3 万")

    def test_render_contains_key_lines(self):
        item = WorkshopItem.from_api(
            {
                "publishedfileid": "3836808875",
                "result": 1,
                "title": "生化危机：爆发夜",
                "consumer_app_id": 550,
                "file_size": "146608378",
                "file_url": "https://cdn.steamusercontent.com/ugc/a/b/",
                "subscriptions": 12000,
            }
        )
        resolved = ResolvedItem(
            item=item,
            direct_url=item.file_url,
            direct_ok=True,
            direct_is_vpk=True,
            direct_size=item.file_size,
            author_name="Xue",
        )
        text = render_item(resolved, video=None, settings=Settings.load({}))
        self.assertIn("求生之路2", text)
        self.assertIn("生化危机：爆发夜", text)
        self.assertIn("VPK 文件头校验通过", text)
        self.assertIn("steam://url/CommunityFilePage/3836808875", text)
        self.assertIn("workshop_download_item 550 3836808875", text)


class DedupeTests(unittest.TestCase):
    def test_hit(self):
        store = DedupeStore(ttl_seconds=3600)
        self.assertFalse(store.hit("a"))
        self.assertTrue(store.hit("a"))
        self.assertFalse(store.hit("b"))

    def test_disabled(self):
        store = DedupeStore(ttl_seconds=0)
        self.assertFalse(store.hit("a"))
        self.assertFalse(store.hit("a"))


if __name__ == "__main__":
    unittest.main()
