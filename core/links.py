"""消息里的链接抽取与 Steam 工坊 ID 识别。

群聊里分享 B 站视频通常有两种形态：

1. 纯文本消息，例如 ``【标题】 https://b23.tv/xxxx``；
2. QQ 结构化卡片（Json 消息段），链接藏在 ``meta.news.jumpUrl`` 里，
   卡片还会带一段 ``meta.news.desc`` —— 有时简介原文就藏在里面。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# URL 里允许出现的字符；遇到空白、中英文标点即截断。
_URL_RE = re.compile(r"https?://[^\s<>'\"\u3000-\u303f\uff00-\uffef]+", re.IGNORECASE)
# 少数分享文案会省略协议头
_BARE_URL_RE = re.compile(
    r"(?<![\w./])((?:b23\.tv|v\.douyin\.com|www\.douyin\.com|bilibili\.com|"
    r"youtu\.be|www\.youtube\.com|steamcommunity\.com)/[^\s<>'\"\u3000-\u303f\uff00-\uffef]+)",
    re.IGNORECASE,
)
_TRAILING = ".,;:!?)]}>，。、；：！？）】》」』”’…"

# 工坊链接 / steam:// 协议
_WORKSHOP_ID_RE = re.compile(
    r"(?:"
    r"steamcommunity\.com/(?:sharedfiles|workshop)/filedetails/?"
    r"\?(?:[^\s#]*?&(?:amp;)?)?id=(\d{5,})"
    r"|steam://url/CommunityFilePage/(\d{5,})"
    r"|steamcommunity\.com/sharedfiles/filedetails/(\d{5,})"
    r")",
    re.IGNORECASE,
)

_BILIBILI_RE = re.compile(
    r"(?:bilibili\.com/video/(BV[0-9A-Za-z]{10}|av\d+)|b23\.tv/[0-9A-Za-z]+)",
    re.IGNORECASE,
)
_DOUYIN_RE = re.compile(
    r"(?:v\.douyin\.com/[0-9A-Za-z_-]+|douyin\.com/(?:video|share/video)/\d+|iesdouyin\.com/share/video/\d+)",
    re.IGNORECASE,
)
_YOUTUBE_RE = re.compile(
    r"(?:youtu\.be/[0-9A-Za-z_-]{11}|youtube\.com/(?:watch\?[^\s#]*v=|shorts/|embed/)[0-9A-Za-z_-]{11})",
    re.IGNORECASE,
)
_ACFUN_RE = re.compile(r"acfun\.cn/(?:v|bangumi)/[^\s?#]+", re.IGNORECASE)


@dataclass(slots=True)
class MessageContext:
    """一条群消息里与解析相关的全部信息。"""

    urls: list[str] = field(default_factory=list)
    video_urls: list[str] = field(default_factory=list)
    workshop_ids: list[int] = field(default_factory=list)
    text: str = ""
    card_text: str = ""

    @property
    def all_text(self) -> str:
        return "\n".join(part for part in (self.text, self.card_text) if part)


def clean_url(url: str) -> str:
    url = url.strip().strip("<>\"'")
    while url and url[-1] in _TRAILING:
        url = url[:-1]
    return url


def find_urls(text: str) -> list[str]:
    """从任意文本中提取 http(s) 链接（去重、去尾部标点）。"""
    if not text:
        return []
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        found.append(clean_url(match.group(0)))
    for match in _BARE_URL_RE.finditer(text):
        found.append(clean_url("https://" + match.group(1)))
    return _unique(found)


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def detect_platform(url: str) -> str | None:
    """返回视频平台标识，非视频链接返回 None。"""
    if _BILIBILI_RE.search(url) or "b23.tv" in url.lower():
        return "bilibili"
    if _DOUYIN_RE.search(url) or "v.douyin.com" in url.lower():
        return "douyin"
    if _YOUTUBE_RE.search(url):
        return "youtube"
    if _ACFUN_RE.search(url):
        return "acfun"
    return None


def extract_workshop_ids(text: str) -> list[int]:
    """从文本中提取 Steam 创意工坊物品 ID（保持出现顺序）。"""
    if not text:
        return []
    ids: list[int] = []
    for match in _WORKSHOP_ID_RE.finditer(text):
        raw = next((group for group in match.groups() if group), None)
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return ids


def is_workshop_url(url: str) -> bool:
    return "steamcommunity.com" in url and (
        "filedetails" in url or "sharedfiles" in url or "workshop" in url
    )


def _walk_strings(node: Any, depth: int = 0) -> Iterable[str]:
    if depth > 8:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _walk_strings(value, depth + 1)
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk_strings(item, depth + 1)


def _card_strings(data: Any) -> list[str]:
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return [data]
    return list(_walk_strings(data))


def analyze_message(messages: list[Any] | None, message_str: str) -> MessageContext:
    """解析消息链，汇总链接、卡片文本与工坊 ID。"""
    ctx = MessageContext(text=message_str or "")

    card_parts: list[str] = []
    for component in messages or []:
        name = type(component).__name__
        if name == "Json":
            data = getattr(component, "data", None)
            card_parts.extend(_card_strings(data))
        elif name in {"Plain", "Unknown"}:
            text = getattr(component, "text", "") or ""
            if text:
                ctx.text = f"{ctx.text}\n{text}" if ctx.text else text
        elif name == "Reply":
            # 引用消息：链接常在别处发的视频卡片里，@机器人只是叫它处理
            quoted = getattr(component, "message_str", "") or ""
            if quoted:
                ctx.text = f"{ctx.text}\n{quoted}" if ctx.text else quoted
            for sub in getattr(component, "chain", None) or []:
                sub_name = type(sub).__name__
                if sub_name in {"Plain", "Unknown"}:
                    sub_text = getattr(sub, "text", "") or ""
                    if sub_text:
                        ctx.text = f"{ctx.text}\n{sub_text}" if ctx.text else sub_text
                elif sub_name == "Json":
                    card_parts.extend(_card_strings(getattr(sub, "data", None)))

    ctx.card_text = "\n".join(part for part in card_parts if part)
    ctx.urls = _unique(find_urls(ctx.text) + find_urls(ctx.card_text))
    ctx.video_urls = [url for url in ctx.urls if detect_platform(url)]
    ctx.workshop_ids = extract_workshop_ids("\n".join((ctx.text, ctx.card_text)))
    return ctx
