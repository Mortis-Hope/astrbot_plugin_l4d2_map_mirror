"""待部署地图记录：第一段发群文件后落一条，第二段群主上传时取。

存成 ``pending.json``，按群隔离；超过 TTL 自动清理。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass(slots=True)
class PendingMap:
    group_id: str
    item_id: int
    title: str
    archive_path: str
    fmt: str
    size: int
    created_at: float = field(default_factory=time.time)
    uploaded_at: float = 0.0

    @property
    def uploaded(self) -> bool:
        return self.uploaded_at > 0


class PendingStore:
    def __init__(self, path: Path, ttl_hours: int = 72) -> None:
        self.path = path
        self.ttl = max(0, ttl_hours) * 3600
        self._items: list[PendingMap] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._items = []
            return
        items: list[PendingMap] = []
        for row in raw if isinstance(raw, list) else []:
            try:
                items.append(PendingMap(**row))
            except (TypeError, ValueError):
                continue
        self._items = items
        self.prune()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps([asdict(item) for item in self._items], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def prune(self) -> int:
        if self.ttl <= 0:
            return 0
        deadline = time.time() - self.ttl
        before = len(self._items)
        self._items = [
            item for item in self._items if item.created_at >= deadline or item.uploaded
        ]
        removed = before - len(self._items)
        if removed:
            self._save()
        return removed

    def add(
        self,
        *,
        group_id: str,
        item_id: int,
        title: str,
        archive_path: Path,
        fmt: str,
        size: int,
    ) -> PendingMap:
        self._items = [
            item
            for item in self._items
            if not (item.group_id == group_id and item.item_id == item_id)
        ]
        record = PendingMap(
            group_id=str(group_id),
            item_id=item_id,
            title=title,
            archive_path=str(archive_path),
            fmt=fmt,
            size=size,
        )
        self._items.append(record)
        self._save()
        return record

    def find(self, group_id: str, item_id: int | None = None) -> PendingMap | None:
        rows = [item for item in self._items if item.group_id == str(group_id)]
        if item_id is not None:
            rows = [item for item in rows if item.item_id == item_id]
        else:
            rows = [item for item in rows if not item.uploaded] or rows
        if not rows:
            return None
        return max(rows, key=lambda item: item.created_at)

    def mark_uploaded(self, record: PendingMap) -> None:
        record.uploaded_at = time.time()
        self._save()

    def recent(self, group_id: str, limit: int = 5) -> list[PendingMap]:
        rows = [item for item in self._items if item.group_id == str(group_id)]
        rows.sort(key=lambda item: item.created_at, reverse=True)
        return rows[:limit]

    def count(self, group_id: str | None = None) -> int:
        if group_id is None:
            return len(self._items)
        return sum(1 for item in self._items if item.group_id == str(group_id))
