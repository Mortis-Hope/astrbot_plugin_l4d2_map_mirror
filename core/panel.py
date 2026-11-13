"""L4D2 Manager 管理端客户端（上传地图 + 热重载 + 队列/切图）。

接口来自面板 v1.14.4 前端：
- ``POST /auth``                    Bearer 校验，返回 role
- ``POST /upload/init``             {filename, fileSize, totalChunks} -> uploadId
- ``POST /upload/chunk``            multipart: uploadId, chunkIndex, chunk(5MB)
- ``POST /upload/merge``            {uploadId, filename}
- ``POST /maps/hot-reload``         挂载新 VPK
- ``POST /maps/queue/add``          {map, position: back|front}
- ``POST /maps/queue/start``        {mode: now|after_campaign}
- ``POST /rcon``                    {cmd}
- ``POST /rcon/maplist``            地图/章节列表
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import httpx

from .config import Settings
from .downloader import CacheDownloader
from .mirror import ResolvedItem
from .render import human_size
from .steam import WorkshopItem
from .vpk import VPKError, pick_start_map, read_map_codes

PANEL_CHUNK = 5 * 1024 * 1024
ProgressCb = Callable[[int, int], Awaitable[None]]


class PanelError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class PanelClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.settings = settings
        self.client = client
        self.base = settings.panel_url.rstrip("/")
        self._auth = {"Authorization": f"Bearer {settings.panel_token}"}

    # ------------------------------------------------------------------ 基础

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                f"{self.base}{path}",
                json=json,
                data=data,
                files=files,
                headers=self._auth,
                timeout=timeout or self.settings.panel_timeout,
            )
        except httpx.HTTPError as exc:
            raise PanelError(f"管理端不可达：{type(exc).__name__}") from exc

        if response.status_code == 401:
            raise PanelError("管理端 token 无效或已过期", 401)
        if response.status_code >= 400:
            text = (response.text or "").strip().replace("\n", " ")[:160]
            raise PanelError(f"管理端返回 {response.status_code}：{text}", response.status_code)
        return response

    async def _json(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        response = await self._request("POST", path, json=payload or {}, timeout=timeout)
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _form(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        """面板的 /upload/*、/maps/hot-reload 用 multipart/form-data，不是 JSON。"""
        response = await self._request("POST", path, data=payload or {}, timeout=timeout)
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def role(self) -> str:
        response = await self._request("POST", "/auth")
        try:
            return str((response.json() or {}).get("role") or "")
        except ValueError:
            return ""

    async def version(self) -> str:
        data = await self._json("/getVersion")
        return str(data.get("version") or "")

    # ------------------------------------------------------------------ 上传

    async def upload_map(
        self, path: Path, on_progress: ProgressCb | None = None, *, name: str = ""
    ) -> None:
        filename = name or path.name
        size = path.stat().st_size
        total = max(1, (size + PANEL_CHUNK - 1) // PANEL_CHUNK)
        init = await self._form(
            "/upload/init",
            {
                "filename": filename,
                "fileSize": size,
                "totalChunks": total,
                # 同名文件允许覆盖，避免重复部署时报"已存在"
                "force": "true",
            },
            timeout=max(self.settings.panel_timeout, 60),
        )
        upload_id = str(init.get("uploadId") or "")
        if not upload_id:
            raise PanelError("管理端没有返回 uploadId")

        sem = asyncio.Semaphore(self.settings.panel_concurrency)
        done = 0
        lock = asyncio.Lock()

        async def send(index: int) -> None:
            nonlocal done
            async with sem:
                chunk = await asyncio.to_thread(_read_chunk, path, index * PANEL_CHUNK)
                if not chunk:
                    raise PanelError(f"分片 {index} 读取为空")
                for attempt in range(3):
                    try:
                        await self._request(
                            "POST",
                            "/upload/chunk",
                            data={"uploadId": upload_id, "chunkIndex": str(index)},
                            files={"chunk": ("chunk", chunk, "application/octet-stream")},
                            timeout=max(self.settings.panel_timeout, 120),
                        )
                        break
                    except PanelError as exc:
                        if exc.status == 401 or attempt == 2:
                            raise
                        await asyncio.sleep(1.5 * (attempt + 1))
            async with lock:
                done += 1
                if on_progress is not None:
                    await on_progress(done, total)

        try:
            await asyncio.gather(*(send(index) for index in range(total)))
            await self._form(
                "/upload/merge",
                {"uploadId": upload_id, "filename": filename},
                timeout=max(self.settings.panel_timeout, 300),
            )
        except Exception:
            with contextlib.suppress(PanelError):
                await self._form("/upload/cancel", {"uploadId": upload_id})
            raise

    # ------------------------------------------------------------- 服务器操作

    async def hot_reload(self) -> None:
        await self._form("/maps/hot-reload", timeout=max(self.settings.panel_timeout, 300))

    async def add_queue(self, map_name: str, position: str) -> None:
        await self._json(
            "/maps/queue/add", {"map": map_name, "position": position}
        )

    async def start_queue(self, mode: str) -> None:
        await self._json("/maps/queue/start", {"mode": mode})

    async def rcon(self, command: str) -> str:
        response = await self._request("POST", "/rcon", json={"cmd": command})
        return (response.text or "").strip()

    async def map_list(self) -> list:
        data = await self._json("/rcon/maplist")
        items = data.get("maps") or data.get("data") or data
        return items if isinstance(items, list) else []


@dataclass(slots=True)
class PanelDeployResult:
    ok: bool
    steps: list[str] = field(default_factory=list)
    error: str = ""
    map_codes: list[str] = field(default_factory=list)
    map_changed: str = ""


class PanelDeployer:
    """下载 → 上传管理端 → 热重载 → 队列/切图。"""

    def __init__(
        self, settings: Settings, client: httpx.AsyncClient, downloader: CacheDownloader
    ) -> None:
        self.settings = settings
        self.client = PanelClient(settings, client)
        self.downloader = downloader
        self._done: set[int] = set()

    def available(self) -> bool:
        return self.settings.panel_ready()

    def already_done(self, item_id: int) -> bool:
        return item_id in self._done

    async def run(
        self,
        item: WorkshopItem,
        resolved: ResolvedItem,
        *,
        force: bool = False,
        on_step: Callable[[str], Awaitable[None]] | None = None,
    ) -> PanelDeployResult:
        if not self.available():
            return PanelDeployResult(ok=False, error="未配置管理端地址或 token")
        if not force and item.item_id in self._done:
            return PanelDeployResult(ok=False, error="这张图已经部署过")

        result = await self.downloader.download(
            item, resolved, limit_mb=self.settings.panel_max_mb
        )
        if not result.ok or result.path is None:
            return PanelDeployResult(ok=False, error=result.error or "下载失败")
        path = result.path

        steps = [f"下载完成：{human_size(result.size)}"]
        codes: list[str] = []
        try:
            codes = await asyncio.to_thread(read_map_codes, path)
        except (VPKError, OSError, ValueError):
            codes = []

        try:
            await self.client.upload_map(path)
            steps.append(f"已上传到管理端：{path.name}")
            if on_step is not None:
                await on_step("已上传")

            mode = self.settings.panel_after_upload
            if mode == "none":
                self._done.add(item.item_id)
                return PanelDeployResult(ok=True, steps=steps, map_codes=codes)

            await self.client.hot_reload()
            steps.append("已热重载地图")
            if on_step is not None:
                await on_step("已热重载")

            if mode == "hot_reload":
                self._done.add(item.item_id)
                return PanelDeployResult(ok=True, steps=steps, map_codes=codes)

            start_map = pick_start_map(codes)
            if not start_map:
                steps.append("没解析到地图代码，跳过换图")
                self._done.add(item.item_id)
                return PanelDeployResult(
                    ok=True, steps=steps, map_codes=codes, error="未解析到地图代码"
                )

            if mode == "queue":
                await self.client.add_queue(start_map, self.settings.panel_queue_position)
                await self.client.start_queue(self.settings.panel_queue_mode)
                label = "立即切图" if self.settings.panel_queue_mode == "now" else "下一战役"
                steps.append(f"已加入换图队列：{start_map}（{label}）")
                self._done.add(item.item_id)
                return PanelDeployResult(
                    ok=True, steps=steps, map_codes=codes, map_changed=start_map
                )

            # changelevel
            output = await self.client.rcon(f"changelevel {start_map}")
            steps.append(f"已发送 changelevel {start_map}")
            if output:
                steps.append(f"RCON：{output[:80]}")
            self._done.add(item.item_id)
            return PanelDeployResult(
                ok=True, steps=steps, map_codes=codes, map_changed=start_map
            )
        except PanelError as exc:
            return PanelDeployResult(
                ok=False, steps=steps, error=str(exc), map_codes=codes
            )


def _read_chunk(path: Path, offset: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        return handle.read(PANEL_CHUNK)
