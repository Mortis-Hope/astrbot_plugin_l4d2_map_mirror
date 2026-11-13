"""AstrBot 求生之路2地图镜像助手。

群友分享 B 站 / 抖音 / YouTube 的求2地图视频时：

1. 抓视频标题与简介；
2. 从简介里找出 Steam 创意工坊链接；
3. 查工坊详情，确认是 L4D2（AppID 550）的地图；
4. 回复官方页 + 实测可用的 CDN 直链 + 备用镜像 + steamcmd 命令。
"""

import asyncio
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import At, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .core.archive import archive_member_name, find_7z, make_archive_async, safe_stem
from .core.config import Settings
from .core.dedupe import DedupeStore
from .core.downloader import CacheDownloader, cache_filename
from .core.folders import FolderResolver, FolderStore
from .core.group_upload import GroupFileUploader
from .core.links import (
    analyze_message,
    detect_platform,
    extract_workshop_ids,
    find_urls,
)
from .core.mirror import MirrorService, ResolvedItem
from .core.panel import PanelDeployer, PanelError
from .core.pending import PendingMap, PendingStore
from .core.render import (
    human_size,
    render_help,
    render_invalid,
    render_item,
    render_missing_link,
    render_not_l4d2,
)
from .core.search import WorkshopSearch
from .core.steam import L4D2_APP_ID, SteamClient, WorkshopItem
from .core.video import VideoInfo, VideoService

PLUGIN_NAME = "astrbot_plugin_l4d2_map_mirror"
COMMAND_NAMES = {
    "解析",
    "机器人",
    "l4d2bot",
    "l4d2",
    "l4d2状态",
    "l4d2镜像",
    "地图",
    "工坊",
    "发送地图",
    "下载地图",
    "搜索",
    "搜图",
    "搜索地图",
    "换一批",
    "下一批",
    "换页",
    "设置群文件夹",
    "群文件夹",
    "上传地图",
    "部署地图",
}

_L4D2_HINTS = (
    "求生之路",
    "l4d2",
    "left 4 dead",
    "求2",
    "三方图",
    "三方地图",
)

_HELP_TEXT = """🤖 求生之路2地图助手
/解析 <链接> [文件夹]
/解析（引用/最近链接）
/上传地图 [序号/文件名/ID]
/设置群文件夹 <名称>
/解析 状态
/解析 只看 <链接/ID>
/搜索地图 <地图名>
/换一批"""


def _as_int(value: str) -> int | str:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def _parse_item_id(text: str) -> int | None:
    ids = extract_workshop_ids(text or "")
    if ids:
        return ids[0]
    value = (text or "").strip()
    return int(value) if value.isdigit() else None


def _safe_file_name(name: str) -> str:
    name = Path(name or "").name
    cleaned = "".join("_" if char in '\\/:*?"<>|' else char for char in name)
    cleaned = cleaned.strip(" .")
    return cleaned or "archive.bin"


def _event_rest(event: AstrMessageEvent, command_names: set[str]) -> str:
    """从 event.message_str 里取命令名之后的完整文本。

    AstrBot 某些版本只把第一个空格前的 token 传给 arg，标题+链接会被截断；
    这里直接读原消息，保证拿到整段参数。
    """
    text = (getattr(event, "message_str", "") or "").strip()
    if not text:
        return ""
    head, _, rest = text.partition(" ")
    head = head.lstrip("/／")
    if head in command_names:
        return rest.strip()
    return ""


def _arg_text(
    event: AstrMessageEvent, arg: object, command_names: set[str]
) -> str:
    value = str(arg or "").strip()
    rest = _event_rest(event, command_names)
    if rest and (not value or len(rest) >= len(value)):
        return rest
    return value


def _panel_upload_name(name: str) -> str:
    """上传给管理端时去掉开头的工坊 ID，避免 maps 里出现 3830961032_xxx.vpk。"""
    path = Path(name or "")
    stem = path.stem
    stripped = stem.lstrip("0123456789")
    if stripped != stem:
        stripped = stripped.lstrip("_- .")
        if stripped:
            stem = stripped
    return f"{stem or path.stem}{path.suffix}"


class L4D2MapMirrorPlugin(Star):
    def __init__(self, context: Context, config=None) -> None:
        super().__init__(context)
        self.settings = Settings.load(config)
        self._client: httpx.AsyncClient | None = None
        self._ready = False
        self.steam: SteamClient | None = None
        self.videos: VideoService | None = None
        self.mirrors: MirrorService | None = None
        self.downloader: CacheDownloader | None = None
        self.uploader: GroupFileUploader | None = None
        self.panel: PanelDeployer | None = None
        self.pending: PendingStore | None = None
        self.archive_dir: Path | None = None
        self.folders: FolderStore | None = None
        self.folder_resolver: FolderResolver | None = None
        self.search_service: WorkshopSearch | None = None
        self.search_dir: Path | None = None
        self._panel_tasks: set[asyncio.Task] = set()
        self._recent_cards: dict[str, list[tuple[float, Any]]] = {}
        self._search_state: dict[str, tuple[str, int, set[int], float]] = {}
        self.dedupe = DedupeStore(self.settings.dedupe_ttl_hours * 3600)

    # ------------------------------------------------------------ 生命周期

    async def initialize(self) -> None:
        if self._ready:
            return
        timeout = httpx.Timeout(
            connect=10.0,
            read=float(self.settings.request_timeout),
            write=30.0,
            pool=10.0,
        )
        kwargs = {
            "timeout": timeout,
            "follow_redirects": True,
            "headers": {"User-Agent": self.settings.user_agent},
            "limits": httpx.Limits(max_connections=20, max_keepalive_connections=10),
        }
        if self.settings.proxy:
            kwargs["proxy"] = self.settings.proxy
        self._client = httpx.AsyncClient(**kwargs)

        self.steam = SteamClient(self.settings, self._client)
        self.videos = VideoService(self.settings, self._client)
        self.mirrors = MirrorService(self.settings, self._client, self.steam)

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        cache_dir = data_dir / "cache"
        self.archive_dir = data_dir / "archives"
        self.search_dir = data_dir / "search"
        self.search_service = WorkshopSearch(self.settings, self._client)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.downloader = CacheDownloader(self.settings, self._client, cache_dir)
        self.uploader = GroupFileUploader(self.settings, self.downloader)
        self.pending = PendingStore(data_dir / "pending.json", self.settings.archive_ttl_hours)
        self.pending.prune()
        self.folders = FolderStore(data_dir / "folders.json")
        self.folder_resolver = FolderResolver(
            self.folders, auto_create=self.settings.folder_auto_create
        )
        if self.settings.panel_ready():
            self.panel = PanelDeployer(self.settings, self._client, self.downloader)
            logger.info(f"[l4d2] 管理端已配置：{self.settings.panel_url}")
        if self.settings.auto_cache or self.settings.send_file_to_group:
            removed = self.downloader.cleanup()
            if removed:
                logger.info(f"[l4d2] 清理过期缓存 {removed} 个文件")
        self._warn_upload_platform()
        self._ready = True
        logger.info(f"[l4d2] 插件就绪，缓存目录：{cache_dir}")

    def _warn_upload_platform(self) -> None:
        """传群文件依赖 OneBot 的 upload_group_file，官方 QQ 机器人做不到。"""
        if not self.settings.send_file_to_group:
            return
        try:
            platforms = self.context.platform_manager.get_insts()
            names = {p.meta().name for p in platforms}
        except Exception:  # noqa: BLE001
            return
        if "aiocqhttp" not in names:
            logger.warning(
                "[l4d2] 已开启传群文件，但当前没有 aiocqhttp(NapCat/Lagrange) 平台，"
                "该功能不会生效；官方 QQ 机器人(qq_official) 无法上传群文件。"
            )

    async def terminate(self) -> None:
        for task in list(self._panel_tasks):
            task.cancel()
        if self._panel_tasks:
            await asyncio.gather(*self._panel_tasks, return_exceptions=True)
        self._panel_tasks.clear()
        if self.uploader is not None:
            await self.uploader.shutdown()
        if self.downloader is not None:
            await self.downloader.shutdown()
        if self.videos is not None:
            await self.videos.close()
        if self._client is not None:
            await self._client.aclose()
        self._client = None
        self._ready = False

    # ------------------------------------------------------------ 消息入口

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        try:
            async for result in self._on_message(event):
                yield result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 插件异常不能污染全局
            logger.error(f"[l4d2] 处理消息失败: {exc}", exc_info=True)

    async def _on_message(self, event: AstrMessageEvent) -> AsyncIterator:
        if not self.settings.enabled or not self._ready:
            return
        if self.settings.group_only and event.is_private_chat():
            return
        if event.get_sender_id() == event.get_self_id():
            return
        umo = event.unified_msg_origin
        if not self.settings.allows(umo):
            return
        ctx = analyze_message(event.get_messages(), event.message_str)
        if not self._is_command(ctx.text):
            self._remember_card(event, ctx)
        # 第一段默认走 /解析 命令；auto_link_enabled 才恢复“@机器人 + 链接”自动处理
        if not self.settings.auto_link_enabled:
            return
        if self.settings.require_at and not event.is_at_or_wake_command:
            return

        if not ctx.urls:
            return
        # 防止某些平台把机器人自己的回复再推回来，形成自激循环
        if ctx.text.lstrip().startswith("🎮 求生之路2 · 创意工坊地图"):
            return
        if self._is_command(ctx.text):
            return

        handled = False
        processed_videos = 0
        for url in ctx.video_urls:
            if processed_videos >= self.settings.max_videos_per_message:
                break
            platform = detect_platform(url)
            if platform not in self.settings.video_platforms:
                continue

            processed_videos += 1
            info = await self._fetch_video(url)
            if info is None:
                continue
            ids = extract_workshop_ids(
                "\n".join((info.title, info.desc, ctx.card_text, ctx.text))
            )
            if not ids:
                if (
                    self.settings.hint_when_no_link
                    and self._looks_like_l4d2(info)
                    and not self.dedupe.hit(f"{umo}:novideo:{info.video_id}")
                ):
                    yield event.plain_result(render_missing_link(info))
                continue

            handled = True
            async for result in self._process_ids(
                event, ids, info, manual=False, deliver=True
            ):
                yield result

        if not handled and ctx.workshop_ids and self.settings.handle_direct_workshop:
            async for result in self._process_ids(
                event, ctx.workshop_ids, None, manual=False, deliver=True
            ):
                yield result

    # ------------------------------------------------------------ 核心流程

    async def _process_ids(
        self,
        event: AstrMessageEvent,
        item_ids: list[int],
        video: VideoInfo | None,
        *,
        manual: bool,
        deliver: bool = False,
        folder: str = "",
    ) -> AsyncIterator:
        if self.steam is None or self.mirrors is None:
            return
        umo = event.unified_msg_origin
        wanted = list(dict.fromkeys(item_ids))[:8]
        details = await self.steam.get_many(wanted)
        emitted = 0

        for item_id in wanted:
            if emitted >= self.settings.max_items_per_message:
                break
            item = details.get(item_id)
            if (item is None or not item.ok) and self.mirrors is not None:
                # api.steampowered.com 被墙/抽风时，用竹烨镜像兜底
                fallback = await self.mirrors.zhr_item(item_id)
                if fallback is not None:
                    logger.info(f"[l4d2] Steam 查询失败，改用竹烨镜像：{item_id}")
                    item = fallback
            if item is None or not item.ok:
                if manual:
                    reason = "物品不存在、已删除或不可见" if item and item.result == 9 else "查询失败"
                    yield event.plain_result(render_invalid(item_id, reason))
                continue

            targets = await self._expand(item)
            if deliver and self.mirrors is not None and len(targets) == 1:
                # Steam 没给 children 时，用竹烨 l4d2-workshop 补 required_items/合集
                zhr_items = await self.mirrors.zhr_bundle_items(item.item_id)
                if len(zhr_items) > 1:
                    logger.info(
                        f"[l4d2] 竹烨补全多 VPK：{item.item_id} -> {len(zhr_items)} 个文件"
                    )
                    targets = zhr_items
            if deliver and len(targets) > 1:
                async for result in self._process_bundle(
                    event, item, targets, video, folder
                ):
                    yield result
                emitted += 1
                continue
            for target in targets:
                if emitted >= self.settings.max_items_per_message:
                    break
                if target.app_id != L4D2_APP_ID:
                    if manual:
                        yield event.plain_result(render_not_l4d2(target))
                    continue

                key = f"{umo}:{target.item_id}"
                if not manual and self.dedupe.hit(key):
                    logger.debug(f"[l4d2] 跳过重复地图 {target.item_id}")
                    continue

                resolved = await self.mirrors.resolve(target)
                text = render_item(resolved, video=video, settings=self.settings)
                extra = self._cache_line(target, resolved)
                if extra:
                    text = f"{text}\n{extra}"
                if deliver:
                    upload_ctx, upload_note = self._delivery_plan(
                        event, target, resolved, folder=folder
                    )
                else:
                    upload_ctx, upload_note = None, ""
                if upload_note:
                    text = f"{text}\n{upload_note}"
                yield self._result(event, text)
                emitted += 1
                self._maybe_cache(target, resolved)
                if upload_ctx is not None:
                    self._spawn_delivery(upload_ctx, target, resolved)

        if manual and emitted == 0 and not any(
            details.get(item_id) and details[item_id].ok for item_id in wanted
        ):
            yield event.plain_result(
                "没解析到有效的工坊物品。用法：/l4d2 <工坊链接 / 视频链接 / 物品ID>"
            )

    async def _expand(self, item: WorkshopItem) -> list[WorkshopItem]:
        """合集展开成单张地图（保持作者排好的 Part 顺序）。"""
        if not item.children or self.steam is None:
            return [item]
        children = await self.steam.get_many(item.children[:10])
        maps = [
            children[child_id]
            for child_id in item.children[:10]
            if child_id in children and children[child_id].ok and children[child_id].app_id == L4D2_APP_ID
        ]
        return maps or [item]

    # ------------------------------------------------------------ 手动指令

    @filter.command("l4d2", alias={"地图", "工坊"})
    async def cmd_l4d2(self, event: AstrMessageEvent, arg: str = ""):
        if not self._ready:
            await self.initialize()
        target = _arg_text(event, arg, {"l4d2", "地图", "工坊"})
        if not target:
            yield event.plain_result(render_help())
            return

        ids = extract_workshop_ids(target)
        video: VideoInfo | None = None
        if not ids and target.strip().isdigit():
            ids = [int(target.strip())]
        if not ids:
            for candidate in find_urls(target):
                video = await self._fetch_video(candidate)
                if video is not None:
                    ids = extract_workshop_ids(video.content_for_links)
                    if ids:
                        break
        if not ids:
            yield event.plain_result(
                "没识别到工坊 ID。\n用法：/l4d2 1234567890 或 /l4d2 <工坊/视频链接>"
            )
            return

        async for result in self._process_ids(event, ids, video, manual=True):
            yield result

    @filter.command("解析", alias={"机器人", "l4d2bot", "Self", "self"})
    async def cmd_bot(self, event: AstrMessageEvent, arg: GreedyStr):
        """统一入口：/解析 <链接|设置群文件夹|状态|只看>；上传用独立命令 /上传地图"""
        text = _arg_text(
            event, arg, {"解析", "机器人", "l4d2bot", "Self", "self"}
        )
        if not text:
            # 没有文字参数时，尝试从当前消息/引用的视频小程序卡片里取链接
            ctx = analyze_message(event.get_messages(), event.message_str)
            if not ctx.video_urls:
                ctx = self._recent_card(event) or ctx
            card_url, _reason = self._pick_card_video(ctx)
            if card_url:
                async for result in self.cmd_send_map(event, GreedyStr("")):
                    yield result
                return
            yield event.plain_result(_HELP_TEXT)
            return
        if text in {"帮助", "help", "？", "?"}:
            yield event.plain_result(_HELP_TEXT)
            return

        head, _, rest = text.partition(" ")
        head = head.strip()
        rest = rest.strip()

        if head in {"上传地图", "上传", "部署地图", "部署"}:
            async for result in self.cmd_upload_map(event, rest):
                yield result
            return
        if head in {"设置群文件夹", "群文件夹"}:
            async for result in self.cmd_set_folder(event, GreedyStr(rest)):
                yield result
            return
        if head in {"状态", "自检"}:
            async for result in self.cmd_status(event):
                yield result
            return
        if head in {"搜索", "搜图", "搜索地图"}:
            async for result in self.cmd_search(event, GreedyStr(rest)):
                yield result
            return
        if head in {"换一批", "下一批", "换页"}:
            async for result in self.cmd_next_search(event):
                yield result
            return
        if head in {"只看", "查看", "链接", "l4d2"}:
            async for result in self.cmd_l4d2(event, rest):
                yield result
            return

        # 其余情况一律当“下载并发送”：/解析 <链接或ID> [文件夹]
        async for result in self.cmd_send_map(event, GreedyStr(text)):
            yield result

    @filter.command("搜索地图", alias={"搜索", "搜图"})
    async def cmd_search(self, event: AstrMessageEvent, arg: GreedyStr):
        """按地图名搜索工坊，返回结果图。"""
        if not self._ready:
            await self.initialize()
        if not self.settings.search_enabled:
            yield event.plain_result("搜索功能已关闭（search_enabled=false）。")
            return
        query = _arg_text(event, arg, {"搜索地图", "搜索", "搜图"})
        if not query:
            yield event.plain_result("用法：/搜索地图 <地图名或关键词>\n例如：/搜索地图 广西灵山")
            return
        if self.search_service is None or self.search_dir is None:
            yield event.plain_result("搜索模块还没初始化，稍后再试。")
            return
        try:
            items, total = await self.search_service.search(
                query, self.settings.search_results, page=1
            )
            if not items:
                yield event.plain_result(f"没搜到「{query}」相关地图，换个关键词试试。")
                return
            self._search_state[self._card_key(event)] = (
                query,
                1,
                {item.item_id for item in items},
                time.time(),
            )
            path = await self.search_service.render(
                items, self.search_dir, query, total, page=1
            )
            yield event.image_result(str(path))
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, OSError) as exc:
            logger.warning(f"[l4d2] 搜索失败 {query}: {exc}")
            yield event.plain_result(f"搜索失败：{exc}")

    @filter.command("换一批", alias={"下一批", "换页"})
    async def cmd_next_search(self, event: AstrMessageEvent):
        """同一关键词看下一批搜索结果。"""
        if not self._ready:
            await self.initialize()
        if self.search_service is None or self.search_dir is None:
            yield event.plain_result("搜索模块还没初始化，稍后再试。")
            return
        key = self._card_key(event)
        state = self._search_state.get(key)
        if state is None or time.time() - state[3] > 1800:
            yield event.plain_result("先发 /搜索地图 <地图名>，再发 /换一批 看下一页。")
            return

        query, page, seen, _ = state
        next_page = page + 1
        try:
            items, total = await self.search_service.search(
                query, self.settings.search_results, page=next_page
            )
            fresh = [item for item in items if item.item_id not in seen]
            if not fresh:
                yield event.plain_result(
                    f"「{query}」没有更多结果了，发 /搜索地图 换个关键词试试。"
                )
                return
            seen.update(item.item_id for item in fresh)
            self._search_state[key] = (query, next_page, seen, time.time())
            path = await self.search_service.render(
                fresh, self.search_dir, query, total, page=next_page
            )
            yield event.image_result(str(path))
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError, OSError) as exc:
            logger.warning(f"[l4d2] 换一批失败 {query}: {exc}")
            yield event.plain_result(f"换一批失败：{exc}")

    @filter.command("发送地图", alias={"下载地图"})
    async def cmd_send_map(self, event: AstrMessageEvent, arg: GreedyStr):
        """第一段：/解析 <链接或ID> [群文件夹]"""
        if not self._ready:
            await self.initialize()
        if not self.settings.send_file_to_group or self.uploader is None:
            yield event.plain_result("文件发送没开启：先在插件配置里打开 send_file_to_group。")
            return

        ctx = analyze_message(event.get_messages(), event.message_str)
        if not ctx.video_urls:
            ctx = self._recent_card(event) or ctx
        rest = _arg_text(
            event,
            arg,
            {"发送地图", "下载地图", "解析", "机器人", "l4d2bot", "Self", "self"},
        )
        card_url, card_reason = (
            (None, "")
            if rest
            else self._pick_card_video(ctx)
        )
        if not rest:
            if not card_url:
                yield event.plain_result(
                    card_reason
                    or "用法：/解析 <工坊链接/视频链接/ID> [群文件夹]\n"
                    "也可以引用视频小程序卡片再发 /解析"
                )
                return
            rest = card_url

        # 兼容“标题 + 链接”的转发文案：优先从整段文字里抽第一个 URL
        urls = find_urls(rest)
        if not urls and ctx.video_urls:
            # 文本里没有链接时，再尝试卡片（带关键词/来源限制）
            if card_url is None:
                card_url, card_reason = self._pick_card_video(ctx)
            if card_url:
                urls = [card_url]
            elif card_reason and not extract_workshop_ids(rest):
                yield event.plain_result(card_reason)
                return
        if urls:
            target = urls[0]
            after = rest.split(target, 1)[1].strip() if target in rest else ""
            folder = after.split(maxsplit=1)[0] if after else ""
        else:
            parts = rest.split(maxsplit=1)
            target = parts[0].strip()
            folder = parts[1].strip() if len(parts) > 1 else ""
        if event.is_private_chat() and folder:
            yield event.plain_result("私聊不支持指定群文件夹，已忽略。")
            folder = ""

        ids = extract_workshop_ids(rest)
        video: VideoInfo | None = None
        video_failed = False
        if not ids and target.isdigit():
            ids = [int(target)]
        if not ids:
            video = await self._fetch_video(target)
            if video is not None:
                ids = extract_workshop_ids(video.content_for_links)
            elif urls:
                video_failed = True
        if not ids:
            if video_failed:
                title_hint = rest.split("http", 1)[0].strip(" 【】[]").strip()[:40]
                hint = (
                    f"可以用 /搜索地图 {title_hint} 手动搜"
                    if title_hint
                    else "可以用 /搜索地图 <地图名> 手动搜"
                )
                yield event.plain_result(
                    "视频链接解析失败（B 站接口超时/风控，或该链接不是视频页）。\n"
                    f"{hint}\n或直接发 /解析 <工坊链接/ID>"
                )
                return
            hint = "可以用 /搜索地图 <地图名> 手动搜"
            yield event.plain_result(
                f"没识别到工坊 ID。\n{hint}\n或直接发 /解析 <工坊链接/ID>"
            )
            return

        async for result in self._process_ids(
            event, ids, video, manual=True, deliver=True, folder=folder
        ):
            yield result

    @filter.command("设置群文件夹", alias={"群文件夹"})
    async def cmd_set_folder(self, event: AstrMessageEvent, arg: GreedyStr):
        """设置本群默认的群文件夹；不带参数则查看。"""
        if not self._ready:
            await self.initialize()
        if event.is_private_chat():
            yield event.plain_result("群文件夹只能在群里设置。")
            return
        if not await self._can_upload(event):
            yield event.plain_result("只有群主/管理员可以设置群文件夹。")
            return
        if self.folders is None or self.folder_resolver is None:
            yield event.plain_result("文件夹模块还没初始化，稍后再试。")
            return

        group_id = str(event.get_group_id() or "")
        name = _arg_text(
            event, arg, {"设置群文件夹", "群文件夹"}
        ).strip().strip('"').strip("'")
        if not name:
            saved = self.folders.get(group_id)
            current = (saved or {}).get("name") or self.settings.group_file_folder or "根目录"
            yield event.plain_result(f"当前群文件夹：{current}\n用 /设置群文件夹 <名称> 修改。")
            return

        bot = getattr(event, "bot", None)
        if bot is None:
            yield event.plain_result("当前平台拿不到 OneBot 客户端，无法设置文件夹。")
            return

        folder_id = await self.folder_resolver.resolve(bot, group_id, name)
        if folder_id == "/":
            yield event.plain_result(
                f"没找到也没能创建群文件夹「{name}」。请先在群文件里手动建一个同名文件夹，再试。"
            )
            return
        self.folders.set(group_id, name, folder_id)
        yield event.plain_result(f"已把本群默认文件夹设为「{name}」。之后 /解析 会传到这里。")

    @filter.command("上传地图", alias={"部署地图"})
    async def cmd_upload_map(self, event: AstrMessageEvent, arg: str = ""):
        if not self._ready:
            await self.initialize()
        if self.panel is None:
            yield event.plain_result(
                "没配置管理端。先在插件配置里填 panel_url、panel_token，并打开 panel_enabled。"
            )
            return
        if self.pending is None:
            yield event.plain_result("待部署记录还没初始化，稍后再试。")
            return

        private = event.is_private_chat()
        if private:
            if not self.settings.allow_private_upload:
                yield event.plain_result("私聊上传已关闭，请到群里让群主/管理员操作。")
                return
            group_id = f"private:{event.get_sender_id()}"
            item_id = _parse_item_id(arg)
            record = self.pending.find(group_id, item_id)
            if record is None:
                hint = f"工坊 {item_id}" if item_id else "最近一张"
                yield event.plain_result(f"没找到{hint}的待部署地图。先发一次视频或工坊链接。")
                return
            path = Path(record.archive_path)
            if not path.is_file():
                yield event.plain_result(
                    f"《{record.title}》的压缩包已被清理，请重新发一次链接。"
                )
                return
            async for result in self._start_panel_upload(event, record, path):
                yield result
            return

        if not await self._can_upload(event):
            yield event.plain_result("只有群主/管理员可以上传地图到服务器。")
            return
        group_id = str(event.get_group_id() or "")
        target = _arg_text(event, arg, {"上传地图", "部署地图"}).strip()
        item_id = _parse_item_id(target)

        # 优先用第一段记录的本地压缩包（能精确对上工坊 ID）
        if item_id:
            record = self.pending.find(group_id, item_id)
            if record is not None:
                path = Path(record.archive_path)
                if path.is_file():
                    async for result in self._start_panel_upload(event, record, path):
                        yield result
                    return

        label, files = await self._list_group_archives(event, group_id)
        if not target:
            if not files:
                yield event.plain_result(
                    f"「{label}」里没有压缩包。先发 /解析 <链接> 让机器人发一个，"
                    "或把压缩包传到该群文件夹。"
                )
                return
            lines = [f"📂 {label} 里的压缩包："]
            for index, item in enumerate(files, start=1):
                size = int(item.get("file_size") or 0)
                lines.append(f"{index}. {item.get('file_name')}（{human_size(size)}）")
            lines.append("发送 /上传地图 <序号> 或 /上传地图 <文件名片段> 上传。")
            yield event.plain_result("\n".join(lines))
            return

        file: dict[str, Any] | None = None
        if target.isdigit() and 1 <= int(target) <= len(files):
            file = files[int(target) - 1]
        else:
            for item in files:
                name = str(item.get("file_name") or "")
                if target.lower() in name.lower():
                    file = item
                    break
        if file is None:
            yield event.plain_result(
                f"在「{label}」里没找到「{target}」。发 /上传地图 看现有压缩包列表。"
            )
            return

        size = int(file.get("file_size") or 0)
        if size and size > self.settings.panel_max_mb * 1024 * 1024:
            yield event.plain_result(
                f"「{file.get('file_name')}」{human_size(size)} 超过管理端上限 "
                f"{self.settings.panel_max_mb}MB。"
            )
            return
        try:
            path = await self._ensure_group_archive(event, group_id, file)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"从群文件取压缩包失败：{exc}")
            return
        record = PendingMap(
            group_id=group_id,
            item_id=item_id or 0,
            title=Path(str(file.get("file_name") or path.name)).stem,
            archive_path=str(path),
            fmt=path.suffix.lstrip(".").lower(),
            size=path.stat().st_size,
        )
        async for result in self._start_panel_upload(event, record, path):
            yield result

    async def _start_panel_upload(
        self, event: AstrMessageEvent, record: PendingMap, path: Path
    ):
        if path.stat().st_size > self.settings.panel_max_mb * 1024 * 1024:
            yield event.plain_result(
                f"《{record.title}》压缩包 {human_size(path.stat().st_size)} "
                f"超过管理端上传上限 {self.settings.panel_max_mb}MB。"
            )
            return
        yield event.plain_result(
            f"开始上传《{record.title}》到管理端…\n· {path.name}（{human_size(path.stat().st_size)}）"
        )
        self._spawn_panel_upload(event.unified_msg_origin, record, path)

    async def _list_group_archives(
        self, event: AstrMessageEvent, group_id: str
    ) -> tuple[str, list[dict[str, Any]]]:
        bot = getattr(event, "bot", None)
        if bot is None:
            return "群文件", []
        folder_name = ""
        folder_id = ""
        if self.folders is not None:
            saved = self.folders.get(group_id)
            folder_name = (saved or {}).get("name", "")
            folder_id = (saved or {}).get("id", "")
        folder_name = folder_name or self.settings.group_file_folder
        if not folder_id and folder_name and self.folder_resolver is not None:
            folder_id = await self.folder_resolver.resolve(bot, group_id, folder_name)
        folder_id = folder_id or "/"
        try:
            if folder_id == "/":
                data = await bot.call_action(
                    "get_group_root_files", group_id=_as_int(group_id)
                )
            else:
                data = await bot.call_action(
                    "get_group_files_by_folder",
                    group_id=_as_int(group_id),
                    folder_id=folder_id,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[l4d2] 列群文件失败: {exc}")
            return folder_name or "群文件", []
        rows = data.get("files") if isinstance(data, dict) else []
        files = [
            row
            for row in (rows or [])
            if isinstance(row, dict)
            and str(row.get("file_name") or "").lower().endswith(
                (".7z", ".zip", ".rar", ".vpk")
            )
        ]
        files.sort(key=lambda row: int(row.get("upload_time") or 0), reverse=True)
        return folder_name or "群文件根目录", files

    async def _ensure_group_archive(
        self, event: AstrMessageEvent, group_id: str, file: dict[str, Any]
    ) -> Path:
        bot = getattr(event, "bot", None)
        if bot is None or self.archive_dir is None or self._client is None:
            raise RuntimeError("平台或缓存目录不可用")
        raw_name = str(file.get("file_name") or "")
        name = _safe_file_name(raw_name)
        dest = self.archive_dir / name
        size = int(file.get("file_size") or 0)
        if dest.is_file() and (not size or dest.stat().st_size == size):
            return dest
        data = await bot.call_action(
            "get_group_file_url",
            group_id=_as_int(group_id),
            file_id=str(file.get("file_id") or ""),
            busid=int(file.get("busid") or 102),
        )
        url = str((data or {}).get("url") or "")
        if not url:
            raise RuntimeError("管理端没有返回群文件下载地址")
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            with tmp.open("wb") as handle:
                async for chunk in response.aiter_bytes(256 * 1024):
                    handle.write(chunk)
        tmp.replace(dest)
        return dest

    @filter.command("l4d2状态", alias={"l4d2镜像"})
    async def cmd_status(self, event: AstrMessageEvent):
        if not self._ready:
            await self.initialize()
        assert self._client is not None
        lines = ["🧪 求生之路2地图镜像助手 · 自检"]

        steam_ok = await self._check(lambda: self._client.get(
            "https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/"
        ))
        lines.append(f"{'✅' if steam_ok else '❌'} Steam Web API")

        if self.settings.use_zhrradiant:
            zhr_ok = await self._check(lambda: self._client.get(
                "https://zhrradiant.com/tools/workshop-accelerator"
            ))
            lines.append(f"{'✅' if zhr_ok else '❌'} 竹烨工坊镜像")

        if self.settings.panel_ready() and self.panel is not None:
            try:
                role = await self.panel.client.role()
                version = await self.panel.client.version()
                lines.append(f"✅ 管理端：{version or '?'}（{role or '未知角色'}）")
            except PanelError as exc:
                lines.append(f"❌ 管理端：{exc}")
        else:
            lines.append("· 管理端：未配置")

        seven = find_7z(self.settings.seven_zip_path)
        lines.append(f"· 7z：{seven or '未找到，回退 zip'}")
        if self.pending is not None:
            if event.is_private_chat():
                key = f"private:{event.get_sender_id()}"
                lines.append(f"· 待部署：私聊 {self.pending.count(key)} 张")
            else:
                group_id = str(event.get_group_id() or "")
                lines.append(f"· 待部署：本群 {self.pending.count(group_id)} 张")
                if self.folders is not None:
                    saved = self.folders.get(group_id)
                    folder = (saved or {}).get("name") or self.settings.group_file_folder or "根目录"
                    lines.append(f"· 默认群文件夹：{folder}")

        lines.append(f"· 直链校验：{'开' if self.settings.verify_links else '关'}")
        lines.append(f"· 自定义镜像：{len(self.settings.custom_mirrors)} 条")
        for mirror in self.settings.custom_mirrors:
            lines.append(f"  - {mirror.name}")
        lines.append(f"· 自动缓存：{'开' if self.settings.auto_cache else '关'}")
        if self.settings.send_file_to_group:
            lines.append(f"· 传群文件：开（≤{self.settings.send_file_max_mb}MB）")
        else:
            lines.append("· 传群文件：关")
        lines.append(f"· 去重：{self.settings.dedupe_ttl_hours} 小时")
        lines.append(f"· B站 Cookie：{'已设置' if self.settings.bilibili_cookie else '未设置'}")
        lines.append(
            f"· 代理：全局 {self.settings.proxy or '未设置'}；"
            f"B站 {self.settings.bilibili_proxy or self.settings.proxy or '未设置'}"
        )
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------ 工具方法

    async def _fetch_video(self, url: str) -> VideoInfo | None:
        if self.videos is None:
            return None
        info = await self.videos.fetch(url)
        if not info.ok and not info.desc:
            logger.warning(f"[l4d2] 视频解析失败 {url}: {info.error or '未知错误'}")
            return None
        return info

    async def _check(self, factory) -> bool:
        try:
            response = await factory()
            return response.status_code < 500
        except (httpx.HTTPError, asyncio.TimeoutError, ValueError):
            return False

    def _result(self, event: AstrMessageEvent, text: str):
        if self.settings.mention_sender and not event.is_private_chat():
            return event.chain_result([At(qq=event.get_sender_id()), Plain(" " + text)])
        return event.plain_result(text)

    def _maybe_cache(self, item: WorkshopItem, resolved: ResolvedItem) -> None:
        if not self.settings.auto_cache or self.downloader is None:
            return
        if not resolved.direct_url or resolved.direct_ok is False:
            return
        self.downloader.spawn(item, resolved)

    def _cache_line(self, item: WorkshopItem, resolved: ResolvedItem) -> str:
        if not self.settings.auto_cache or not resolved.direct_url:
            return ""
        if resolved.direct_ok is False:
            return ""
        if self.settings.public_base_url:
            url = f"{self.settings.public_base_url}/{quote(cache_filename(item))}"
            return f"📦 机器人镜像：{url}（后台缓存，完成后可下）"
        return "📦 已加入机器人缓存队列"

    def _card_key(self, event: AstrMessageEvent) -> str:
        if event.is_private_chat():
            return f"private:{event.get_sender_id()}"
        try:
            return f"group:{event.get_group_id()}"
        except Exception:  # noqa: BLE001 - 个别平台没有群概念
            return event.unified_msg_origin

    def _remember_card(self, event: AstrMessageEvent, ctx) -> None:
        """缓存最近几条带视频链接的卡片或文字消息，供后面的 /解析 使用。"""
        if self.settings.miniprogram_recent_seconds <= 0:
            return
        if not ctx.video_urls and not ctx.workshop_ids:
            return
        key = self._card_key(event)
        now = time.time()
        bucket = [
            (stamp, card)
            for stamp, card in self._recent_cards.get(key, [])
            if now - stamp <= self.settings.miniprogram_recent_seconds
        ]
        bucket.append((now, ctx))
        self._recent_cards[key] = bucket[-5:]
        if len(self._recent_cards) > 200:
            oldest = sorted(
                self._recent_cards.items(),
                key=lambda kv: kv[1][-1][0] if kv[1] else 0,
            )[:50]
            for group_key, _ in oldest:
                self._recent_cards.pop(group_key, None)

    def _recent_card(self, event: AstrMessageEvent):
        key = self._card_key(event)
        bucket = self._recent_cards.get(key)
        if not bucket:
            return None
        now = time.time()
        fresh = [
            (stamp, card)
            for stamp, card in bucket
            if now - stamp <= self.settings.miniprogram_recent_seconds
        ]
        if not fresh:
            self._recent_cards.pop(key, None)
            return None
        self._recent_cards[key] = fresh
        # 从新到旧找第一张能通过关键词/来源限制的卡片
        for _, card in reversed(fresh):
            if self._pick_card_video(card)[0]:
                return card
        return fresh[-1][1]

    def _pick_card_video(self, ctx) -> tuple[str | None, str]:
        """从最近消息里挑视频链接；卡片受关键词/来源限制，普通文字链接直接放行。"""
        is_card = bool(ctx.card_text.strip())
        if is_card and not self.settings.miniprogram_enabled:
            return None, "小程序卡片解析已关闭。"
        if not ctx.video_urls:
            if ctx.workshop_ids:
                return (
                    "https://steamcommunity.com/sharedfiles/filedetails/"
                    f"?id={ctx.workshop_ids[0]}",
                    "",
                )
            return None, "没找到视频链接。"
        if not is_card:
            # 群友可以直接发“标题 + 链接”的文字，再 /解析
            return ctx.video_urls[0], ""
        haystack = ctx.all_text.lower()
        if self.settings.miniprogram_require_keyword:
            keywords = [
                word.lower() for word in self.settings.miniprogram_keywords if word
            ]
            if keywords and not any(word in haystack for word in keywords):
                return None, "卡片标题/简介不像求生之路2地图，已跳过。"
        sources = [
            word.lower() for word in self.settings.miniprogram_allowed_sources if word
        ]
        if sources and not any(word in haystack for word in sources):
            return None, "卡片来源不在允许列表，已跳过。"
        return ctx.video_urls[0], ""

    def _delivery_plan(
        self,
        event: AstrMessageEvent,
        item: WorkshopItem,
        resolved: ResolvedItem,
        *,
        folder: str = "",
        size_override: int = 0,
    ) -> tuple[dict[str, Any] | None, str]:
        """第一段：是否打包发群文件/私聊。"""
        if not self.settings.send_file_to_group or self.uploader is None:
            return None, ""
        if not resolved.direct_url or resolved.direct_ok is False:
            return None, ""

        bot = getattr(event, "bot", None)
        if bot is None:
            return None, ""

        size = size_override or resolved.direct_size or item.file_size
        limit = self.settings.send_file_max_mb * 1024 * 1024
        if size and size > limit:
            return None, (
                f"📤 文件 {human_size(size)} 超过文件上限 "
                f"{self.settings.send_file_max_mb}MB，未打包上传"
            )

        if event.is_private_chat():
            target = str(event.get_sender_id() or "")
            if not target:
                return None, ""
            tip = (
                f"📦 正在下载并打包（{human_size(size)}），完成后发到私聊"
                if size
                else "📦 正在下载并打包，完成后发到私聊"
            )
            return (
                {
                    "bot": bot,
                    "private": True,
                    "target_id": target,
                    "pending_key": f"private:{target}",
                    "umo": event.unified_msg_origin,
                },
                tip,
            )

        try:
            group_id = str(event.get_group_id() or "")
        except Exception:  # noqa: BLE001 - 个别平台没有群概念
            group_id = ""
        if not group_id:
            return None, ""

        folder = (folder or "").strip().strip('"').strip("'")
        folder_hint = f" → {folder}" if folder else ""
        tip = (
            f"📦 正在下载并打包（{human_size(size)}），完成后发群文件{folder_hint}"
            if size
            else f"📦 正在下载并打包，完成后发群文件{folder_hint}"
        )
        return (
            {
                "bot": bot,
                "private": False,
                "group_id": group_id,
                "pending_key": group_id,
                "folder_name": folder,
                "umo": event.unified_msg_origin,
            },
            tip,
        )

    async def _folder_target(self, ctx: dict[str, Any]) -> tuple[str, str]:
        if ctx.get("private") or self.folder_resolver is None:
            return "", ""
        group_id = str(ctx.get("group_id") or "")
        folder_name = str(ctx.get("folder_name") or "")
        if not folder_name and self.folders is not None:
            saved = self.folders.get(group_id)
            if saved and saved.get("id"):
                return str(saved["id"]), ""
            folder_name = (saved or {}).get("name", "")
        if not folder_name:
            folder_name = self.settings.group_file_folder
        if not folder_name:
            return "", ""
        folder_id = await self.folder_resolver.resolve(ctx["bot"], group_id, folder_name)
        note = "" if folder_id != "/" else f"没找到群文件夹「{folder_name}」，已发到根目录"
        return folder_id, note

    def _spawn_delivery(
        self, ctx: dict[str, Any], item: WorkshopItem, resolved: ResolvedItem
    ) -> None:
        task = asyncio.create_task(self._deliver_map(ctx, item, resolved))
        self._panel_tasks.add(task)
        task.add_done_callback(self._panel_tasks.discard)

    async def _deliver_map(
        self, ctx: dict[str, Any], item: WorkshopItem, resolved: ResolvedItem
    ) -> None:
        umo = str(ctx["umo"])
        try:
            if self.downloader is None or self.uploader is None or self.archive_dir is None:
                return
            result = await self.downloader.download(
                item, resolved, limit_mb=self.settings.send_file_max_mb
            )
            if not result.ok or result.path is None:
                await self._notify(
                    umo,
                    f"❌ 《{item.display_name}》下载失败：{result.error or '未知错误'}",
                )
                return

            archive = await make_archive_async(
                result.path,
                self.archive_dir,
                seven_zip=self.settings.seven_zip_path,
                level=self.settings.archive_level,
                name=safe_stem(item.item_id, item.display_name, item.filename),
                member_name=archive_member_name(
                    item.item_id, item.display_name, item.filename
                ),
            )
            private = bool(ctx.get("private"))
            folder_id, folder_note = await self._folder_target(ctx)
            ok, error = await self.uploader.send_path(
                bot=ctx["bot"],
                group_id=str(ctx.get("group_id") or ""),
                user_id=str(ctx.get("target_id") or "") if private else "",
                path=archive.path,
                name=archive.path.name,
                folder=folder_id,
            )
            if not ok:
                label = "私聊文件" if private else "群文件"
                await self._notify(
                    umo, f"❌ 《{item.display_name}》传{label}失败：{error}"
                )
                return

            if self.pending is not None:
                self.pending.add(
                    group_id=str(ctx["pending_key"]),
                    item_id=item.item_id,
                    title=item.display_name,
                    archive_path=archive.path,
                    fmt=archive.fmt,
                    size=archive.size,
                )
            label = "私聊文件" if private else "群文件"
            lines = [
                f"📦 已发{label}：{archive.path.name}（{human_size(archive.size)}，{archive.fmt}）",
            ]
            if folder_note:
                lines.append(f"⚠️ {folder_note}")
            if archive.note:
                lines.append(f"⚠️ {archive.note}")
            if private:
                lines.append(f"回复「/上传地图 {item.item_id}」可部署到服务器")
            else:
                lines.append(
                    f"群主/管理员可 /上传地图 {item.item_id} 部署到服务器"
                )
            await self._notify(umo, "\n".join(lines))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[l4d2] 文件投递失败 {item.item_id}: {exc}")
            await self._notify(umo, f"❌ 《{item.display_name}》投递失败：{exc}")

    async def _process_bundle(
        self,
        event: AstrMessageEvent,
        parent: WorkshopItem,
        targets: list[WorkshopItem],
        video: VideoInfo | None,
        folder: str,
    ) -> AsyncIterator:
        """合集/多 Part：所有 VPK 下载后打成一个包，一次发全。"""
        if self.mirrors is None:
            return
        resolved_items: list[tuple[WorkshopItem, ResolvedItem]] = []
        for target in targets[:10]:
            if target.app_id != L4D2_APP_ID:
                continue
            resolved = await self.mirrors.resolve(target)
            resolved_items.append((target, resolved))
        if not resolved_items:
            yield event.plain_result(render_invalid(parent.item_id, "合集里没有可用的求生之路2地图"))
            return

        total = sum(
            (resolved.direct_size or target.file_size)
            for target, resolved in resolved_items
        )
        upload_ctx, note = self._delivery_plan(
            event, parent, resolved_items[0][1], folder=folder, size_override=total
        )
        if upload_ctx is not None:
            upload_ctx["bundle"] = resolved_items
            upload_ctx["parent_id"] = parent.item_id
            upload_ctx["title"] = parent.display_name
            upload_ctx["note"] = note

        lines = [
            f"🧩 《{parent.display_name}》共 {len(resolved_items)} 个文件，合计 {human_size(total)}",
        ]
        if note:
            lines.append(note)
        yield self._result(event, "\n".join(lines))
        for target, resolved in resolved_items:
            self._maybe_cache(target, resolved)
        if upload_ctx is not None:
            task = asyncio.create_task(self._deliver_bundle(upload_ctx))
            self._panel_tasks.add(task)
            task.add_done_callback(self._panel_tasks.discard)

    async def _deliver_bundle(self, ctx: dict[str, Any]) -> None:
        umo = str(ctx["umo"])
        bundle_dir: Path | None = None
        try:
            if self.downloader is None or self.uploader is None or self.archive_dir is None:
                return
            parent_id = int(ctx.get("parent_id") or 0)
            title = str(ctx.get("title") or f"合集 {parent_id}")
            stem = safe_stem(parent_id, title, "")
            bundle_dir = self.archive_dir / f"{stem}_bundle"
            if bundle_dir.exists():
                shutil.rmtree(bundle_dir, ignore_errors=True)
            bundle_dir.mkdir(parents=True, exist_ok=True)

            count = 0
            for target, resolved in ctx["bundle"]:
                result = await self.downloader.download(
                    target, resolved, limit_mb=self.settings.send_file_max_mb
                )
                if not result.ok or result.path is None:
                    await self._notify(
                        umo,
                        f"❌ 合集《{title}》里的 {target.item_id} 下载失败："
                        f"{result.error or '未知错误'}",
                    )
                    return
                member = archive_member_name(
                    target.item_id, target.display_name, target.filename
                )
                dest = bundle_dir / member
                if dest.exists():
                    dest = bundle_dir / (
                        f"{Path(member).stem}_{target.item_id}{Path(member).suffix}"
                    )
                shutil.copy2(result.path, dest)
                count += 1
            if count == 0:
                await self._notify(umo, f"❌ 合集《{title}》没有下载到文件")
                return

            archive = await make_archive_async(
                bundle_dir,
                self.archive_dir,
                seven_zip=self.settings.seven_zip_path,
                level=self.settings.archive_level,
                name=stem,
            )
            private = bool(ctx.get("private"))
            folder_id, folder_note = await self._folder_target(ctx)
            ok, error = await self.uploader.send_path(
                bot=ctx["bot"],
                group_id=str(ctx.get("group_id") or ""),
                user_id=str(ctx.get("target_id") or "") if private else "",
                path=archive.path,
                name=archive.path.name,
                folder=folder_id,
            )
            if not ok:
                label = "私聊文件" if private else "群文件"
                await self._notify(umo, f"❌ 合集《{title}》传{label}失败：{error}")
                return

            if self.pending is not None:
                self.pending.add(
                    group_id=str(ctx["pending_key"]),
                    item_id=parent_id,
                    title=title,
                    archive_path=archive.path,
                    fmt=archive.fmt,
                    size=archive.size,
                )
            label = "私聊文件" if private else "群文件"
            lines = [
                f"📦 已发{label}：{archive.path.name}（{human_size(archive.size)}，{archive.fmt}）",
                f"🧩 合集共 {count} 个 VPK，已打成一个包",
            ]
            if folder_note:
                lines.append(f"⚠️ {folder_note}")
            if archive.note:
                lines.append(f"⚠️ {archive.note}")
            lines.append(f"群主/管理员可 /上传地图 {parent_id} 部署到服务器")
            await self._notify(umo, "\n".join(lines))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[l4d2] 合集投递失败: {exc}")
            await self._notify(umo, f"❌ 合集投递失败：{exc}")
        finally:
            if bundle_dir is not None:
                shutil.rmtree(bundle_dir, ignore_errors=True)

    async def _notify(self, umo: str, text: str) -> None:
        if not self.settings.upload_notify:
            logger.info(f"[l4d2] {text}")
            return
        try:
            await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[l4d2] 补发消息失败: {exc}")

    # ------------------------------------------------------------ 管理端部署

    # ------------------------------------------------------------ 管理端上传

    def _spawn_panel_upload(self, umo: str, record: PendingMap, path: Path) -> None:
        task = asyncio.create_task(self._run_panel_upload(umo, record, path))
        self._panel_tasks.add(task)
        task.add_done_callback(self._panel_tasks.discard)

    async def _run_panel_upload(self, umo: str, record: PendingMap, path: Path) -> None:
        if self.panel is None:
            return
        try:
            await self.panel.client.upload_map(
                path, name=_panel_upload_name(path.name)
            )
        except PanelError as exc:
            await self._notify(umo, f"❌ 上传《{record.title}》失败：{exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[l4d2] 管理端上传异常: {exc}")
            await self._notify(umo, f"❌ 上传《{record.title}》失败：{exc}")
            return

        if self.pending is not None:
            self.pending.mark_uploaded(record)

        mode = self.settings.panel_after_upload
        reload_error = ""
        if mode == "hot_reload":
            try:
                await self.panel.client.hot_reload()
            except PanelError as exc:
                reload_error = str(exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reload_error = str(exc)

        if mode == "hot_reload" and not reload_error:
            tail = "🔁 已热重载地图，服务器现在应该能读到新图。"
        elif mode == "hot_reload":
            tail = f"⚠️ 上传成功，但热重载失败：{reload_error}\n请到管理端点「热重载地图」。"
        else:
            tail = "如要开图，请在管理端点「热重载地图」，或等下次换图。"

        await self._notify(
            umo,
            f"🖥 已上传到管理端：{record.title}\n"
            f"· {path.name}（{human_size(record.size)}）\n"
            f"{tail}",
        )

    async def _can_upload(self, event: AstrMessageEvent) -> bool:
        if event.is_private_chat():
            return False
        sender = str(event.get_sender_id())
        try:
            group = await event.get_group()
        except Exception:  # noqa: BLE001
            group = None
        if group is not None:
            if sender == str(getattr(group, "group_owner", "") or ""):
                return True
            if self.settings.upload_permission == "owner_admin":
                admins = {str(item) for item in (getattr(group, "group_admins", None) or [])}
                return sender in admins
            return False
        return event.role == "admin"

    @staticmethod
    def _is_command(text: str) -> bool:
        stripped = (text or "").strip()
        if not stripped:
            return False
        first = stripped.split(maxsplit=1)[0]
        # 带唤醒前缀的才按指令处理，避免把「地图 https://...」这种正常分享误判成指令
        head = first.lstrip("/／#!！.")
        if head != first:
            return head in COMMAND_NAMES
        return head in {
            "解析",
            "机器人",
            "l4d2bot",
            "l4d2",
            "l4d2状态",
            "l4d2镜像",
            "发送地图",
            "下载地图",
            "搜索",
            "搜图",
            "搜索地图",
            "换一批",
            "下一批",
            "换页",
            "设置群文件夹",
            "群文件夹",
            "上传地图",
            "部署地图",
        }

    @staticmethod
    def _command_arg(event: AstrMessageEvent, arg: str) -> str:
        if arg and arg.strip():
            return arg.strip()
        text = (event.message_str or "").strip()
        parts = text.split(maxsplit=1)
        head = parts[0].lstrip("/／").strip()
        if len(parts) == 2 and head in COMMAND_NAMES:
            return parts[1].strip()
        if head in COMMAND_NAMES:
            return ""
        return text

    @staticmethod
    def _looks_like_l4d2(info: VideoInfo) -> bool:
        haystack = f"{info.title}\n{info.desc}".lower()
        return any(hint in haystack for hint in _L4D2_HINTS)
