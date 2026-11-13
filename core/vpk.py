"""VPK 目录树解析：只做一件事 —— 读出地图代码。

部署到服务器后要 ``changelevel <map>``，而工坊元数据里没有地图代码，
只能从 vpk 里的 ``maps/*.bsp`` 拿。这里只解析目录树，不读文件内容，
一张 700MB 的图也能毫秒级读完。

格式参考 Valve VPK：
- 头：magic 0x55AA1234 + version(1/2)
- v1 头 12 字节，v2 头 28 字节，随后是目录树
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from pathlib import Path

VPK_SIGNATURE = 0x55AA1234
DIR_VPK_INDEX = 0x7FFF
_MAX_TREE = 64 * 1024 * 1024


class VPKError(Exception):
    """不是可读的 VPK，或者目录树损坏。"""


@dataclass(slots=True)
class VPKEntry:
    path: str


class _Cursor:
    __slots__ = ("buf", "pos")

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def cstr(self) -> str:
        end = self.buf.find(b"\x00", self.pos)
        if end < 0:
            raise VPKError("目录树字符串未结束")
        raw = self.buf[self.pos:end]
        self.pos = end + 1
        return raw.decode("utf-8", "replace")

    def skip(self, size: int) -> None:
        if self.pos + size > len(self.buf):
            raise VPKError("目录树被截断")
        self.pos += size


def is_vpk(path: str | Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4)
    except OSError:
        return False
    return len(head) == 4 and struct.unpack("<I", head)[0] == VPK_SIGNATURE


def read_entries(path: str | Path) -> list[VPKEntry]:
    file_path = Path(path)
    size = file_path.stat().st_size
    with file_path.open("rb") as handle:
        head = handle.read(28)
        if len(head) < 12:
            raise VPKError("文件太小，不是 VPK")
        signature, version = struct.unpack_from("<II", head, 0)
        if signature != VPK_SIGNATURE:
            raise VPKError("VPK 签名不对（可能是 zip 改名）")
        if version == 1:
            header_size = 12
        elif version == 2:
            if len(head) < 28:
                raise VPKError("VPK v2 头被截断")
            header_size = 28
        else:
            raise VPKError(f"不支持的 VPK 版本 {version}")

        tree_size = struct.unpack_from("<I", head, 8)[0]
        if tree_size <= 0 or tree_size > _MAX_TREE or header_size + tree_size > size:
            raise VPKError("目录树大小异常")
        handle.seek(header_size)
        tree = handle.read(tree_size)

    return _parse_tree(tree)


def _parse_tree(tree: bytes) -> list[VPKEntry]:
    entries: list[VPKEntry] = []
    cursor = _Cursor(tree)
    while True:
        ext = cursor.cstr()
        if not ext:
            break
        while True:
            directory = cursor.cstr()
            if not directory:
                break
            directory = directory.replace("\\", "/").strip("/")
            while True:
                name = cursor.cstr()
                if not name:
                    break
                if cursor.pos + 18 > len(cursor.buf):
                    raise VPKError("目录树被截断")
                _crc, preload, _archive, _offset, _length, term = struct.unpack_from(
                    "<IHHIIH", cursor.buf, cursor.pos
                )
                if term != 0xFFFF:
                    raise VPKError("目录项格式不对")
                cursor.skip(18 + preload)
                full = f"{directory}/{name}.{ext}" if directory else f"{name}.{ext}"
                entries.append(VPKEntry(path=full))
    return entries


_CHAPTER_RE = re.compile(r"(?:^|[_-])(?:c\d+)?m(\d+)(?:[_-]|$)", re.IGNORECASE)


def _chapter_number(code: str) -> int:
    match = _CHAPTER_RE.search(code)
    return int(match.group(1)) if match else 99


def read_map_codes(path: str | Path) -> list[str]:
    """返回 vpk 内的地图代码（不含 ``maps/`` 和 ``.bsp``），按章节号排序。"""
    codes: list[str] = []
    for entry in read_entries(path):
        lower = entry.path.lower()
        if not lower.startswith("maps/") or not lower.endswith(".bsp"):
            continue
        code = Path(lower).stem
        if code and code not in codes:
            codes.append(code)
    codes.sort(key=lambda item: (_chapter_number(item), item))
    return codes


def pick_start_map(codes: list[str]) -> str:
    """挑一张适合做 ``changelevel`` 的起始图：优先 m1，其次章节号最小的。"""
    if not codes:
        return ""
    for code in codes:
        if _chapter_number(code) == 1:
            return code
    return codes[0]
