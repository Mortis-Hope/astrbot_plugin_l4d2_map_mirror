"""插件配置读取与校验。

AstrBot 会把 ``_conf_schema.json`` 中的默认值合并进 ``AstrBotConfig``，
这里再统一做一次类型收敛，避免用户手改配置后出现 ``str/int`` 混用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int, low: int | None = None, high: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = _as_str(value).lower()
    return text if text in allowed else default


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.splitlines() if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


@dataclass(slots=True)
class CustomMirror:
    """用户自定义镜像。

    模板占位符：
    ``{id}`` 工坊 ID、``{url}`` 官方直链、``{url_encoded}`` 直链的 URL 编码、
    ``{workshop_url}`` 工坊页面、``{filename}`` 文件名、``{title}`` 地图标题。
    """

    name: str
    template: str
    enabled: bool = True

    def build(self, values: dict[str, str]) -> str:
        try:
            return self.template.format(**values)
        except (KeyError, IndexError, ValueError):
            return self.template


@dataclass(slots=True)
class Settings:
    enabled: bool = True
    group_only: bool = True
    whitelist: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)

    video_platforms: list[str] = field(default_factory=lambda: ["bilibili", "douyin", "youtube"])
    max_videos_per_message: int = 2
    max_items_per_message: int = 2
    handle_direct_workshop: bool = True
    hint_when_no_link: bool = False
    mention_sender: bool = False

    use_zhrradiant: bool = True
    include_dependencies: bool = True
    include_steamcmd: bool = True
    include_steam_page: bool = True
    verify_links: bool = True
    verify_timeout: int = 10
    custom_mirrors: list[CustomMirror] = field(default_factory=list)

    bilibili_cookie: str = ""
    bilibili_proxy: str = ""
    proxy: str = ""
    user_agent: str = DEFAULT_UA
    request_timeout: int = 20

    dedupe_ttl_hours: int = 12

    auto_cache: bool = False
    cache_max_mb: int = 800
    cache_cleanup_days: int = 7
    steamcmd_path: str = ""
    public_base_url: str = ""

    send_file_to_group: bool = True
    send_file_max_mb: int = 1024
    group_file_folder: str = ""
    upload_timeout: int = 900
    upload_notify: bool = True
    require_at: bool = True
    auto_link_enabled: bool = False
    folder_auto_create: bool = True
    miniprogram_enabled: bool = True
    miniprogram_require_keyword: bool = True
    miniprogram_keywords: list[str] = field(
        default_factory=lambda: [
            "求生之路",
            "l4d2",
            "left 4 dead",
            "求2",
            "三方图",
            "三方地图",
        ]
    )
    miniprogram_allowed_sources: list[str] = field(default_factory=list)
    miniprogram_recent_seconds: int = 600
    search_enabled: bool = True
    search_results: int = 6
    seven_zip_path: str = ""
    archive_level: int = 3
    archive_ttl_hours: int = 72
    upload_permission: str = "owner_admin"
    allow_private_upload: bool = True

    panel_enabled: bool = False
    panel_url: str = ""
    panel_token: str = ""
    panel_after_upload: str = "hot_reload"
    panel_queue_position: str = "back"
    panel_queue_mode: str = "after_campaign"
    panel_concurrency: int = 3
    panel_timeout: int = 120
    panel_max_mb: int = 1024

    @classmethod
    def load(cls, raw: Any) -> Settings:
        data: dict[str, Any] = dict(raw or {}) if not isinstance(raw, dict) else raw

        mirrors: list[CustomMirror] = []
        for row in _as_list(data.get("custom_mirrors")):
            if "|" not in row:
                continue
            name, template = row.split("|", 1)
            name, template = name.strip(), template.strip()
            if not template:
                continue
            mirrors.append(CustomMirror(name=name or "自定义镜像", template=template))

        platforms = [p.lower() for p in _as_list(data.get("video_platforms"))]
        if not platforms:
            platforms = ["bilibili", "douyin", "youtube"]

        mp_keywords = _as_list(data.get("miniprogram_keywords"))
        if not mp_keywords:
            mp_keywords = ["求生之路", "l4d2", "left 4 dead", "求2", "三方图", "三方地图"]

        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            group_only=_as_bool(data.get("group_only"), True),
            whitelist=_as_list(data.get("whitelist")),
            blacklist=_as_list(data.get("blacklist")),
            video_platforms=platforms,
            max_videos_per_message=_as_int(data.get("max_videos_per_message"), 2, 1, 5),
            max_items_per_message=_as_int(data.get("max_items_per_message"), 2, 1, 5),
            handle_direct_workshop=_as_bool(data.get("handle_direct_workshop"), True),
            hint_when_no_link=_as_bool(data.get("hint_when_no_link"), False),
            mention_sender=_as_bool(data.get("mention_sender"), False),
            use_zhrradiant=_as_bool(data.get("use_zhrradiant"), True),
            include_dependencies=_as_bool(data.get("include_dependencies"), True),
            include_steamcmd=_as_bool(data.get("include_steamcmd"), True),
            include_steam_page=_as_bool(data.get("include_steam_page"), True),
            verify_links=_as_bool(data.get("verify_links"), True),
            verify_timeout=_as_int(data.get("verify_timeout"), 10, 3, 60),
            custom_mirrors=mirrors,
            bilibili_cookie=_as_str(data.get("bilibili_cookie")),
            bilibili_proxy=_as_str(data.get("bilibili_proxy")),
            proxy=_as_str(data.get("proxy")),
            user_agent=_as_str(data.get("user_agent")) or DEFAULT_UA,
            request_timeout=_as_int(data.get("request_timeout"), 20, 5, 120),
            dedupe_ttl_hours=_as_int(data.get("dedupe_ttl_hours"), 12, 0, 168),
            auto_cache=_as_bool(data.get("auto_cache"), False),
            cache_max_mb=_as_int(data.get("cache_max_mb"), 800, 1, 20480),
            cache_cleanup_days=_as_int(data.get("cache_cleanup_days"), 7, 0, 365),
            steamcmd_path=_as_str(data.get("steamcmd_path")),
            public_base_url=_as_str(data.get("public_base_url")).rstrip("/"),
            send_file_to_group=_as_bool(data.get("send_file_to_group"), True),
            send_file_max_mb=_as_int(data.get("send_file_max_mb"), 1024, 1, 4096),
            group_file_folder=_as_str(data.get("group_file_folder")),
            upload_timeout=_as_int(data.get("upload_timeout"), 900, 30, 3600),
            upload_notify=_as_bool(data.get("upload_notify"), True),
            require_at=_as_bool(data.get("require_at"), True),
            auto_link_enabled=_as_bool(data.get("auto_link_enabled"), False),
            folder_auto_create=_as_bool(data.get("folder_auto_create"), True),
            miniprogram_enabled=_as_bool(data.get("miniprogram_enabled"), True),
            miniprogram_require_keyword=_as_bool(
                data.get("miniprogram_require_keyword"), True
            ),
            miniprogram_keywords=mp_keywords,
            miniprogram_allowed_sources=_as_list(
                data.get("miniprogram_allowed_sources")
            ),
            miniprogram_recent_seconds=max(
                0, _as_int(data.get("miniprogram_recent_seconds"), 600)
            ),
            search_enabled=_as_bool(data.get("search_enabled"), True),
            search_results=_as_int(data.get("search_results"), 6, 1, 6),
            seven_zip_path=_as_str(data.get("seven_zip_path")),
            archive_level=_as_int(data.get("archive_level"), 3, 1, 9),
            archive_ttl_hours=_as_int(data.get("archive_ttl_hours"), 72, 0, 720),
            upload_permission=_choice(
                data.get("upload_permission"), {"owner_admin", "owner"}, "owner_admin"
            ),
            allow_private_upload=_as_bool(data.get("allow_private_upload"), True),
            panel_enabled=_as_bool(data.get("panel_enabled"), False),
            panel_url=_as_str(data.get("panel_url")).rstrip("/"),
            panel_token=_as_str(data.get("panel_token")),
            panel_after_upload=_choice(
                data.get("panel_after_upload"),
                {"none", "hot_reload", "queue", "changelevel"},
                "hot_reload",
            ),
            panel_queue_position=_choice(
                data.get("panel_queue_position"), {"back", "front"}, "back"
            ),
            panel_queue_mode=_choice(
                data.get("panel_queue_mode"), {"now", "after_campaign"}, "after_campaign"
            ),
            panel_concurrency=_as_int(data.get("panel_concurrency"), 3, 1, 5),
            panel_timeout=_as_int(data.get("panel_timeout"), 120, 15, 1800),
            panel_max_mb=_as_int(data.get("panel_max_mb"), 1024, 1, 8192),
        )

    def panel_ready(self) -> bool:
        return bool(self.panel_enabled and self.panel_url and self.panel_token)

    def allows(self, unified_msg_origin: str) -> bool:
        if self.whitelist and unified_msg_origin not in self.whitelist:
            return False
        return unified_msg_origin not in self.blacklist
