"""联机冒烟测试：用 AstrBot 运行时加载插件，模拟一条群消息跑通全链路。

不依赖 AstrBot 主进程，但要用 AstrBot 自带的 Python 跑：

    E:\\AstrBot\\backend\\python\\python.exe -X utf8 tests/live_smoke.py

AstrBot 源码目录默认 ``E:\\AstrBot\\backend\\app``，可用环境变量覆盖：

    set ASTRBOT_APP=D:\\AstrBot\\backend\\app
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import types
from pathlib import Path

ASTRBOT_APP = Path(os.environ.get("ASTRBOT_APP", r"E:\AstrBot\backend\app"))
PLUGIN_DIR = Path(__file__).resolve().parents[1]
PACKAGE = PLUGIN_DIR.name

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(ASTRBOT_APP))
sys.path.insert(0, str(PLUGIN_DIR.parent))


class StubContext:
    pass


class StubEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:123456"
    message_str = ""

    def __init__(self, text: str) -> None:
        self.message_str = text

    def get_messages(self):
        return []

    def is_private_chat(self) -> bool:
        return False

    def get_sender_id(self) -> str:
        return "10001"

    def get_self_id(self) -> str:
        return "99999"

    def plain_result(self, text: str):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", chain)


def load_plugin_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(PLUGIN_DIR)]
    sys.modules[PACKAGE] = package
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.main", PLUGIN_DIR / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def collect(agen):
    return [item async for item in agen]


async def main() -> None:
    module = load_plugin_module()
    plugin = module.L4D2MapMirrorPlugin(StubContext(), {})
    await plugin.initialize()
    try:
        cases = [
            ("视频分享（B站，简介带工坊链接）", "【求生之路2】11月10日新三方图 生化危机：爆发夜 https://www.bilibili.com/video/BV1oKWM6CEVw"),
            ("直接发工坊链接", "这张图不错 https://steamcommunity.com/sharedfiles/filedetails/?id=3564279976"),
            ("非 L4D2 物品", "看看这个 https://steamcommunity.com/sharedfiles/filedetails/?id=2933705772"),
        ]
        for name, text in cases:
            print("=" * 78)
            print("CASE:", name)
            event = StubEvent(text)
            results = await collect(plugin._on_message(event))
            if not results:
                print("  <无回复>")
            for kind, payload in results:
                print(f"  [{kind}] {payload if isinstance(payload, str) else payload}")
            plugin.dedupe._seen.clear()

        print("=" * 78)
        print("CASE: /l4d2 3564279976")
        event = StubEvent("/l4d2 3564279976")
        for kind, payload in await collect(plugin.cmd_l4d2(event, "3564279976")):
            print(f"  [{kind}] {payload if isinstance(payload, str) else payload}")

        print("=" * 78)
        print("CASE: /l4d2状态")
        event = StubEvent("/l4d2状态")
        for kind, payload in await collect(plugin.cmd_status(event)):
            print(f"  [{kind}] {payload}")
    finally:
        await plugin.terminate()


if __name__ == "__main__":
    asyncio.run(main())
