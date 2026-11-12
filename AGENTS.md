# AGENTS.md — bili_monitor

给开发助手的最小上下文：这是 NeoBot 2.1 的第三方插件，移植自桌面版「bili 监控助手 1.7.20」。

## 边界

- 不修改 NeoBot 本体代码；只用 `neobot_modloader` 公开 API（`Plugin` / `Reply` /
  `MessageChain` / `Migration` / `RenderOptions` / `ctx.*`）。
- 发送一律走 `ctx.send` / `ctx.send_group` / `ctx.send_private` / `ctx.send_image`，
  不自己建连接、不持有适配器。
- 长任务用 `ctx.create_task`；停止时在 `on_shutdown` 里取消并释放（`engine.stop()`、`api.close()`）。

## 结构

| 文件 | 职责 |
| --- | --- |
| `__init__.py` | Plugin 声明、命令分发、生命周期、Agent 工具、使用说明 |
| `bili_api.py` | httpx 客户端：buvid、WBI 签名、风控冷却、扫码登录 |
| `bili_model.py` | 纯函数：动态分类、事件构造、里程碑计算 |
| `bili_engine.py` | 三条轮询循环、去重、推送节流 |
| `bili_render.py` | 文案模板、消息链、卡片 HTML |
| `bili_store.py` | SQLAlchemy 模型 + 迁移 + 仓储 |
| `bili_notify.py` | 宿主发送器与截图渲染器 |

## 必须遵守的行为约定

1. **首次订阅只记基线**，不推送历史动态；置顶动态跳过。
2. 游标推进必须在推送成功之后；失败靠 `claim_event` 去重后重试。
3. 单条动态只归一个类别（视频投稿不再重复算「新动态」）。
4. 风控错误（`-352/-412/412`）触发全局冷却，不要把异常吞掉继续打接口。
5. 文案模板缺字段必须替换成空串，不能抛 `KeyError`/`ValueError`。

## 提交前

```bash
uv run --no-sync python -m pytest tests
uv run --no-sync python -m neobot_modloader.sdk check .
uv run --no-sync python -m neobot_modloader.sdk export .
```
