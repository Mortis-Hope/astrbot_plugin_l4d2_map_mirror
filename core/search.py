"""工坊搜索：调竹烨 l4d2-workshop 搜索接口，渲染成一张结果图。"""

from __future__ import annotations

import asyncio
import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import Settings
from .mirror import ZHR_WS_API

SEARCH_WIDTH = 1200
CARD_W = 556
CARD_H = 150
MARGIN = 32
GAP = 24
HEADER_H = 100
FOOTER_H = 50

BG = (23, 26, 33)
CARD = (27, 40, 56)
CARD_HOVER = (32, 48, 66)
TEXT = (220, 226, 232)
MUTED = (143, 152, 160)
ACCENT = (102, 192, 244)
BORDER = (42, 71, 94)


@dataclass(slots=True)
class SearchItem:
    item_id: int
    title: str
    preview: str = ""
    subscriptions: int = 0
    author: str = ""
    url: str = ""


def _human_count(value: int) -> str:
    if value >= 10000:
        return f"{value / 10000:.1f}万"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    names = ["msyhbd.ttc", "msyh.ttc"] if bold else ["msyh.ttc", "msyhbd.ttc"]
    for name in names:
        path = Path(r"C:\Windows\Fonts") / name
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        if draw.textlength(current + char, font=font) <= max_width:
            current += char
            continue
        if current:
            lines.append(current)
        current = char
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if not lines:
        return [""]
    if len(lines) == max_lines:
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_width:
            last = last[:-1]
        if last != lines[-1]:
            lines[-1] = last + "…"
    return lines


class WorkshopSearch:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client

    async def search(
        self, query: str, limit: int = 6, page: int = 1
    ) -> tuple[list[SearchItem], int]:
        limit = max(1, min(8, limit))
        page = max(1, page)
        response = await self.client.get(
            f"{ZHR_WS_API}/query",
            params={"search": query, "per_page": limit, "page": page},
        )
        response.raise_for_status()
        data = response.json()
        items: list[SearchItem] = []
        for row in data.get("items") or []:
            if not isinstance(row, dict):
                continue
            try:
                item_id = int(row.get("id") or 0)
            except (TypeError, ValueError):
                continue
            title = str(row.get("title") or "").strip().replace("\n", " ")
            if item_id <= 0 or not title:
                continue
            items.append(
                SearchItem(
                    item_id=item_id,
                    title=title,
                    preview=str(row.get("preview") or ""),
                    subscriptions=int(row.get("subscriptions") or 0),
                    author=str(row.get("creator_name") or ""),
                    url=str(row.get("steam_url") or ""),
                )
            )
        total = int(data.get("total") or len(items))
        return items[:limit], total

    async def render(
        self,
        items: list[SearchItem],
        out_dir: Path,
        query: str,
        total: int = 0,
        page: int = 1,
    ) -> Path:
        previews = await asyncio.gather(
            *(self._preview(item.preview) for item in items), return_exceptions=True
        )
        images = [p if isinstance(p, Image.Image) else None for p in previews]
        out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5(f"{query}:{total}:{page}".encode()).hexdigest()[:10]
        path = out_dir / f"search_{digest}.png"
        await asyncio.to_thread(self._draw, items, images, path, query, total, page)
        return path

    async def _preview(self, url: str) -> Image.Image | None:
        candidates = []
        direct = self._direct_preview(url)
        if direct:
            candidates.append(direct)
        if url and url not in candidates:
            candidates.append(url)
        for candidate in candidates:
            try:
                response = await self.client.get(candidate, timeout=15)
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content))
                return image.convert("RGB")
            except (httpx.HTTPError, OSError, ValueError):
                continue
        return None

    @staticmethod
    def _direct_preview(url: str) -> str:
        try:
            query = parse_qs(urlparse(url).query)
        except ValueError:
            return ""
        values = query.get("url")
        return unquote(values[0]) if values else ""

    @staticmethod
    def _draw(
        items: list[SearchItem],
        previews: list[Image.Image | None],
        path: Path,
        query: str,
        total: int,
        page: int = 1,
    ) -> None:
        rows = max(1, (len(items) + 1) // 2)
        height = HEADER_H + rows * CARD_H + (rows - 1) * 16 + FOOTER_H
        image = Image.new("RGB", (SEARCH_WIDTH, height), BG)
        draw = ImageDraw.Draw(image)

        title_font = _font(30, bold=True)
        sub_font = _font(17)
        card_title_font = _font(22, bold=True)
        meta_font = _font(17)
        small_font = _font(15)
        footer_font = _font(16)

        draw.text((MARGIN, 22), f"创意工坊搜索：{query}", font=title_font, fill=TEXT)
        shown = len(items)
        draw.text(
            (MARGIN, 62),
            f"共 {total or shown} 个结果 · 第 {page} 批显示 {shown} 个 · Left 4 Dead 2",
            font=sub_font,
            fill=MUTED,
        )

        for index, item in enumerate(items):
            row, col = divmod(index, 2)
            x = MARGIN + col * (CARD_W + GAP)
            y = HEADER_H + row * (CARD_H + 16)
            draw.rounded_rectangle(
                (x, y, x + CARD_W, y + CARD_H), radius=12, fill=CARD, outline=BORDER
            )

            px, py, pw, ph = x + 16, y + 19, 200, 112
            preview = previews[index] if index < len(previews) else None
            if preview is not None:
                fitted = ImageOps.fit(preview, (pw, ph), Image.LANCZOS)
                mask = Image.new("L", (pw, ph), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    (0, 0, pw, ph), radius=10, fill=255
                )
                image.paste(fitted, (px, py), mask)
            else:
                draw.rounded_rectangle(
                    (px, py, px + pw, py + ph),
                    radius=10,
                    fill=(38, 50, 64),
                    outline=BORDER,
                )
                draw.text(
                    (px + 52, py + 44), "暂无预览", font=small_font, fill=MUTED
                )

            tx = x + 232
            max_text_width = CARD_W - 232 - 18
            lines = _wrap(draw, item.title, card_title_font, max_text_width, 2)
            ty = y + 14
            for line in lines:
                draw.text((tx, ty), line, font=card_title_font, fill=TEXT)
                ty += 28

            draw.text((tx, y + 80), f"ID {item.item_id}", font=meta_font, fill=ACCENT)
            draw.text(
                (tx, y + 106),
                f"订阅 {_human_count(item.subscriptions)}",
                font=meta_font,
                fill=TEXT,
            )
            author = item.author or "未知作者"
            while author and draw.textlength(author, font=small_font) > max_text_width:
                author = author[:-1]
            draw.text((tx, y + 129), f"作者 {author}", font=small_font, fill=MUTED)

        footer = "发送 /解析 <工坊ID> 下载 · 发送 /搜索地图 <关键词> 继续搜"
        draw.text(
            (MARGIN, height - FOOTER_H + 10), footer, font=footer_font, fill=MUTED
        )
        image.save(path, format="PNG", optimize=True)
