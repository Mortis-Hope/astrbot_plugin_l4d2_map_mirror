"""镜像线路：直链、竹烨工坊解析、自定义反代。

设计目标是"至少有一条真的能下"：

* ``cdn``      —— Steam 官方 CDN 直链，国内多数网络可直连，实测最好用；
* ``zhrradiant`` —— 竹烨柠檬的工坊解析接口，既能在直链缺失时补链，
                   又能给出地图依赖（材质包 / Part 系列）；
* ``custom``   —— 用户自建反代（仓库里附了 Cloudflare Worker 模板）。

每条线路都会做一次轻量探测（Range GET 4 字节，顺便校验 VPK 文件头），
探测结果缓存 15 分钟，避免每次都打一遍网络。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from .config import Settings
from .steam import L4D2_APP_ID, SteamClient, WorkshopItem

ZHR_TOOL_PAGE = "https://zhrradiant.com/tools/workshop-accelerator"
ZHR_AJAX = "https://zhrradiant.com/wp-admin/admin-ajax.php"
ZHR_WS_API = "https://zhrradiant.com/wp-json/l4d2-workshop/v1"
_NONCE_RE = re.compile(r'NONCE\s*=\s*"([0-9a-zA-Z]{6,})"')
_CONTENT_RANGE_RE = re.compile(r"bytes\s+\d+-\d+/(\d+)")
_VPK_MAGIC = b"\x34\x12\xaa\x55"  # VPK 0x55AA1234 的小端字节序


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    status: int = 0
    size: int = 0
    vpk: bool = False


@dataclass(slots=True)
class MirrorLink:
    label: str
    url: str
    kind: str = "page"  # page | direct | command
    ok: bool | None = None
    note: str = ""


@dataclass(slots=True)
class ResolvedItem:
    item: WorkshopItem
    direct_url: str = ""
    direct_ok: bool | None = None
    direct_is_vpk: bool = False
    direct_size: int = 0
    author_name: str = ""
    mirrors: list[MirrorLink] = field(default_factory=list)
    dependencies: list[WorkshopItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class MirrorService:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, steam: SteamClient
    ) -> None:
        self.settings = settings
        self.client = client
        self.steam = steam
        self._probe_cache: dict[str, tuple[float, ProbeResult]] = {}
        self._probe_ttl = 900.0
        self._zr_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
        self._zr_ttl = 1800.0
        self._zr_ws_cache: dict[int, tuple[float, dict[str, Any]]] = {}
        self._zr_ws_ttl = 1800.0
        self._nonce: str = ""
        self._nonce_at: float = 0.0
        self._nonce_lock = asyncio.Lock()

    # ------------------------------------------------------------ 对外入口

    async def zhr_workshop_detail(self, item_id: int) -> dict[str, Any] | None:
        """竹烨 l4d2-workshop 详情接口，带 required_items/children。"""
        if not self.settings.use_zhrradiant:
            return None
        cached = self._zr_ws_cache.get(item_id)
        if cached and time.time() - cached[0] < self._zr_ws_ttl:
            return cached[1]
        try:
            response = await self.client.get(
                f"{ZHR_WS_API}/detail", params={"id": str(item_id)}
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        self._zr_ws_cache[item_id] = (time.time(), data)
        return data

    @staticmethod
    def _zhr_workshop_item(
        data: Any, fallback_id: int = 0
    ) -> WorkshopItem | None:
        if not isinstance(data, dict):
            return None
        url = str(data.get("file_url") or data.get("url") or "").strip()
        if not url:
            return None
        try:
            item_id = int(data.get("id") or fallback_id or 0)
        except (TypeError, ValueError):
            item_id = int(fallback_id or 0)
        if item_id <= 0:
            return None
        size = 0
        try:
            size = int(data.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if not size:
            match = re.search(
                r"([\d.]+)\s*([KMG])B", str(data.get("file_size_human") or ""), re.I
            )
            if match:
                unit = {"K": 1024, "M": 1024**2, "G": 1024**3}[match.group(2).upper()]
                size = int(float(match.group(1)) * unit)
        title = str(data.get("title") or "").strip().replace("\n", " ")
        return WorkshopItem(
            item_id=item_id,
            ok=True,
            result=1,
            title=title,
            creator=str(data.get("author_name") or data.get("creator") or "").strip(),
            app_id=L4D2_APP_ID,
            file_size=size,
            filename=f"{item_id}.vpk",
            file_url=url,
            preview_url=str(data.get("preview") or data.get("preview_url") or "").strip(),
        )

    async def zhr_bundle_items(self, item_id: int) -> list[WorkshopItem]:
        """返回主物品 + 必需物品/合集子物品；用于多 VPK 打包。"""
        detail = await self.zhr_workshop_detail(item_id)
        if not detail or detail.get("is_unavailable"):
            return []
        rows: list[WorkshopItem] = []
        seen: set[int] = set()
        main = self._zhr_workshop_item(detail, item_id)
        if main is not None:
            rows.append(main)
            seen.add(main.item_id)
        for key in ("children", "children_page_items", "required_items"):
            for row in detail.get(key) or []:
                child = self._zhr_workshop_item(
                    row, int(row.get("id") or 0) if isinstance(row, dict) else 0
                )
                if child is not None and child.item_id not in seen:
                    rows.append(child)
                    seen.add(child.item_id)
        for raw_id in (detail.get("children_ids") or [])[:10]:
            if len(rows) >= 10:
                break
            try:
                child_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if child_id in seen:
                continue
            child_detail = await self.zhr_workshop_detail(child_id)
            child = self._zhr_workshop_item(child_detail, child_id)
            if child is not None:
                rows.append(child)
                seen.add(child.item_id)
        return rows

    async def zhr_item(self, item_id: int) -> WorkshopItem | None:
        """Steam API 不可用时，用竹烨镜像兜底拼一个工坊物品。"""
        if not self.settings.use_zhrradiant:
            return None
        detail = await self.zhr_workshop_detail(item_id)
        item = self._zhr_workshop_item(detail, item_id) if detail else None
        if item is not None:
            return item
        try:
            rows = await self._zhr_lookup(item_id)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            return None
        if not rows:
            return None
        row = rows[0]
        url = str(row.get("url") or "").strip()
        if not url:
            return None
        try:
            size = int(row.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        return WorkshopItem(
            item_id=item_id,
            ok=True,
            result=1,
            title=str(row.get("title") or "").strip(),
            creator=str(row.get("author_name") or "").strip(),
            app_id=L4D2_APP_ID,
            file_size=size,
            filename=f"{item_id}.vpk",
            file_url=url,
            preview_url=str(row.get("preview") or "").strip(),
        )

    async def resolve(self, item: WorkshopItem, *, verify: bool | None = None) -> ResolvedItem:
        verify = self.settings.verify_links if verify is None else verify
        resolved = ResolvedItem(item=item)
        zr_items: list[dict[str, Any]] = []

        if self.settings.use_zhrradiant:
            try:
                zr_items = await self._zhr_lookup(item.item_id)
            except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
                resolved.warnings.append("竹烨镜像接口暂时不可用")

        if zr_items:
            resolved.author_name = str(zr_items[0].get("author_name") or "")
            if not item.file_url:
                fallback = str(zr_items[0].get("url") or "")
                if fallback:
                    resolved.direct_url = fallback
                    resolved.warnings.append("Steam 未返回直链，已由镜像补链")

        if not resolved.direct_url:
            resolved.direct_url = item.file_url

        if resolved.direct_url:
            if verify:
                probe = await self.probe(resolved.direct_url)
                resolved.direct_ok = probe.ok
                resolved.direct_is_vpk = probe.vpk
                resolved.direct_size = probe.size or item.file_size
            else:
                resolved.direct_ok = None
                resolved.direct_size = item.file_size

        resolved.mirrors = await self._build_mirrors(item, resolved.direct_url, verify)
        resolved.dependencies = await self._collect_dependencies(item, zr_items)
        return resolved

    async def probe(self, url: str, *, timeout: float | None = None) -> ProbeResult:
        now = time.time()
        cached = self._probe_cache.get(url)
        if cached and now - cached[0] < self._probe_ttl:
            return cached[1]

        timeout = timeout or self.settings.verify_timeout
        result = await self._range_probe(url, timeout)
        if not result.ok:
            result = await self._head_probe(url, timeout)

        self._probe_cache[url] = (now, result)
        return result

    async def _range_probe(self, url: str, timeout: float) -> ProbeResult:
        try:
            async with self.client.stream(
                "GET",
                url,
                headers={"Range": "bytes=0-3"},
                timeout=timeout,
                follow_redirects=True,
            ) as response:
                if response.status_code not in (200, 206):
                    return ProbeResult(ok=False, status=response.status_code)
                chunk = b""
                async for part in response.aiter_bytes():
                    chunk += part
                    if len(chunk) >= 4:
                        break
                size = 0
                match = _CONTENT_RANGE_RE.search(response.headers.get("content-range", ""))
                if match:
                    size = int(match.group(1))
                elif response.status_code == 200:
                    size = int(response.headers.get("content-length") or 0)
                return ProbeResult(
                    ok=True,
                    status=response.status_code,
                    size=size,
                    vpk=chunk[:4] == _VPK_MAGIC,
                )
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            return ProbeResult(ok=False)

    async def _head_probe(self, url: str, timeout: float) -> ProbeResult:
        try:
            response = await self.client.head(url, timeout=timeout, follow_redirects=True)
            if response.status_code < 400:
                return ProbeResult(
                    ok=True,
                    status=response.status_code,
                    size=int(response.headers.get("content-length") or 0),
                )
            return ProbeResult(ok=False, status=response.status_code)
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            return ProbeResult(ok=False)

    # ------------------------------------------------------------ 镜像列表

    async def _build_mirrors(
        self, item: WorkshopItem, direct_url: str, verify: bool
    ) -> list[MirrorLink]:
        links: list[MirrorLink] = []

        if self.settings.use_zhrradiant:
            links.append(
                MirrorLink(
                    label="竹烨工坊解析（网页直下）",
                    url=ZHR_TOOL_PAGE,
                    kind="page",
                    note=f"粘贴链接或 ID {item.item_id}",
                )
            )

        parsed = urlparse(direct_url)
        values = {
            "id": str(item.item_id),
            "url": direct_url,
            "url_encoded": quote(direct_url, safe=""),
            "host": parsed.netloc,
            "path": parsed.path.lstrip("/"),
            "workshop_url": item.workshop_url,
            "filename": item.filename or f"{item.item_id}.vpk",
            "title": item.title,
        }
        for mirror in self.settings.custom_mirrors:
            if not mirror.enabled:
                continue
            url = mirror.build(values)
            ok: bool | None = None
            if verify and url.startswith("http"):
                ok = (await self.probe(url)).ok
                if not ok:
                    continue
            links.append(MirrorLink(label=mirror.name, url=url, kind="direct", ok=ok))

        return links

    async def _collect_dependencies(
        self, item: WorkshopItem, zr_items: list[dict[str, Any]]
    ) -> list[WorkshopItem]:
        if not self.settings.include_dependencies or not zr_items:
            return []

        required_ids: list[int] = []
        for raw in zr_items[0].get("required_ids") or []:
            try:
                required_ids.append(int(raw))
            except (TypeError, ValueError):
                continue
        for row in zr_items[1:]:
            try:
                dep_id = int(row.get("item_id") or 0)
            except (TypeError, ValueError):
                continue
            if row.get("is_required") and dep_id:
                required_ids.append(dep_id)

        wanted = [dep_id for dep_id in dict.fromkeys(required_ids) if dep_id != item.item_id][:5]
        if not wanted:
            return []
        details = await self.steam.get_many(wanted)
        return [details[dep_id] for dep_id in wanted if dep_id in details and details[dep_id].ok]

    # ------------------------------------------------------------ 竹烨接口

    async def _zhr_lookup(self, item_id: int) -> list[dict[str, Any]]:
        cached = self._zr_cache.get(item_id)
        if cached and time.time() - cached[0] < self._zr_ttl:
            return cached[1]

        nonce = await self._zhr_nonce()
        body = await self._zhr_post(item_id, nonce)
        if not body.get("success"):
            # nonce 可能过期：强制刷新一次再试
            self._nonce_at = 0.0
            body = await self._zhr_post(item_id, await self._zhr_nonce(force=True))
        if not body.get("success"):
            return []
        data = body.get("data")
        rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
        if rows:
            self._zr_cache[item_id] = (time.time(), rows)
        return rows

    async def _zhr_post(self, item_id: int, nonce: str) -> dict[str, Any]:
        response = await self.client.post(
            ZHR_AJAX,
            data={
                "action": "parse_steam_workshop",
                "workshop_url": f"https://steamcommunity.com/sharedfiles/filedetails/?id={item_id}",
                "nonce": nonce,
            },
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": ZHR_TOOL_PAGE,
                "Origin": "https://zhrradiant.com",
            },
        )
        body = response.json()
        return body if isinstance(body, dict) else {}

    async def _zhr_nonce(self, force: bool = False) -> str:
        async with self._nonce_lock:
            if not force and self._nonce and time.time() - self._nonce_at < 1500:
                return self._nonce
            try:
                response = await self.client.get(ZHR_TOOL_PAGE)
                match = _NONCE_RE.search(response.text)
                if match:
                    self._nonce = match.group(1)
                    self._nonce_at = time.time()
            except (httpx.HTTPError, asyncio.TimeoutError):
                pass
            return self._nonce
