# bili_monitor — B 站监控助手（NeoBot 插件）

把桌面版「**bili 监控助手 1.7.20**」（易语言 DLL，支持 MyQQ / IR / NTQQ 等多框架）移植成
NeoBot 2.1 插件：订阅 UP 主后，机器人会在群里/私聊推送 **新动态、转发、新视频、
新专栏、新音频、开播、下播、粉丝里程碑** 提醒。

原来的窗口配置改成 **网页面板配置 + 聊天命令**，多框架发送统一交给 NeoBot 适配器，
不再需要 `推送QQ / 推送群号` 的 .ini 文件。

```
plugins/
  bili_monitor/
    plugin.toml
    __init__.py
    bili_api.py      # B 站接口：buvid、WBI 签名、风控退避、扫码登录
    bili_engine.py   # 动态 / 直播 / 粉丝三条轮询循环
    bili_model.py    # 动态分类与事件归一化
    bili_render.py   # 文案模板与卡片 HTML
    bili_store.py    # SQLite：订阅、目标、游标、去重
    bili_notify.py   # 交给宿主发送 / 截图渲染
    bili_config.py   # 面板配置模型
    tests/
```

> **完整操作手册见 [使用说明.md](使用说明.md)**（安装、命令明细、模板、配方、排障）。

## 安装

**网页面板**：插件 → 安装 → 填仓库地址（或上传打包好的 `plugin.zip`），选择 `bili_monitor`。

**手动**：把本目录整个放到 NeoBot 数据目录的 `plugins/bili_monitor/`，重启或热重载。

要求 NeoBot **2.1.0 及以上**；`python_dependencies` 里的 `httpx / sqlalchemy / aiosqlite /
pydantic` 由面板自动安装（本体已自带）。

## 快速开始

1. 面板里打开 `bili_monitor` 的配置，按需填 **B 站登录 Cookie**（可选，见下）；
2. 在群里 @机器人：

```
/b站管理 add 546195        # 订阅「老番茄」，提醒推到当前群
/b站监控 list              # 看订阅列表
/b站管理 test 546195       # 发一条测试提醒，确认链路
```

3. 之后该 UP 主有新动态/开播就会自动推送；`/b站监控 status` 看轮询与推送统计。

## 命令

### `/b站监控`（所有人）

| 子命令 | 说明 |
| --- | --- |
| `list` | 订阅列表（默认） |
| `info <uid>` | 某个 UP 主的监控配置与游标 |
| `status` | 轮询间隔、事件数、推送成功/失败、接口风控次数 |
| `help` | 命令帮助 |

### `/b站管理`（次级管理员）

| 子命令 | 说明 |
| --- | --- |
| `add <uid\|空间链接> [直播间号]` | 添加监控，默认推送到当前会话 |
| `del <uid>` | 取消监控 |
| `on <uid>` / `off <uid>` | 恢复 / 暂停 |
| `cat <uid> <类别,...>` | 提醒类别，支持 `动态,转发,视频,专栏,音频,开播,下播,粉丝`、`all`、`none` |
| `mode <uid> <auto\|text\|image\|card\|default>` | 发送模式 |
| `at <uid> <on\|off>` | 开播提醒 @全体成员（需要机器人有群管理员权限） |
| `atlist <uid> <qq,qq\|clear>` | 提醒时 @指定成员 |
| `tpl <uid> <类别> <模板\|clear>` | 自定义文案；`tpl <uid> <类别> ?` 查占位符 |
| `targets [uid]` | 查看推送目标，含当前会话的群号 |
| `bind <uid>` | 把当前会话加为推送目标（旧名 `pushhere`） |
| `push <uid> <群号>` / `push <uid> 私聊 <QQ号>` | 追加推送目标（也认 `group:123`） |
| `unpush <uid> <群号\|私聊 QQ>` | 移除推送目标 |
| `test <uid>` | 发一条测试提醒 |
| `check <uid>` | 立即检查一次（不等轮询） |
| `sync <uid>` | 刷新昵称 / 头像 / 直播间号 |

### `/b站登录`（超级管理员）

| 子命令 | 说明 |
| --- | --- |
| `qr` | 扫码登录 B 站账号，凭证保存在插件数据目录 |
| `status` | 当前登录状态 |
| `logout` | 清除本地凭证（面板里的 `cookie` 配置不受影响） |

## 配置项

面板表单由 `bili_config.py` 生成，`plugin.toml` 的 `[config]` 是打包默认值。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `cookie` | 空 | `SESSDATA=...; bili_jct=...`，填了显著降低风控概率；面板只显示「已设置/未设置」 |
| `dynamic_interval_seconds` | 120 | 动态轮询间隔（60~3600） |
| `live_interval_seconds` | 90 | 开播/下播轮询间隔（30~1800） |
| `follower_interval_seconds` | 1800 | 粉丝数轮询间隔 |
| `send_mode` | `auto` | `auto` 有图发图、`text` 纯文本、`image` 文本+原图、`card` 渲染卡片 |
| `push_gap_seconds` | 3 | 两条提醒之间的最小间隔（旧版的「防屏蔽延迟」） |
| `max_events_per_cycle` | 6 | 单轮最多推送条数，超出跳过并记日志 |
| `at_all` | false | 新订阅默认 @全体 |
| `at_all_on_live_only` | true | @全体只用于开播提醒 |
| `include_link` | true | 附带原文链接 |
| `max_text_chars` | 180 | 正文截断长度 |
| `max_subscriptions` | 30 | 订阅上限 |
| `proxy` | 空 | 请求代理 |
| `qr_image_service` | qrserver | 扫码登录的二维码图片服务模板，`{data}` 替换为二维码内容；留空只发链接 |

## 提醒类别

| 内部键 | 来源 | 中文 |
| --- | --- | --- |
| `dynamic_new` | 动态流图文/纯文字 | 发布新动态 |
| `dynamic_repost` | `DYNAMIC_TYPE_FORWARD` | 转发动态 |
| `dynamic_video` | `MAJOR_TYPE_ARCHIVE` / PGC / 合集 | 投稿新视频 |
| `dynamic_article` | `MAJOR_TYPE_ARTICLE` | 发布新专栏 |
| `dynamic_audio` | `MAJOR_TYPE_MUSIC` | 发布新音频 |
| `live_on` / `live_off` | 直播接口状态机 | 开播 / 下播 |
| `follower` | 粉丝数跨越 1000 的整数倍 | 粉丝里程碑 |

单条动态分类与旧版一致：一条动态只归到一个类别（视频投稿不会再重复发「新动态」）。
**置顶动态会被跳过**，避免订阅时把旧内容当新动态推送。

## 文案模板

`tpl` 支持占位符，缺字段会替换成空串（不会报错）：

| 类别 | 可用占位符 |
| --- | --- |
| 动态 | `{name} {uid} {title} {text} {url} {time}` |
| 转发 | 另加 `{orig_author} {orig_text} {orig_title}` |
| 视频 | 另加 `{duration} {play_text} {bvid}` |
| 开播 | `{name} {title} {url} {area} {online_text} {room_id} {time}` |
| 下播 | `{name} {title} {url} {duration_text} {peak_text} {room_id} {time}` |
| 粉丝 | `{name} {follower_text} {milestone_text} {url} {time}` |

例：`/b站管理 tpl 546195 开播 【开播啦】{name} 正在播《{title}》，快来 直播间：{url}`

## 与旧版的能力对照

| 旧版（1.7.20） | 本插件 |
| --- | --- |
| 桌面窗口配置 | 网页面板表单 + `[config]` 注释说明 |
| `监控任务.ini` / `订阅任务\UID.ini` / `开播信息.ini` | SQLite（`plugins_data/bili_monitor/databases/bili_monitor.db`） |
| 推送 QQ / 推送群号 | 每个订阅多个推送目标（群/私聊），`bind` / `push` |
| 开播·下播·新动态·转发·新视频·新专栏·新音频·千粉提醒 | 全部支持，按类别独立开关 |
| 自定义提醒文案 | `tpl` 命令，占位符与旧版字段对应 |
| 图片模式（远程 PHP 截图服务） | `image` 文本+原图；`card` 用本体截图端口渲染本地卡片 |
| 防屏蔽延迟（10 秒） | `push_gap_seconds`（默认 3 秒，可调） |
| AT 全体（需群管理员） | `at` 命令 + `atlist` 指定成员 |
| 扫码登录 B 站 | `/b站登录 qr`，凭证存插件数据目录 |
| 自动更新（下载 exe/dll） | 交给 NeoBot 面板的「检查更新 / 更新此插件」 |
| 爱发电动态监控 | **未移植**：依赖爱发电私有接口，与本插件「B 站监控」定位不同 |
| 数据统计 / 付费接口（data_stats.php） | **未移植**：旧的第三方收费统计服务，插件本身不依赖 |

## 风控与稳定性

- 每个请求都带 `buvid3/buvid4` 指纹、空间页 `Referer` 与 `Origin`；`feed/space`、
  `space/wbi/acc/info` 走 **WBI 签名**（`w_rid`+`wts`）；
- 触发 `-352/-412/HTTP 412` 时全局冷却（5~10 分钟）并指数退避，不会越撞越死；
- **游标落库 + 事件去重**：重启不会重复提醒，推送失败下一轮会重试；
- 首次订阅只记基线不推送历史，避免「刚订阅就刷屏」；
- 动态接口返回的 `play` 常是 `"237.2万"` 这类字符串，已统一解析成整数。

## 排障

| 现象 | 处理 |
| --- | --- |
| `/b站监控 status` 里风控命中一直涨 | 填 `cookie` 或 `/b站登录 qr`；调大轮询间隔 |
| 日志反复「风控冷却中，还需等待 N 秒」 | 1.0.1 起冷却期整轮跳过、同类提示 10 分钟只记一次；仍频繁说明请求密度高，填 cookie 并把动态间隔调到 180s+ |
| 加订阅后昵称显示成 UID | 当时在冷却期；冷却结束 `/b站管理 sync <uid>` 补全 |
| 加订阅提示「没有找到 UID」 | 确认 UID 正确；风控冷却期间稍后再试 |
| 开播提醒发不出去 | @全体需要机器人是群管理员；看日志里的发送失败原因 |
| 卡片模式没效果 | 本体未启用截图（浏览器）；会自动降级成 `image` 模式 |
| 想改端口/服务 | 本插件不监听端口，无需重启 NeoBot；改配置后重载插件即可 |

### 风控策略（1.0.1）

| 触发 | 冷却时长 |
| --- | --- |
| `-352`（请求太快） | 90 秒起 |
| `-412` / HTTP 412 / 429 | 600 秒起 |
| 连续命中 | 指数退避，上限 30 分钟 |
| 成功一次 | 计数清零 |

冷却期间不发起任何 B 站请求；`add` 仍可建订阅（资料等 `sync` 补），
`sync` / `check` / `/b站登录 qr` 直接回剩余秒数。


## 开发

```bash
# 在 NeoBot 源码根目录（已 uv sync）下
uv run --no-sync python -m pytest ../../plugins/bili_monitor/tests

# 静态契约与打包
uv run --no-sync python -m neobot_modloader.sdk check ../bili_monitor
uv run --no-sync python -m neobot_modloader.sdk export ../bili_monitor
uv run --no-sync python scripts/plugin_ci.py ../bili_monitor --output-dir tmp/bili_ci
```

测试覆盖：动态分类、文案渲染、持久化（真实 SQLite）、轮询引擎（基线/去重/类别过滤/
直播状态机/粉丝里程碑/限速）、WBI 签名、以及**用真实 modloader 跑一遍插件生命周期**
（迁移、命令注册、启停清理）。

## License

MIT
