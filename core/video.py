"""视频元数据抓取：B 站 / 抖音 / YouTube。

只做一件事：拿到 ``标题 + 简介``，因为工坊链接几乎都写在简介里。
拿不到时返回 ``ok=False``，让上层安静跳过，不阻塞群聊。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings
from .links import detect_platform

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_DOUYIN_ID_RE = re.compile(r"/(?:video|share/video)/(\d{6,})")
_DOUYIN_MODAL_RE = re.compile(r"[?&]modal_id=(\d{6,})")
_YT_ID_RE = re.compile(r"(?:v=|youtu\.be/|shorts/|embed/)([0-9A-Za-z_-]{11})")


@dataclass(slots=True)
class VideoInfo:
    platform: str
    url: str
    video_id: str
    title: str = ""
    desc: str = ""
    author: str = ""
    cover: str = ""
    duration: int = 0
    views: int = 0
    ok: bool = False
    error: str = ""

    @property
    def content_for_links(self) -> str:
        return "\n".join(part for part in (self.title, self.desc) if part)


class VideoService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self._cache: dict[str, tuple[float, VideoInfo]] = {}
        self._ttl = 1800.0
        self._bili_client: httpx.AsyncClient | None = None

    def _bili_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": self.settings.user_agent,
            "Referer": "https://www.bilibili.com/",
        }
        if self.settings.bilibili_cookie:
            headers["Cookie"] = self.settings.bilibili_cookie
        return headers

    def _bili(self) -> httpx.AsyncClient:
        if self._bili_client is None:
            kwargs: dict[str, Any] = {
                "timeout": httpx.Timeout(float(self.settings.request_timeout)),
                "follow_redirects": True,
            }
            proxy = self.settings.bilibili_proxy or self.settings.proxy
            if proxy:
                kwargs["proxy"] = proxy
            self._bili_client = httpx.AsyncClient(**kwargs)
        return self._bili_client

    async def close(self) -> None:
        if self._bili_client is not None:
            await self._bili_client.aclose()
            self._bili_client = None

    async def fetch(self, url: str) -> VideoInfo:
        cached = self._cache.get(url)
        if cached and time.time() - cached[0] < self._ttl:
            return cached[1]

        platform = detect_platform(url) or "unknown"
        try:
            if platform == "bilibili":
                info = await self._bilibili(url)
            elif platform == "douyin":
                info = await self._douyin(url)
            elif platform == "youtube":
                info = await self._youtube(url)
            else:
                info = VideoInfo(platform=platform, url=url, video_id=url, error="暂不支持的平台")
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
            info = VideoInfo(
                platform=platform,
                url=url,
                video_id=url,
                error=f"{type(exc).__name__}: {exc}",
            )

        if info.ok:
            self._cache[url] = (time.time(), info)
        return info

    # ------------------------------------------------------------------ B 站

    async def _bilibili(self, url: str) -> VideoInfo:
        bili = self._bili()
        headers = self._bili_headers()
        final_url = url
        if "b23.tv" in url:
            final_url = await self._resolve_redirect(
                url, client=bili, headers=headers
            )
        bvid_match = _BV_RE.search(final_url) or _BV_RE.search(url)
        aid_match = _AV_RE.search(final_url) or _AV_RE.search(url)
        if not bvid_match and not aid_match:
            return VideoInfo("bilibili", url, url, error="未识别到 BV/av 号")

        params: dict[str, Any] = {}
        if bvid_match:
            params["bvid"] = bvid_match.group(1)
            video_id = bvid_match.group(1)
        else:
            params["aid"] = aid_match.group(1)  # type: ignore[union-attr]
            video_id = f"av{aid_match.group(1)}"  # type: ignore[union-attr]

        response = await bili.get(
            "https://api.bilibili.com/x/web-interface/view",
            params=params,
            headers=headers,
        )
        payload = response.json()
        if payload.get("code") != 0:
            return VideoInfo(
                "bilibili",
                url,
                video_id,
                error=f"B站接口返回 {payload.get('code')}: {payload.get('message')}",
            )
        data = payload.get("data") or {}
        desc = data.get("desc") or ""
        if not desc:
            desc = "\n".join(
                str(item.get("raw_text", "")) for item in (data.get("desc_v2") or []) if isinstance(item, dict)
            ).strip()
        return VideoInfo(
            platform="bilibili",
            url=url,
            video_id=data.get("bvid") or video_id,
            title=data.get("title") or "",
            desc=desc,
            author=(data.get("owner") or {}).get("name", ""),
            cover=data.get("pic", ""),
            duration=int(data.get("duration") or 0),
            views=int((data.get("stat") or {}).get("view") or 0),
            ok=True,
        )

    # ------------------------------------------------------------------ 抖音

    async def _douyin(self, url: str) -> VideoInfo:
        final_url = url
        if "v.douyin.com" in url:
            final_url = await self._resolve_redirect(url, mobile=True)

        match = _DOUYIN_ID_RE.search(final_url) or _DOUYIN_MODAL_RE.search(final_url)
        if not match:
            # 短链有时直接返回带 modal_id 的页面，再退一步从 HTML 里找
            html = await self._get_text(final_url, mobile=True)
            match = _DOUYIN_MODAL_RE.search(html) or _DOUYIN_ID_RE.search(html)
        if not match:
            return VideoInfo("douyin", url, url, error="未识别到抖音视频 ID")
        video_id = match.group(1)

        html = await self._get_text(f"https://www.iesdouyin.com/share/video/{video_id}/", mobile=True)
        data = _extract_router_data(html)
        item = _first_item(data)
        if item:
            author = (item.get("author") or {}).get("nickname", "")
            cover = ""
            video = item.get("video") or {}
            cover_list = ((video.get("cover") or {}).get("url_list")) or []
            if cover_list:
                cover = cover_list[0]
            return VideoInfo(
                platform="douyin",
                url=url,
                video_id=video_id,
                title=(item.get("desc") or "").strip(),
                desc=(item.get("desc") or "").strip(),
                author=author,
                cover=cover,
                ok=True,
            )

        title = _html_title(html)
        if title:
            return VideoInfo("douyin", url, video_id, title=title, desc="", ok=True)
        return VideoInfo("douyin", url, video_id, error="抖音页面结构变化，未取到简介")

    # ---------------------------------------------------------------- YouTube

    async def _youtube(self, url: str) -> VideoInfo:
        match = _YT_ID_RE.search(url)
        video_id = match.group(1) if match else url

        info = await asyncio.to_thread(_ytdlp_extract, url)
        if info:
            return VideoInfo(
                platform="youtube",
                url=url,
                video_id=video_id,
                title=info.get("title") or "",
                desc=info.get("description") or "",
                author=info.get("uploader") or "",
                cover=info.get("thumbnail") or "",
                duration=int(info.get("duration") or 0),
                views=int(info.get("view_count") or 0),
                ok=True,
            )

        html = await self._get_text(f"https://www.youtube.com/watch?v={video_id}")
        title = _html_title(html)
        desc = _json_string_field(html, "shortDescription")
        if title or desc:
            return VideoInfo(
                platform="youtube",
                url=url,
                video_id=video_id,
                title=title,
                desc=desc,
                ok=True,
            )
        return VideoInfo("youtube", url, video_id, error="YouTube 不可达且未安装 yt-dlp")

    # ------------------------------------------------------------------ 工具

    async def _resolve_redirect(
        self,
        url: str,
        mobile: bool = False,
        *,
        client: httpx.AsyncClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> str:
        request_headers = headers or {
            "User-Agent": _mobile_ua() if mobile else self.settings.user_agent
        }
        try:
            response = await (client or self.client).get(
                url, headers=request_headers, follow_redirects=True
            )
            return str(response.url)
        except httpx.HTTPError:
            return url

    async def _get_text(self, url: str, mobile: bool = False) -> str:
        headers = {"User-Agent": _mobile_ua() if mobile else self.settings.user_agent}
        response = await self.client.get(url, headers=headers, follow_redirects=True)
        return response.text


def _mobile_ua() -> str:
    return (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )


def _html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


def _json_string_field(html: str, field: str) -> str:
    match = re.search(rf'"{field}":"((?:[^"\\]|\\.)*)"', html)
    if not match:
        return ""
    try:
        return json.loads(f'"{match.group(1)}"')
    except ValueError:
        return match.group(1)


def _extract_router_data(html: str) -> dict[str, Any]:
    """从抖音分享页里抠出 ``window._ROUTER_DATA``。"""
    marker = "_ROUTER_DATA"
    index = html.find(marker)
    if index < 0:
        return {}
    start = html.find("{", index)
    if start < 0:
        return {}
    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(html)):
        char = html[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : position + 1])
                except ValueError:
                    return {}
    return {}


def _first_item(data: dict[str, Any]) -> dict[str, Any] | None:
    """在抖音返回的嵌套结构里找到第一条视频信息。"""
    if not isinstance(data, dict):
        return None
    loader = data.get("loaderData")
    if isinstance(loader, dict):
        for value in loader.values():
            if not isinstance(value, dict):
                continue
            info = value.get("videoInfoRes") or value.get("videoInfo")
            if isinstance(info, dict):
                items = info.get("item_list") or info.get("itemList")
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items[0]
    for key in ("item_list", "itemList"):
        items = data.get(key)
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
    return None


def _ytdlp_extract(url: str) -> dict[str, Any] | None:
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return None
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:  # noqa: BLE001 - yt-dlp 抛出的异常类型五花八门
        return None
    return info if isinstance(info, dict) else None
