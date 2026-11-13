# 求生之路2地图镜像助手

两段式：**群里 `/解析 <视频/工坊链接> [群文件夹]` → 下载地图、压成 7z、发到指定群文件夹；群主再用 `/上传地图` → 机器人登录 L4D2 Manager 管理端完成部署。**

- 视频平台：B 站、抖音、YouTube
- 只认真货：工坊物品必须是求生之路2（AppID `550`），别的游戏自动跳过
- 直链先实测：Range 请求 + VPK 文件头校验，死链不往外发
- 第一段：`/解析 <链接>` → 下载 vpk → 7z 打包 → `upload_group_file` 发群文件
- 合集/多 Part：所有子物品的 VPK 下载后打成一个包，一次发全
- 竹烨 `l4d2-workshop`：自动补齐 `required_items`（必需分包）和合集 `children`
- `/搜索地图 <地图名>`：视频简介没链接时手动搜工坊，返回一张结果图
- 群文件夹：命令里指定，或 `/解析 设置群文件夹` 记住本群默认
- 第二段：群主/管理员 `/上传地图`，机器人调管理端 `/upload/*` 接口
- 国内可用：Steam API 直连 + CDN 直链 + 竹烨工坊镜像 + 可自建反代
- Steam API 被墙/超时时，自动用竹烨镜像兜底取标题、直链和大小

## 效果

**第一段**，群友发命令：

```
/解析 https://www.bilibili.com/video/BV1oKWM6CEVw L4D2地图
```

机器人回卡片，随后发群文件：

```
🎮 求生之路2 · 创意工坊地图
🗺 生化危机：爆发夜 - 前往浣熊市
👤 Xue｜139.8 MB｜订阅 3,667
⬇️ 直链（已实测 · VPK 文件头校验通过）
https://cdn.steamusercontent.com/ugc/...
🚀 备用镜像
· 竹烨工坊解析（网页直下）：https://zhrradiant.com/tools/workshop-accelerator
🎮 一键订阅：steam://url/CommunityFilePage/3836808875
🖥 服务器下载：steamcmd +login anonymous +workshop_download_item 550 3836808875 +quit
📦 正在下载并打包（139.8 MB），完成后发群文件 → L4D2地图
```

```
📦 已发群文件：生化危机：爆发夜.zip（126.3 MB，zip）
群主/管理员可 /上传地图 3836808875 部署到服务器
```

**第二段**，群主：

```
/上传地图 3836808875
```

```
🖥 已上传到管理端：生化危机：爆发夜 - 前往浣熊市
· 生化危机：爆发夜.zip（126.3 MB）
如要开图，请在管理端点「热重载地图」，或等下次换图。
```

## 安装

1. 把整个 `astrbot_plugin_l4d2_map_mirror` 目录放进：

   ```
   <AstrBot 数据目录>/data/plugins/
   ```

   桌面版一般是 `C:\Users\<你>\.astrbot\data\plugins\`。

2. 管理面板 → 插件管理 → 刷新 → 启用「求生之路2地图镜像助手」。
3. 装个 [7-Zip](https://www.7-zip.org/)（推荐），或在配置里填 `seven_zip_path`；没装会自动回退 zip。
4. 到插件配置里填管理端地址/token（第二段才需要）。

除了 `Pillow`（`/搜索地图` 生成图片用），没有其他三方包；`httpx` 是 AstrBot 自带的。
AstrBot 会按 `requirements.txt` 自动安装 Pillow；手动部署可先 `pip install pillow`。

## 给别人用

- **同一个机器人、同一个群**：别人不用装任何东西，直接发 `/解析`、`/搜索地图`、`/换一批`。
- **同一个机器人、其他群**：把机器人拉进群，在 AstrBot 会话白名单加上 `Left4:GroupMessage:<群号>`，再 `/设置群文件夹`。
- **别人自己部署**：把整个插件目录（或发布 zip）放进对方的 `data/plugins/`，由对方自己填 `panel_token`、`seven_zip_path`、OneBot 连接；不要共用你的面板授权码。

## 配置

| 配置项 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | 开 | 总开关 |
| `group_only` | 开 | 只在群聊生效 |
| `require_at` | 开 | 自动模式才生效：必须 @机器人 |
| `auto_link_enabled` | 关 | 打开后恢复“@机器人 + 链接”自动处理；默认只用 `/解析` |
| `folder_auto_create` | 开 | 群文件夹不存在时尝试自动创建 |
| `miniprogram_enabled` | 开 | 解析视频小程序卡片 |
| `miniprogram_require_keyword` | 开 | 卡片必须命中关键词才解析 |
| `miniprogram_keywords` | 求生之路 / l4d2 / left 4 dead / 求2 / 三方图 | 卡片关键词 |
| `miniprogram_allowed_sources` | 空 | 允许的卡片来源；空=不限，可填 哔哩哔哩/抖音/YouTube |
| `whitelist` / `blacklist` | 空 | 按会话 UMO 放行/屏蔽 |
| `video_platforms` | bilibili, douyin, youtube | 启用的平台 |
| `max_items_per_message` | 2 | 一条视频最多处理几张图，防刷屏 |
| `use_zhrradiant` | 开 | 竹烨镜像补链、必需分包/合集、作者/依赖 |
| `verify_links` | 开 | 回复前实测直链（+VPK 校验） |
| `custom_mirrors` | 空 | 自建镜像，见 [mirror/README.md](mirror/README.md) |
| `bilibili_cookie` | 空 | B 站 Cookie，遇到 412/风控时填 |
| `bilibili_proxy` | 空 | B 站专用代理；留空用全局 `proxy`，都空则直连 |
| `proxy` | 空 | 给机器人所有请求用的 HTTP 代理；可能影响本地管理端，优先用 `bilibili_proxy` |
| `dedupe_ttl_hours` | 12 | 同一张图 12 小时内只回一次 |
| `send_file_to_group` | 开 | 第一段：打包发群文件 |
| `send_file_max_mb` | 1024 | 超过这个大小不发群文件，只发链接 |
| `group_file_folder` | 空 | 默认群文件夹名，留空为根目录 |
| `upload_permission` | owner_admin | 谁能上传/设文件夹：群主+管理员 / 仅群主 |
| `allow_private_upload` | 开 | 私聊也能 `/上传地图`，方便测试 |
| `upload_timeout` | 900 | 传群文件超时（秒） |
| `upload_notify` | 开 | 第一段/第二段结果补一条消息 |
| `seven_zip_path` | 空 | `7z.exe` 路径；留空自动找 |
| `archive_level` | 3 | 7z 压缩等级 1~9 |
| `archive_ttl_hours` | 72 | 待部署压缩包保留时长 |
| `panel_enabled` | 关 | 允许第二段上传到管理端 |
| `panel_url` | 空 | 如 `http://127.0.0.1:5090` |
| `panel_token` | 空 | 面板密码或授权码（建议 admin 授权码） |
| `panel_concurrency` | 3 | 上传分片并发 |
| `panel_timeout` | 120 | 管理端请求超时 |
| `panel_max_mb` | 1024 | 上传管理端的压缩包上限 |

## 命令

| 命令 | 作用 |
| --- | --- |
| `/解析 <视频/工坊链接> [群文件夹]` | 第一段：下载 → 7z → 发到指定群文件夹 |
| `/解析 设置群文件夹 <名称>` | 设置本群默认文件夹；不带参数查看 |
| `/上传地图 [序号/文件名/工坊ID]` | 从本群指定文件夹选压缩包上传；不带参数列出文件 |
| `/解析 状态` | 自检：Steam、竹烨、7z、管理端、待部署、默认文件夹 |
| `/解析 只看 <链接或ID>` | 只解析，不发文件 |
| `/搜索地图 <地图名>` | 搜索工坊，返回结果图（预览、ID、订阅数、作者） |
| `/换一批` | 同一关键词看下一批搜索结果 |
| `/l4d2 <链接或ID>` | 旧命令，只解析 |

别名：`/下载地图`、`/群文件夹`、`/部署地图`。

## 两段式流程

**第一段（任何群成员）**

1. `/解析 <链接> [文件夹]`；
2. 机器人抓简介/链接里的工坊 ID；
3. 校验是 L4D2（AppID 550）；
4. 下载 vpk（优先 Steam CDN，失败走镜像）；
5. 压成 7z（没有 7z 就 zip）；
6. 解析群文件夹名 → folder_id（不存在且 `folder_auto_create` 开启时尝试创建）；
7. `upload_group_file` 发到该文件夹（或根目录）；
8. 在 `pending.json` 记一条待部署。

**第二段（群主/管理员）**

1. `/上传地图`：列出本群指定文件夹里的压缩包（`.7z/.zip/.rar/.vpk`）；
2. `/上传地图 <序号>` 或 `/上传地图 <文件名片段>` 选一个；也可以 `/上传地图 <工坊ID>` 用待部署记录；
3. 校验发言人是群主/管理员（`get_group()` 的 owner/admins）；
4. 本地缓存有就用本地文件，没有就 `get_group_file_url` 从群文件下载；
5. 登录管理端（Bearer token）；
6. `POST /upload/init` → `POST /upload/chunk`（5MB 分片，3 并发）→ `POST /upload/merge`；
7. 按 `panel_after_upload` 决定是否 `POST /maps/hot-reload`。

## 指定群文件夹

OneBot 的 `upload_group_file` 只认 `folder_id`，不认名称，所以插件会：

1. 调 `get_group_root_files` 列出群文件夹；
2. 按名称匹配（大小写不敏感）；
3. 没找到且 `folder_auto_create=true` → `create_group_file_folder` 创建后再查一次；
4. 仍然找不到 → 发到根目录，并在群里提示。

两种用法：

```
/解析 设置群文件夹 L4D2地图        # 记住本群默认
/解析 https://b23.tv/xxx L4D2地图   # 只对这一次生效
```

默认文件夹优先级：命令参数 > `/解析 设置群文件夹` > `group_file_folder` > 根目录。

## 视频小程序卡片

群友发的是 B 站/抖音等小程序卡片，或直接发“标题 + 链接”的文字时，有两种用法：

1. **发完卡片/带链接的文字后 10 分钟内直接发 `/解析`**（插件会记住本群最近几条内容）；
2. 或者**引用那张卡片/那条消息再发 `/解析`**。

为了不乱解析，默认有三道限制：

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `miniprogram_enabled` | 开 | 卡片解析总开关 |
| `miniprogram_require_keyword` | 开 | 卡片标题/简介必须命中关键词 |
| `miniprogram_keywords` | 求生之路、l4d2、left 4 dead、求2、三方图、三方地图 | 命中任意一个才处理 |
| `miniprogram_allowed_sources` | 空 | 非空时还要求卡片文本出现来源名，如 哔哩哔哩 |
| `miniprogram_recent_seconds` | 600 | 发完卡片后多久内可直接 `/解析`；0=关闭 |
| `search_enabled` | 开 | 启用 `/搜索地图` 工坊搜索 |
| `search_results` | 6 | 结果图显示几个，1~6 |

流程：

1. 只通过 `/解析` 触发（`auto_link_enabled` 关闭时，@机器人 不会自动解析卡片）；
2. 从当前消息、引用消息，或最近 `miniprogram_recent_seconds` 内的卡片里找视频平台 URL；
3. 关键词不命中 → 回「卡片标题/简介不像求生之路2地图，已跳过」；
4. 命中 → 走正常视频解析、下载、打包；
5. 卡片里没有 URL → 提示「卡片里没找到视频链接」。

注意：纯标题卡片没有链接时不会自动猜，但可以用 `/搜索地图 <标题>` 手动搜。

## 手动搜索

视频简介没写工坊链接时，直接按地图名搜：

```
/搜索地图 广西灵山
```

机器人会调竹烨 `l4d2-workshop` 搜索接口，返回一张结果图，包含：

- 地图预览图
- 标题
- 工坊 ID
- 订阅数
- 作者

图里最多显示 `search_results`（默认 6）个结果。看到目标后直接：

```
/解析 <图里的工坊ID>
```

如果这批里没有想要的，发：

```
/换一批
```

会按同一个关键词取下一批（30 分钟内有效），并自动跳过已经展示过的 ID。

`/解析` 失败时也会提示用视频标题去 `/搜索地图`。

## 合集 / 多个 VPK

有些地图是工坊**合集**，或主物品需要额外订阅多个 **Pack/材质包** 才能玩。插件会：

1. 先展开 Steam 返回的合集 `children`；
2. 再查竹烨 `l4d2-workshop` 详情接口的 `required_items` / `children_ids`；
3. 把所有相关物品逐个下载；
4. 全部放进同一个目录，打成一个 `.7z`（或 zip）；
5. 只发**一个压缩包**，群友解压后就是完整的一套。

例如「广西灵山 V3.1」会返回主图 + Pack-2 + Pack-3，共 3 个 VPK。

卡片会提示：

```
🧩 《合集标题》共 3 个文件，合计 512.4 MB
📦 正在下载并打包（512.4 MB），完成后发群文件 → 今日游玩
```

完成后：

```
📦 已发群文件：广西灵山 V3.1.7z（380.2 MB，7z）
🧩 合集共 3 个 VPK，已打成一个包
群主/管理员可 /上传地图 123456 部署到服务器
```

上传管理端时用合集的工坊 ID（父物品 ID），不是子物品 ID。

## 打包

- 优先调用 `7z.exe a -t7z -mx=3`；`seven_zip_path` 留空时自动找 PATH、
  `C:\Program Files\7-Zip\7z.exe`、`D:\7-Zip\7z.exe`、`/usr/bin/7z`。
- `7zFM.exe` / `7zG.exe` 是图形界面，不能命令行压缩，会被自动跳过。
- 找不到 7z 或 7z 执行失败 → 回退标准库 `zipfile`，群里会提示一句。
- 压缩包名：`<地图名>.7z`，例如 `Beyond the Exosphere.7z`、`广西灵山 V3.1.7z`；
  没有标题时才退回工坊 ID。
- 包内 VPK 同样重命名为 `<地图名>.vpk`，不带 `<工坊ID>_` 前缀；
  上传管理端时也会去掉文件名开头的工坊 ID。
- 压缩包保存在 `data/plugin_data/astrbot_plugin_l4d2_map_mirror/archives/`，
  超过 `archive_ttl_hours` 且已上传的会被清理。

## 部署到管理端

对接的是 **L4D2 Manager v1.14+**（`Q1en/L4D2-Manager-Panel`）的接口：

```
POST /auth              Bearer 校验
POST /upload/init       {filename, fileSize, totalChunks}
POST /upload/chunk      multipart: uploadId, chunkIndex, chunk
POST /upload/merge      {uploadId, filename}
```

- `panel_token` 填面板密码，或「授权码」页生成的授权码；
  建议用 **有期限、可撤销的 admin 授权码**，不要长期用主密码。
- 管理端的上传 UI 接受 `.vpk/.zip/.rar/.7z`，压缩包会由面板侧处理。
- `panel_after_upload=hot_reload` 时，上传成功会自动调 `POST /maps/hot-reload`；
  设为 `none` 则只上传，手动去面板点热重载。
- 401 会在群里提示 "token 无效或已过期"，不会刷屏。

## 从群文件夹选图上传

上传来源是**本群设置的那个群文件夹**：

```
/设置群文件夹 今日游玩
```

之后群主/管理员发：

```
/上传地图
```

机器人会列出该文件夹里的压缩包：

```
📂 今日游玩 里的压缩包：
1. 广西灵山 V3.1.7z（380.2 MB）
2. 安全室广告涂鸦1.0.7z（0.7 MB）
发送 /上传地图 <序号> 或 /上传地图 <文件名片段> 上传。
```

然后：

```
/上传地图 1
```

也可以 `/上传地图 广西灵山` 按文件名片段选。

本地 `archives/` 里还有这个压缩包就直接用；没有就通过 OneBot
`get_group_file_url` 从群文件下载回来，再传给管理端。

用 `/上传地图 <工坊ID>` 时优先走第一段留下的待部署记录，能精确对应到本地文件。

## 它怎么工作

```
群消息（/解析）
  ├─ 纯文本 / QQ 卡片 / 视频小程序卡片（Json 段）→ 抽 URL
  ├─ 识别视频平台 → 取 标题 + 简介
  │    ├─ B 站：b23.tv 解短链 → api.bilibili.com/x/web-interface/view
  │    ├─ 抖音：v.douyin.com → 分享页 _ROUTER_DATA
  │    └─ YouTube：yt-dlp（可选）→ 页面 shortDescription
  ├─ 正则抽工坊 ID（sharedfiles / workshop / steam://）
  ├─ Steam Web API 查详情 → consumer_app_id == 550 ?（失败时竹烨兜底）
  ├─ 取 file_url 直链 → Range 探测 + VPK 头校验
  └─ 下载 → 7z → 发群文件 → 记待部署
```

判断是不是"求生之路2地图"靠工坊物品的 AppID，不是视频标题关键词。

## 本地缓存

下载的 vpk 会留在：

```
data/plugin_data/astrbot_plugin_l4d2_map_mirror/cache/<工坊ID>_<文件名>
```

- 支持断点续传（`.part`），失败下次接着下
- 超过 `cache_max_mb` 的不下
- 启动时清理超过 `cache_cleanup_days` 的旧文件
- 填了 `steamcmd_path` 就先走官方 steamcmd，失败了再回退 HTTP

## 测试

离线单元测试（不联网，27 条）：

```bash
python -m unittest discover -s tests -v
```

覆盖：链接/工坊 ID 抽取、QQ 卡片解析、配置解析、回复排版、去重、
本地上传/断点续传/体积上限、7z/zip 打包、待部署记录、管理端分片上传。

联机冒烟测试（需要 AstrBot 的 Python 环境）：

```bash
E:\AstrBot\backend\python\python.exe -X utf8 tests/live_smoke.py
```

会真实打 B 站 + Steam API，跑视频分享、工坊链接、非 L4D2 物品、命令等用例。

## 常见问题

**Q：为什么 /解析 没反应？**
链接里没有工坊 ID、视频简介没写链接，或命令没被唤醒（AstrBot 的命令前缀/白名单）。
先用 `/l4d2 <链接>` 验证解析，再看日志有没有 `[l4d2]`。

**Q：为什么发的是 zip 不是 7z？**
没找到 `7z.exe`，或 7z 执行失败。装 7-Zip 或在 `seven_zip_path` 填绝对路径。

**Q：群文件太大传不上去？**
QQ 群文件有上限，机器人协议端也有超时。调小 `send_file_max_mb`，超过就只发链接。

**Q：群主上传提示没权限？**
默认只认群主+管理员。`upload_permission` 改 `owner` 就只认群主；
机器人需要能调 `get_group_member_list`（NapCat/SnowLuma 都支持）。

**Q：上传管理端 401？**
token 失效或不是 admin 角色。到面板「授权码」重新生成，填进 `panel_token`。

**Q：上传完服务器里没图？**
机器人只负责上传到管理端 maps 目录。还需要在面板点「热重载地图」，
或在面板的地图页确认文件已落盘。

## 目录结构

```
astrbot_plugin_l4d2_map_mirror/
├── main.py                 # 事件入口、两段式流程、命令
├── metadata.yaml
├── _conf_schema.json
├── core/
│   ├── config.py           # 配置收敛
│   ├── links.py            # URL / 工坊 ID 抽取
│   ├── video.py            # B站 / 抖音 / YouTube
│   ├── steam.py            # Steam Web API + 物品模型
│   ├── mirror.py           # 直链探测、竹烨、自定义镜像
│   ├── downloader.py       # 下载 + 断点续传
│   ├── archive.py          # 7z / zip 打包
│   ├── pending.py          # 待部署记录
│   ├── group_upload.py     # OneBot 群文件上传
│   ├── panel.py            # L4D2 Manager 客户端
│   ├── vpk.py              # VPK 地图代码解析
│   ├── render.py           # 回复排版
│   └── dedupe.py           # 去重
├── mirror/                 # 自建镜像模板
└── tests/
```

## 已知限制

- 只解析**视频简介**，不读评论区/弹幕。
- 抖音接口无官方文档，改版后可能失效（会静默跳过）。
- YouTube 需要 `yt-dlp` 才能读简介。
- 群文件上传依赖 OneBot `upload_group_file`；官方 QQ 机器人（qq_official）不支持。
- 管理端支持上传 + 可选热重载；不会自动切图/改战役。
- 管理端 UI 虽接受 7z，但面板侧是否自动解包取决于它的版本；若上传后没图，先传 vpk 验证。

## License

MIT License，详见 [LICENSE](LICENSE)。
