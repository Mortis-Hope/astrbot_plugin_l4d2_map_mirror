# 镜像方案说明

这个插件默认**不依赖你自建镜像**也能用：Steam Web API 会直接给出工坊文件的 CDN 直链
（`cdn.steamusercontent.com`），国内多数网络能连，插件回复前还会实测一次
（Range 请求 + VPK 文件头校验）。

但国内网络对 Steam CDN 的连通性并不稳定，所以按"从省事到彻底"给了三层方案。

---

## 第 0 层：什么都不搭（默认）

- 直链：`https://cdn.steamusercontent.com/ugc/...`，回复里标注"已实测"。
- 网页镜像：竹烨柠檬的 [工坊链接解析](https://zhrradiant.com/tools/workshop-accelerator)，
  国内可直连，把工坊链接粘进去就能在网页上直接下。
- 官方命令：`steamcmd +login anonymous +workshop_download_item 550 <id> +quit`。

覆盖 90% 的场景，先跑起来再说。

---

## 第 1 层：让机器人自己当镜像（最实用）

适合：机器人跑在**有公网 IP 的服务器**上，或群友和机器人在同一局域网。

1. 打开插件配置里的 `auto_cache`；
2. 用 nginx / caddy 把缓存目录映射成静态站点：

```nginx
location /l4d2/ {
    alias /opt/astrbot/data/plugin_data/astrbot_plugin_l4d2_map_mirror/cache/;
    autoindex off;
    add_header Accept-Ranges bytes;
}
```

caddy 更简单：

```
handle_path /l4d2/* {
    root * /opt/astrbot/data/plugin_data/astrbot_plugin_l4d2_map_mirror/cache
    file_server
}
```

3. 配置 `public_base_url: https://你的域名/l4d2`。

之后每条回复都会多一行：

```
📦 机器人镜像：https://你的域名/l4d2/3836808875_shwj.vpk（后台缓存，完成后可下）
```

缓存目录在 `data/plugin_data/astrbot_plugin_l4d2_map_mirror/cache/`，
文件名格式 `<工坊ID>_<原始文件名>`，开服玩家可以直接丢进
`left4dead2/addons/`。

> 带宽提示：一张三方图 100MB~1GB，群友同时下载会吃满上传。
> 建议只给开黑小群开，或者在 nginx 里加 `limit_rate`。

---

## 第 2 层：自建反代（给"CDN 完全连不上"的机器）

插件支持 `custom_mirrors` 模板，填一行就能接入任何反代。

### 2.1 Cloudflare Worker

文件：[`worker.js`](./worker.js)

```bash
npm i -g wrangler
wrangler deploy worker.js --name l4d2-mirror
# 可选：wrangler secret put MIRROR_TOKEN
```

部署后：

```
https://l4d2-mirror.<你的账号>.workers.dev/file?url=<encodeURIComponent(直链)>
```

写进插件配置：

```
CF镜像|https://l4d2-mirror.xxx.workers.dev/file?url={url_encoded}
```

> ⚠️ `*.workers.dev` 在国内经常打不开。要给群友用，请在 Cloudflare 绑定一个
> 国内可访问的自有域名（Custom Domain），否则只能自己用。

### 2.2 自己的 VPS + nginx

文件：[`nginx.conf.example`](./nginx.conf.example)

按注释改好域名后，插件配置：

```
我的镜像|https://mirror.example.com/l4d2/cdn/{path}
```

`{path}` 是直链去掉域名后的路径，例如
`ugc/16973183041107065040/5C1011828E23889D26C4FABA7FED68A6AB233D20/`。

### 2.3 模板占位符

`custom_mirrors` 每行 `名称|模板`，可用：

| 占位符 | 含义 |
| --- | --- |
| `{id}` | 工坊物品 ID |
| `{url}` | Steam 直链 |
| `{url_encoded}` | 直链的 URL 编码（塞进 `?url=` 用） |
| `{host}` | 直链域名 |
| `{path}` | 直链路径（不带开头的 `/`） |
| `{workshop_url}` | 工坊页面地址 |
| `{filename}` | 原始文件名（如 `shwj.vpk`） |
| `{title}` | 地图标题 |

填了 `custom_mirrors` 后，机器人会用 Range 请求逐条探活，
**只把能下的一起回复**，探不通的自动略过。

---

## 第 3 层：官方 steamcmd（CDN 全挂时的兜底）

```bash
steamcmd +login anonymous +workshop_download_item 550 3836808875 +quit
```

文件落在 `steamapps/workshop/content/550/3836808875/`。

插件配置里填 `steamcmd_path` 后，`auto_cache` 会优先走这条官方通道，
下载完自动复制进缓存目录 —— 比 HTTP 直链更抗封锁。

---

## 线路对比

| 线路 | 需要什么 | 国内可用性 | 速度 | 稳定性 |
| --- | --- | --- | --- | --- |
| Steam CDN 直链 | 无 | 多数可连，少数地区超时 | 中~快 | 官方，最稳 |
| 竹烨工坊解析 | 无 | 可 | 中 | 第三方，可能改版 |
| 机器人本地缓存 | 公网/局域网 | 取决于你的线路 | 快 | 自己掌控 |
| CF Worker | Cloudflare + 自有域名 | 域名可访问即可 | 快 | 自己掌控 |
| VPS + nginx | VPS | 好 | 取决于 VPS | 自己掌控 |
| steamcmd | 装个 steamcmd | 官方通道，较稳 | 中 | 官方 |

实际回复会同时给出多条，群友哪条能点开用哪条。
