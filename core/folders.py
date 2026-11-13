"""群文件夹：按名称解析 folder_id，并记住每个群的默认文件夹。

OneBot 的 ``upload_group_file`` 只认 ``folder``（folder_id），不认名称，
所以要先 ``get_group_root_files`` 找，找不到就 ``create_group_file_folder``
再找一次。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class FolderStore:
    """每个群记住一个默认文件夹：``{group_id: {"name":..., "id":...}}``。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
            return
        self._data = {
            str(key): value
            for key, value in (raw.items() if isinstance(raw, dict) else [])
            if isinstance(value, dict)
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(self.path)

    def get(self, group_id: str) -> dict[str, str] | None:
        return self._data.get(str(group_id))

    def set(self, group_id: str, name: str, folder_id: str) -> None:
        self._data[str(group_id)] = {"name": name, "id": folder_id}
        self._save()

    def reset(self, group_id: str) -> None:
        self._data.pop(str(group_id), None)
        self._save()


class FolderResolver:
    def __init__(self, store: FolderStore, auto_create: bool = True) -> None:
        self.store = store
        self.auto_create = auto_create
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._ttl = 600.0

    async def resolve(self, bot: Any, group_id: str, name: str) -> str:
        """把文件夹名解析成 folder_id；解析不到返回 ``"/"``（根目录）。"""
        name = (name or "").strip()
        if not name or name in {"/", "根目录", "群文件"}:
            return "/"

        key = (str(group_id), name)
        cached = self._cache.get(key)
        if cached and time.time() - cached[0] < self._ttl:
            return cached[1]

        folder_id = await self._find(bot, group_id, name)
        if folder_id is None and self.auto_create:
            await self._create(bot, group_id, name)
            folder_id = await self._find(bot, group_id, name)

        if folder_id is None:
            return "/"
        self._cache[key] = (time.time(), folder_id)
        return folder_id

    async def _find(self, bot: Any, group_id: str, name: str) -> str | None:
        try:
            data = await bot.call_action(
                "get_group_root_files", group_id=_as_int(group_id)
            )
        except Exception:  # noqa: BLE001 - 协议端差异
            return None
        folders = []
        if isinstance(data, dict):
            folders = data.get("folders") or data.get("folder_list") or []
        for folder in folders if isinstance(folders, list) else []:
            if not isinstance(folder, dict):
                continue
            folder_name = str(folder.get("folder_name") or folder.get("name") or "")
            if folder_name.strip().lower() == name.strip().lower():
                folder_id = folder.get("folder_id") or folder.get("id")
                if folder_id:
                    return str(folder_id)
        return None

    async def _create(self, bot: Any, group_id: str, name: str) -> None:
        try:
            await bot.call_action(
                "create_group_file_folder",
                group_id=_as_int(group_id),
                name=name,
            )
        except Exception:  # noqa: BLE001 - 创建失败就当没有
            return


def _as_int(value: str) -> int | str:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else text
