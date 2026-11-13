"""地图打包：优先 7z.exe，找不到就回退 zip。

QQ 群文件和面板上传都走压缩包。7z 体积更小，但依赖本机装了 7-Zip；
没装时用标准库 zip 保底，保证功能不断。
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


@dataclass(slots=True)
class ArchiveResult:
    path: Path
    fmt: str  # "7z" | "zip"
    size: int
    note: str = ""


def find_7z(configured: str = "") -> str:
    candidates: list[str] = []
    if configured:
        candidates.append(configured)
    for name in ("7z", "7za"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates.extend(
        [
            r"C:\Program Files\7-Zip\7z.exe",
            r"C:\Program Files (x86)\7-Zip\7z.exe",
            r"D:\7-Zip\7z.exe",
            r"E:\7-Zip\7z.exe",
            "/usr/bin/7z",
            "/usr/bin/7zz",
        ]
    )
    for candidate in candidates:
        try:
            if not candidate or not Path(candidate).is_file():
                continue
            # 7zFM/7zG 是图形界面，不能接 a -t7z 这类命令行
            if Path(candidate).name.lower() in {"7zfm.exe", "7zg.exe"}:
                continue
            return str(candidate)
        except OSError:
            continue
    return ""


def safe_stem(item_id: int, title: str, filename: str) -> str:
    """压缩包名优先用地图标题，没有标题才退回原始文件名/工坊 ID。"""
    base = title.strip() or (Path(filename).stem if filename else "")
    base = _BAD_CHARS.sub("_", base or "").strip(" .")
    if not base:
        return str(item_id)
    if len(base) > 60:
        base = base[:60]
    return base


def archive_member_name(item_id: int, title: str, filename: str) -> str:
    """压缩包内层 VPK 的名字：地图名 + 原扩展名，不带工坊 ID。"""
    suffix = Path(filename).suffix.lower() or ".vpk"
    return f"{safe_stem(item_id, title, filename)}{suffix}"


def _archive_stem(name: str, source: Path) -> str:
    base = (name or "").strip() or source.stem
    base = _BAD_CHARS.sub("_", base).strip(" .")
    return (base or source.stem)[:60]


def make_archive(
    source: Path,
    out_dir: Path,
    *,
    seven_zip: str = "",
    level: int = 3,
    name: str = "",
    member_name: str = "",
) -> ArchiveResult:
    """把 vpk/目录压成 ``.7z``；7z 不可用或失败时回退 ``.zip``。

    ``name`` 是压缩包名（不含扩展名）；``member_name`` 是包内文件名。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = _archive_stem(name, source)
    out_7z = out_dir / f"{stem}.7z"
    if out_7z.exists():
        out_7z.unlink(missing_ok=True)

    work_source = source
    staging: Path | None = None
    if member_name and source.is_file() and source.name != member_name:
        staging = out_dir / f".stage_{source.stem}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        work_source = staging / member_name
        try:
            os.link(source, work_source)
        except OSError:
            shutil.copy2(source, work_source)

    try:
        exe = find_7z(seven_zip)
        if exe:
            try:
                if source.is_dir():
                    command = [
                        exe,
                        "a",
                        "-t7z",
                        f"-mx={max(1, min(9, level))}",
                        "-y",
                        str(out_7z),
                        "*",
                    ]
                    workdir = str(source)
                else:
                    command = [
                        exe,
                        "a",
                        "-t7z",
                        f"-mx={max(1, min(9, level))}",
                        "-y",
                        str(out_7z),
                        str(work_source),
                    ]
                    workdir = None
                completed = subprocess.run(
                    command,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=1800,
                )
                if (
                    completed.returncode == 0
                    and out_7z.is_file()
                    and out_7z.stat().st_size > 0
                ):
                    return ArchiveResult(
                        path=out_7z, fmt="7z", size=out_7z.stat().st_size
                    )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        out_zip = out_dir / f"{stem}.zip"
        with zipfile.ZipFile(
            out_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            if source.is_dir():
                for path in source.rglob("*"):
                    if path.is_file():
                        archive.write(path, path.relative_to(source).as_posix())
            else:
                archive.write(work_source, arcname=member_name or source.name)
        note = "没找到 7z.exe，已回退 zip" if not exe else "7z 打包失败，已回退 zip"
        return ArchiveResult(
            path=out_zip, fmt="zip", size=out_zip.stat().st_size, note=note
        )
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)


async def make_archive_async(
    source: Path,
    out_dir: Path,
    *,
    seven_zip: str = "",
    level: int = 3,
    name: str = "",
    member_name: str = "",
) -> ArchiveResult:
    return await asyncio.to_thread(
        make_archive,
        source,
        out_dir,
        seven_zip=seven_zip,
        level=level,
        name=name,
        member_name=member_name,
    )
