"""把解析结果拼成群里能看懂的一段话。

刻意保持纯文本：QQ 群对长文本最宽容，图片卡片反而容易糊，
而且纯文本里的链接可以被直接点开 / 复制。
"""

from __future__ import annotations

from .config import Settings
from .mirror import ResolvedItem
from .steam import WorkshopItem
from .video import VideoInfo


def human_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "未知大小"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def human_count(num: int) -> str:
    if num <= 0:
        return "0"
    if num >= 10000:
        return f"{num / 10000:.1f} 万".replace(".0 ", " ")
    return f"{num:,}"


def _shorten(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def direct_status(resolved: ResolvedItem) -> str:
    if resolved.direct_ok is None:
        return "（未探测）"
    if not resolved.direct_ok:
        return "（探测失败，建议走镜像或 steamcmd）"
    if resolved.direct_is_vpk:
        return "（已实测 · VPK 文件头校验通过）"
    return "（已实测可下载）"


def render_item(
    resolved: ResolvedItem,
    *,
    video: VideoInfo | None,
    settings: Settings,
) -> str:
    item = resolved.item
    lines: list[str] = ["🎮 求生之路2 · 创意工坊地图", f"🗺 {_shorten(item.display_name, 80)}"]

    author = resolved.author_name or "未知"
    meta = [f"👤 {author}", human_size(resolved.direct_size or item.file_size)]
    if item.subscriptions:
        meta.append(f"订阅 {human_count(item.subscriptions)}")
    lines.append("｜".join(meta))

    if item.tags:
        lines.append("🏷 " + " / ".join(item.tags[:4]))

    if settings.include_steam_page:
        lines.append(f"🔗 工坊页：{item.workshop_url}")

    if resolved.direct_url:
        lines.append(f"⬇️ 直链{direct_status(resolved)}")
        lines.append(resolved.direct_url)

    if resolved.mirrors:
        lines.append("🚀 备用镜像")
        for mirror in resolved.mirrors:
            if mirror.note:
                lines.append(f"· {mirror.label}：{mirror.url}（{mirror.note}）")
            else:
                lines.append(f"· {mirror.label}：{mirror.url}")

    lines.append(f"🎮 一键订阅：{item.subscribe_url}")
    if settings.include_steamcmd:
        lines.append(f"🖥 服务器下载：{item.steamcmd}")

    if resolved.dependencies:
        lines.append("📦 依赖（不装可能缺材质 / 报错）")
        for dep in resolved.dependencies:
            lines.append(
                f"· {_shorten(dep.display_name, 48)}｜{human_size(dep.file_size)}｜{dep.workshop_url}"
            )

    if video is not None:
        lines.append(f"🎬 视频：{_shorten(video.title or video.url, 60)}")

    if resolved.warnings:
        lines.append("⚠️ " + "；".join(resolved.warnings))

    return "\n".join(lines)


def render_missing_link(video: VideoInfo) -> str:
    title = _shorten(video.title or video.url, 60)
    return (
        f"🎬 《{title}》\n"
        "简介里没找到 Steam 创意工坊链接。\n"
        "链接可能被 B 站折叠在「展开更多」里，或放在置顶评论 —— 手动发一下工坊链接，我也能解析。"
    )


def render_invalid(item_id: int, reason: str) -> str:
    return f"❌ 工坊物品 {item_id} 解析失败：{reason}"


def render_not_l4d2(item: WorkshopItem, app_name: str = "") -> str:
    app = app_name or f"AppID {item.app_id}"
    return f"ℹ️ 《{_shorten(item.display_name, 60)}》不是求生之路2的物品（{app}），已跳过。"


def render_help() -> str:
    return (
        "🧭 求生之路2地图镜像助手\n"
        "· 群里发 B 站/抖音视频，简介带工坊链接时自动回复下载地址\n"
        "· /l4d2 <工坊链接或 ID> 手动解析\n"
        "· /l4d2状态 检查接口与镜像线路"
    )
