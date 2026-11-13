"""可选的本地缓存下载。

开了 ``auto_cache`` 之后，机器人会把 vpk 落到本地：

* 对开服玩家：直接丢进 ``left4dead2/addons`` 就能开图；
* 对群友：配合 ``public_base_url``（nginx / caddy 指到缓存目录），
  机器人本身就成了一个"群内自建镜像"。

默认关闭，避免一上来就下几百 MB。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import Settings
from .mirror import ResolvedItem
from .steam import WorkshopItem

logger = logging.getLogger("astrbot")

_CHUNK = 256 * 1024


def cache_filename(item: WorkshopItem) -> str:
    """缓存文件名：``<工坊ID>_<原始文件名>``，保证可追溯。"""
    raw = Path(item.filename or "").name or f"{item.item_id}.vpk"
    name = "".join("_" if ch in '\r\n\t"*/:<>?\\|' else ch for ch in raw).strip(" .")
    if not name or len(name) > 80:
        name = f"{item.item_id}.vpk"
    return f"{item.item_id}_{name}"


@dataclass(slots=True)
class DownloadResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    error: str = ""
    public_url: str = ""


class CacheDownloader:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, cache_dir: Path) -> None:
        self.settings = settings
        self.client = client
        self.cache_dir = cache_dir
        self._tasks: set[asyncio.Task] = set()
        self._inflight: dict[int, asyncio.Task] = {}

    # ------------------------------------------------------------------ 入口

    def spawn(self, item: WorkshopItem, resolved: ResolvedItem, on_done=None) -> asyncio.Task | None:
        if not resolved.direct_url:
            return None
        running = self._inflight.get(item.item_id)
        if running is not None and not running.done():
            return running
        task = asyncio.create_task(self._run(item, resolved, on_done))
        self._tasks.add(task)
        self._inflight[item.item_id] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._inflight.pop(item.item_id, None))
        return task

    async def _run(self, item: WorkshopItem, resolved: ResolvedItem, on_done) -> None:
        try:
            result = await self.download(item, resolved)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛给事件循环
            logger.warning(f"[l4d2] 缓存 {item.item_id} 失败: {exc}")
            result = DownloadResult(ok=False, error=str(exc))
        if on_done is not None:
            try:
                await on_done(result)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[l4d2] 缓存回调异常: {exc}")

    async def download(
        self,
        item: WorkshopItem,
        resolved: ResolvedItem,
        limit_mb: int | None = None,
    ) -> DownloadResult:
        cap_mb = self.settings.cache_max_mb if limit_mb is None else limit_mb
        limit = cap_mb * 1024 * 1024
        expected = resolved.direct_size or item.file_size
        if expected and expected > limit:
            return DownloadResult(
                ok=False,
                error=f"文件 {expected / 1024 / 1024:.0f}MB 超过上限 {cap_mb}MB",
            )

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        target = self.cache_dir / cache_filename(item)

        if target.exists() and (not expected or target.stat().st_size == expected):
            return self._done(target)

        method = self._steamcmd
        if method is not None:
            downloaded = await method(item)
            if downloaded is not None:
                shutil.copy2(downloaded, target)
                return self._done(target)

        return await self._http_download(item, resolved.direct_url, target, expected, limit)

    # ------------------------------------------------------------------ HTTP

    async def _http_download(
        self,
        item: WorkshopItem,
        url: str,
        target: Path,
        expected: int,
        limit: int = 0,
        retry: bool = True,
    ) -> DownloadResult:
        part = target.with_suffix(target.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        if expected and offset > expected:
            part.unlink(missing_ok=True)
            offset = 0

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        timeout = httpx.Timeout(connect=15, read=120, write=60, pool=15)
        logger.info(f"[l4d2] 开始缓存 {item.item_id} -> {target.name} (从 {offset} 字节续传)")

        async with self.client.stream("GET", url, headers=headers, timeout=timeout) as response:
            if response.status_code == 416:
                if expected and part.exists() and part.stat().st_size == expected:
                    part.replace(target)
                    return self._done(target)
                part.unlink(missing_ok=True)
                if retry:
                    return await self._http_download(
                        item, url, target, expected, limit, retry=False
                    )
                return DownloadResult(ok=False, error="断点位置无效")
            if response.status_code not in (200, 206):
                return DownloadResult(ok=False, error=f"HTTP {response.status_code}")

            mode = "ab" if (offset and response.status_code == 206) else "wb"
            if mode == "wb":
                offset = 0
            written = offset
            last_report = written
            with part.open(mode) as handle:
                async for chunk in response.aiter_bytes(_CHUNK):
                    handle.write(chunk)
                    written += len(chunk)
                    if limit and written > limit:
                        handle.close()
                        part.unlink(missing_ok=True)
                        return DownloadResult(
                            ok=False,
                            error=f"文件超过上限 {limit / 1024 / 1024:.0f}MB",
                        )
                    if written - last_report >= 32 * 1024 * 1024:
                        last_report = written
                        logger.info(f"[l4d2] 缓存 {item.item_id}: {written / 1024 / 1024:.0f}MB")

        if expected and written < expected:
            return DownloadResult(ok=False, error=f"下载不完整 {written}/{expected} 字节", size=written)

        part.replace(target)
        logger.info(f"[l4d2] 缓存完成 {target.name} ({written / 1024 / 1024:.1f}MB)")
        return self._done(target, written)

    def _done(self, path: Path, size: int | None = None) -> DownloadResult:
        size = size if size is not None else path.stat().st_size
        public_url = ""
        if self.settings.public_base_url:
            public_url = f"{self.settings.public_base_url}/{quote(path.name)}"
        return DownloadResult(ok=True, path=path, size=size, public_url=public_url)

    # --------------------------------------------------------------- steamcmd

    @property
    def _steamcmd(self):
        path = self.settings.steamcmd_path
        if not path:
            return None
        candidate = Path(path)
        if not candidate.is_file():
            return None
        return self._steamcmd_download

    async def _steamcmd_download(self, item: WorkshopItem) -> Path | None:
        exe = Path(self.settings.steamcmd_path)
        args = [
            str(exe),
            "+login",
            "anonymous",
            "+workshop_download_item",
            "550",
            str(item.item_id),
            "+quit",
        ]
        logger.info(f"[l4d2] steamcmd 下载 {item.item_id}")
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                cwd=str(exe.parent),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(process.wait(), timeout=1800)
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(f"[l4d2] steamcmd 执行失败: {exc}")
            return None

        root = exe.parent / "steamapps" / "workshop" / "content" / "550" / str(item.item_id)
        if not root.is_dir():
            return None
        candidates = sorted(root.rglob("*.vpk"), key=lambda p: p.stat().st_size, reverse=True)
        return candidates[0] if candidates else None

    # ------------------------------------------------------------------ 清理

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._inflight.clear()

    def cleanup(self) -> int:
        days = self.settings.cache_cleanup_days
        if days <= 0 or not self.cache_dir.is_dir():
            return 0
        deadline = time.time() - days * 86400
        removed = 0
        for file in self.cache_dir.iterdir():
            if not file.is_file():
                continue
            try:
                if file.stat().st_mtime < deadline:
                    os.remove(file)
                    removed += 1
            except OSError:
                continue
        return removed
