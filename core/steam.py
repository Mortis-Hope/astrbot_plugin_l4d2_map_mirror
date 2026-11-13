"""Steam 创意工坊 Web API 封装。

用的是免 key 的 ``ISteamRemoteStorage/GetPublishedFileDetails``：
国内可直连，返回里带 ``file_url``（创意工坊文件的 CDN 直链），
这就是"确保能下载"的第一条腿。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

STEAM_API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
L4D2_APP_ID = 550


@dataclass(slots=True)
class WorkshopItem:
    item_id: int
    ok: bool = False
    result: int = 0
    title: str = ""
    description: str = ""
    creator: str = ""
    app_id: int = 0
    creator_app_id: int = 0
    file_size: int = 0
    filename: str = ""
    file_url: str = ""
    preview_url: str = ""
    time_created: int = 0
    time_updated: int = 0
    subscriptions: int = 0
    favorited: int = 0
    views: int = 0
    tags: list[str] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    banned: int = 0
    visibility: int = 0

    @property
    def is_l4d2(self) -> bool:
        return self.app_id == L4D2_APP_ID

    @property
    def workshop_url(self) -> str:
        return f"https://steamcommunity.com/sharedfiles/filedetails/?id={self.item_id}"

    @property
    def subscribe_url(self) -> str:
        return f"steam://url/CommunityFilePage/{self.item_id}"

    @property
    def steamcmd(self) -> str:
        return (
            "steamcmd +login anonymous "
            f"+workshop_download_item {L4D2_APP_ID} {self.item_id} +quit"
        )

    @property
    def display_name(self) -> str:
        return self.title or f"工坊物品 {self.item_id}"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> WorkshopItem:
        item_id = int(data.get("publishedfileid") or 0)
        result = int(data.get("result") or 0)
        return cls(
            item_id=item_id,
            ok=result == 1,
            result=result,
            title=(data.get("title") or "").strip(),
            description=(data.get("description") or "").strip(),
            creator=str(data.get("creator") or ""),
            app_id=int(data.get("consumer_app_id") or 0),
            creator_app_id=int(data.get("creator_app_id") or 0),
            file_size=int(data.get("file_size") or 0),
            filename=(data.get("filename") or "").strip(),
            file_url=(data.get("file_url") or "").strip(),
            preview_url=(data.get("preview_url") or "").strip(),
            time_created=int(data.get("time_created") or 0),
            time_updated=int(data.get("time_updated") or 0),
            subscriptions=int(data.get("subscriptions") or 0),
            favorited=int(data.get("favorited") or 0),
            views=int(data.get("views") or 0),
            tags=[str(tag.get("tag")) for tag in (data.get("tags") or []) if isinstance(tag, dict)],
            children=[
                int(child.get("publishedfileid"))
                for child in (data.get("children") or [])
                if isinstance(child, dict) and str(child.get("publishedfileid", "")).isdigit()
            ],
            banned=int(data.get("banned") or 0),
            visibility=int(data.get("visibility") or 0),
        )


class SteamClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[int, tuple[float, WorkshopItem]] = {}
        self._ttl = 1800.0
        self._negative_ttl = 6 * 3600.0
        self._lock = asyncio.Lock()

    async def get(self, item_id: int) -> WorkshopItem | None:
        items = await self.get_many([item_id])
        return items.get(item_id)

    async def get_many(self, item_ids: Iterable[int]) -> dict[int, WorkshopItem]:
        wanted = [int(i) for i in item_ids if int(i) > 0]
        result: dict[int, WorkshopItem] = {}
        pending: list[int] = []
        now = time.time()
        for item_id in dict.fromkeys(wanted):
            cached = self._cache.get(item_id)
            if cached and now - cached[0] < (self._ttl if cached[1].ok else self._negative_ttl):
                result[item_id] = cached[1]
            else:
                pending.append(item_id)
        if not pending:
            return result

        async with self._lock:
            for start in range(0, len(pending), 100):
                chunk = pending[start : start + 100]
                try:
                    fetched = await self._fetch(chunk)
                except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
                    continue
                for item_id, item in fetched.items():
                    self._cache[item_id] = (time.time(), item)
                    result[item_id] = item
        return result

    async def _fetch(self, item_ids: list[int]) -> dict[int, WorkshopItem]:
        data: dict[str, Any] = {"itemcount": len(item_ids)}
        for index, item_id in enumerate(item_ids):
            data[f"publishedfileids[{index}]"] = str(item_id)
        response = await self.client.post(STEAM_API, data=data)
        response.raise_for_status()
        payload = response.json()
        details = ((payload.get("response") or {}).get("publishedfiledetails")) or []
        items: dict[int, WorkshopItem] = {}
        for row in details:
            if not isinstance(row, dict):
                continue
            item = WorkshopItem.from_api(row)
            if item.item_id:
                items[item.item_id] = item
        # 接口没有返回的 ID 也记一笔，避免反复请求
        for item_id in item_ids:
            if item_id not in items:
                items[item_id] = WorkshopItem(item_id=item_id, result=-1)
        return items

    def remember(self, item: WorkshopItem) -> None:
        self._cache[item.item_id] = (time.time(), item)
