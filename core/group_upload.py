"""把下载好的 vpk 传到 QQ 群文件。

走 OneBot 11 的 ``upload_group_file``（NapCat / Lagrange 等协议端都实现了）。
上传是慢操作，所以放在后台任务里：先回文字卡片，传完再补一条消息。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .config import Settings
from .downloader import CacheDownloader, cache_filename
from .mirror import ResolvedItem
from .render import human_size
from .steam import WorkshopItem

logger = logging.getLogger("astrbot")

Notify = Callable[[str, str], Awaitable[None]]


@dataclass(slots=True)
class UploadOutcome:
    ok: bool
    item_id: int
    name: str = ""
    size: int = 0
    error: str = ""


class GroupFileUploader:
    def __init__(self, settings: Settings, downloader: CacheDownloader) -> None:
        self.settings = settings
        self.downloader = downloader
        self._tasks: set[asyncio.Task] = set()
        self._inflight: dict[tuple[str, int], asyncio.Task] = {}
        self._done: set[tuple[str, int]] = set()
        self._sem = asyncio.Semaphore(1)

    def already_done(self, group_id: str, item_id: int) -> bool:
        return (str(group_id), item_id) in self._done

    async def send_path(
        self,
        *,
        bot: Any,
        group_id: str = "",
        path: Path,
        name: str,
        user_id: str = "",
        folder: str = "",
    ) -> tuple[bool, str]:
        """把本地文件传到群文件或私聊，返回 (是否成功, 失败原因)。"""
        if user_id:
            action = "upload_private_file"
            payload: dict[str, Any] = {
                "user_id": _group_id(user_id),
                "file": str(path),
                "name": name,
            }
        else:
            action = "upload_group_file"
            payload = {
                "group_id": _group_id(group_id),
                "file": str(path),
                "name": name,
            }
            target_folder = folder or self.settings.group_file_folder
            if target_folder:
                payload["folder"] = target_folder
        try:
            await asyncio.wait_for(
                bot.call_action(action, **payload),
                timeout=self.settings.upload_timeout,
            )
        except asyncio.TimeoutError:
            return False, "上传超时"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return False, _short_error(exc)
        return True, ""

    def spawn(
        self,
        *,
        bot: Any,
        group_id: str,
        umo: str,
        item: WorkshopItem,
        resolved: ResolvedItem,
        notify: Notify,
    ) -> asyncio.Task | None:
        key = (str(group_id), item.item_id)
        if key in self._done:
            logger.debug(f"[l4d2] {item.item_id} 已上传过，跳过")
            return None
        running = self._inflight.get(key)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(
            self._run(
                key=key,
                bot=bot,
                group_id=group_id,
                umo=umo,
                item=item,
                resolved=resolved,
                notify=notify,
            )
        )
        self._tasks.add(task)
        self._inflight[key] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._inflight.pop(key, None))
        return task

    async def _run(
        self,
        *,
        key: tuple[str, int],
        bot: Any,
        group_id: str,
        umo: str,
        item: WorkshopItem,
        resolved: ResolvedItem,
        notify: Notify,
    ) -> None:
        async with self._sem:
            try:
                result = await self.downloader.download(
                    item, resolved, limit_mb=self.settings.send_file_max_mb
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[l4d2] 上传前下载失败 {item.item_id}: {exc}")
                await notify(umo, f"❌ 《{item.display_name}》下载失败：{exc}")
                return

            if not result.ok or result.path is None:
                await notify(
                    umo,
                    f"❌ 《{item.display_name}》没能上传：{result.error or '下载失败'}",
                )
                return

            name = cache_filename(item)
            payload: dict[str, Any] = {
                "group_id": _group_id(group_id),
                "file": str(result.path),
                "name": name,
            }
            if self.settings.group_file_folder:
                payload["folder"] = self.settings.group_file_folder

            try:
                await asyncio.wait_for(
                    bot.call_action("upload_group_file", **payload),
                    timeout=self.settings.upload_timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(f"[l4d2] 上传群文件超时 {item.item_id}")
                await notify(umo, f"❌ 《{item.display_name}》上传超时，请稍后在群文件里确认")
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 协议端抛什么都有可能
                logger.warning(f"[l4d2] 上传群文件失败 {item.item_id}: {exc!r}")
                await notify(
                    umo,
                    f"❌ 《{item.display_name}》上传失败：{_short_error(exc)}",
                )
                return

            self._done.add(key)
            logger.info(
                f"[l4d2] 已上传群文件 {name} ({result.size / 1024 / 1024:.1f}MB) -> {group_id}"
            )
            await notify(
                umo,
                f"📦 已上传群文件：{name}（{human_size(result.size)}）\n"
                "在群文件里找到后下载，解压/放进 left4dead2/addons 即可。",
            )

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()


def _group_id(value: str) -> int | str:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text


def _short_error(exc: BaseException) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text if len(text) <= 160 else text[:159] + "…"
