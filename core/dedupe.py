"""去重：同一张图在 TTL 内只回复一次，防止刷屏。"""

from __future__ import annotations

import time


class DedupeStore:
    def __init__(self, ttl_seconds: int, max_entries: int = 5000) -> None:
        self.ttl = max(0, ttl_seconds)
        self.max_entries = max_entries
        self._seen: dict[str, float] = {}
        self._last_gc = time.time()

    def hit(self, key: str) -> bool:
        """命中过返回 True，否则记录并返回 False。"""
        if self.ttl <= 0:
            return False
        now = time.time()
        self._gc(now)
        stamp = self._seen.get(key)
        if stamp is not None and now - stamp < self.ttl:
            return True
        self._seen[key] = now
        return False

    def forget(self, key: str) -> None:
        self._seen.pop(key, None)

    def _gc(self, now: float) -> None:
        if now - self._last_gc < 300 and len(self._seen) < self.max_entries:
            return
        self._last_gc = now
        if self.ttl > 0:
            expired = [key for key, stamp in self._seen.items() if now - stamp >= self.ttl]
            for key in expired:
                self._seen.pop(key, None)
        if len(self._seen) > self.max_entries:
            for key in sorted(self._seen, key=self._seen.get)[: len(self._seen) - self.max_entries]:
                self._seen.pop(key, None)
