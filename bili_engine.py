"""轮询引擎：动态 / 直播 / 粉丝三条独立循环，带限速、去重与风控退避。

设计要点（对应旧版插件的几个关键行为）：
- **首次订阅只记基线不推送**，避免把历史动态刷屏；
- **游标落库 + 事件去重**，重启或推送失败都不会重复提醒；
- **全局推送间隔** 对应旧版的「防屏蔽延迟」；
- 单个 UP 主失败不影响其它订阅，风控时整体退避。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
import time
from typing import Any, Protocol

from .bili_api import BiliApiError, BiliClient, BiliCooldownError, LiveRoomSnapshot
from .bili_config import cfg
from .bili_model import (
    DYNAMIC_KINDS,
    MonitorEvent,
    build_follower_event,
    build_live_off,
    build_live_on,
    crossed_milestone,
    dynamic_id,
    is_pinned,
    parse_dynamic_item,
)
from .bili_notify import OFFICIAL, media_allowed
from .bili_render import TEMPLATE_FIELDS, build_chain, build_tuwen, link_chain, render_text
from .bili_store import BiliStore, Subscription

#: 单次轮询里个别订阅失败后的冷却（秒）：指数退避，上限 30 分钟
_MAX_BACKOFF = 1800


class Sender(Protocol):
    async def send_chain(self, kind: str, target_id: str, chain: Any) -> bool: ...

    async def send_image(self, kind: str, target_id: str, data: bytes, filename: str) -> bool: ...


class CardRenderer(Protocol):
    @property
    def available(self) -> bool: ...

    async def render(self, event: MonitorEvent, custom: dict[str, str] | None) -> bytes | None: ...


class MonitorEngine:
    def __init__(
        self,
        *,
        api: BiliClient,
        store: BiliStore,
        sender: Sender,
        cards: CardRenderer | None,
        config: Any,
        logger: Any,
    ) -> None:
        self._api = api
        self._store = store
        self._sender = sender
        self._cards = cards
        self._config = config
        self._logger = logger
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._due: dict[tuple[int, str], float] = {}
        self._failures: dict[int, int] = {}
        self._gate_lock = asyncio.Lock()
        self._last_push = 0.0
        self._cooldown_logged = 0.0
        #: 图片通道是否被判死刑（官方通道上传失败过一次就不再尝试）
        self._media_blocked = False
        self.stats = {
            "cycles": 0, "events": 0, "pushed": 0, "skipped": 0,
            "failed": 0, "last_error": "", "last_push_at": 0.0, "cooldown_left": 0,
        }

    # ---------------------------------------------------------------- 生命周期
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._loop("dynamic", self._dynamic_tick, name="bili-dynamic")),
            asyncio.create_task(self._loop("live", self._live_tick, name="bili-live")),
            asyncio.create_task(self._loop("follower", self._follower_tick, name="bili-follower")),
        ]
        self._log("info", "B 站监控引擎已启动")

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._log("info", "B 站监控引擎已停止")

    @property
    def running(self) -> bool:
        return self._running

    def _log(self, level: str, message: str) -> None:
        try:
            getattr(self._logger, level)(message)
        except Exception:  # pragma: no cover
            pass

    # ---------------------------------------------------------------- 循环框架
    async def _loop(self, kind: str, tick, *, name: str) -> None:
        interval = {
            "dynamic": self._config.dynamic_interval_seconds,
            "live": self._config.live_interval_seconds,
            "follower": cfg(self._config, "follower_interval_seconds"),
        }[kind]
        # 唤醒粒度跟着间隔走：3 秒档必须按秒醒，否则会被 5 秒的调度粒度吃掉
        step = max(1.0, min(15.0, interval / 3))
        while self._running:
            started = time.monotonic()
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 轮询异常不能打死循环
                self.stats["last_error"] = f"{kind}: {exc}"
                self._log("error", f"B 站 {kind} 轮询异常：{exc}")
            self.stats["cycles"] += 1
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, step - elapsed))

    async def _due_subscriptions(self, kind: str, interval: float) -> list[Subscription]:
        subs = await self._store.list_subscriptions(only_enabled=True)
        now = time.monotonic()
        due: list[Subscription] = []
        # 快速档（≤10 秒）抖动收窄，否则 3 秒会被抖成 3.5 秒
        low, high = (1.0, 1.02) if interval <= 10 else (0.9, 1.15)
        for sub in subs:
            key = (sub.id, kind)
            threshold = self._due.get(key, 0.0)
            if now < threshold:
                continue
            # 失败退避：连续失败时把下次到期时间往后推
            failures = self._failures.get(sub.id, 0)
            if failures:
                backoff = min(_MAX_BACKOFF, interval * (2 ** min(failures, 6)))
                threshold = max(threshold, now)
                self._due[key] = threshold + backoff
            else:
                self._due[key] = now + interval * random.uniform(low, high)
            due.append(sub)
        return due

    # ---------------------------------------------------------------- 动态
    async def _dynamic_tick(self) -> None:
        if self._skip_for_cooldown():
            return
        subs = await self._due_subscriptions("dynamic", self._config.dynamic_interval_seconds)
        for sub in subs:
            if not (sub.category_set & set(DYNAMIC_KINDS)):
                continue
            await self._guard(sub, self._poll_dynamic)

    async def _poll_dynamic(self, sub: Subscription) -> None:
        cursor = await self._store.get_cursor(sub.id, "dynamic")
        last_id = 0
        if cursor is not None and str(cursor.value or "").isdigit():
            last_id = int(cursor.value)

        items = await self._api.fetch_dynamic_feed(sub.uid)
        fresh = [item for item in items if not is_pinned(item) and dynamic_id(item) > last_id]
        if not fresh:
            return
        newest = max(dynamic_id(item) for item in fresh)

        if last_id == 0:
            # 首次订阅：只记录基线，不推送历史动态
            await self._store.set_cursor(sub.id, "dynamic", str(newest))
            self._log("info", f"订阅 {sub.display_name} 建立动态基线 {newest}")
            return

        fresh.sort(key=dynamic_id)
        events: list[MonitorEvent] = []
        for item in fresh:
            event = parse_dynamic_item(item, text_limit=cfg(self._config, "max_text_chars"))
            if event is not None:
                events.append(event)

        limit = max(1, int(cfg(self._config, "max_events_per_cycle")))
        if len(events) > limit:
            dropped = len(events) - limit
            events = events[-limit:]
            self.stats["skipped"] += dropped
            self._log("warning", f"{sub.display_name} 单轮 {len(events) + dropped} 条新动态，只推送最新 {limit} 条")

        if not events:
            # 全是无需推送的类型（例如直播推荐）：直接推进游标，避免每轮重复解析
            await self._store.set_cursor(sub.id, "dynamic", str(newest))
            return

        delivered = True
        for event in events:
            if event.kind not in sub.category_set:
                continue
            delivered = await self._dispatch(sub, event) and delivered

        # 推送成功或本来就无需推送时才推进游标；失败的留给下一轮重试（事件去重兜底）
        if delivered:
            await self._store.set_cursor(sub.id, "dynamic", str(newest))

    # ---------------------------------------------------------------- 直播
    async def _live_tick(self) -> None:
        if self._skip_for_cooldown():
            return
        subs = await self._due_subscriptions("live", self._config.live_interval_seconds)
        for sub in subs:
            if not (sub.category_set & {"live_on", "live_off"}):
                continue
            await self._guard(sub, self._poll_live)

    async def _poll_live(self, sub: Subscription) -> None:
        room_id = sub.room_id
        if not room_id:
            room_id = await self._resolve_room_id(sub)
            if not room_id:
                return
        snapshot = await self._api.fetch_live_room(room_id)
        cursor = await self._store.get_cursor(sub.id, "live")
        extra = cursor.extra_map if cursor is not None else {}
        prev_status = int(extra.get("status") or 0)
        peak = int(extra.get("peak") or 0)
        start_ts = int(extra.get("start") or 0)
        prev_title = str(extra.get("title") or "")

        live = snapshot.is_living
        if live:
            if prev_status != 1:
                start_ts = snapshot.live_time or int(time.time())
                peak = snapshot.online
                event = build_live_on(
                    uid=sub.uid, uname=sub.uname or str(sub.uid), face=sub.face,
                    room_id=snapshot.room_id, title=snapshot.title, cover=snapshot.cover or snapshot.keyframe,
                    area=snapshot.parent_area_name or snapshot.area_name, online=snapshot.online,
                    start_ts=start_ts,
                )
                if not await self._dispatch(sub, event):
                    return
                self._log("info", f"{sub.display_name} 开播：{snapshot.title}")
            else:
                peak = max(peak, snapshot.online)
            await self._store.set_cursor(
                sub.id, "live", str(snapshot.room_id),
                {
                    "status": 1, "peak": peak, "start": start_ts or int(time.time()),
                    "title": snapshot.title or prev_title, "check": int(time.time()),
                },
            )
            return

        if prev_status == 1:
            event = build_live_off(
                uid=sub.uid, uname=sub.uname or str(sub.uid), face=sub.face,
                room_id=snapshot.room_id, title=prev_title or snapshot.title,
                cover=snapshot.keyframe or snapshot.cover,
                start_ts=start_ts, end_ts=int(time.time()), peak_online=peak,
            )
            if not await self._dispatch(sub, event):
                return
            self._log("info", f"{sub.display_name} 下播，时长 {event.extra.get('duration_text')}")
        await self._store.set_cursor(
            sub.id, "live", str(snapshot.room_id),
            {"status": snapshot.live_status, "peak": 0, "start": 0, "title": snapshot.title,
             "check": int(time.time())},
        )

    async def _resolve_room_id(self, sub: Subscription) -> int | None:
        try:
            master = await self._api.fetch_live_master(sub.uid)
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 取直播间号失败：{exc}")
            return None
        room_id = master.get("room_id")
        if not room_id:
            return None
        room_id = int(room_id)
        await self._store.update_subscription(sub.uid, room_id=room_id)
        sub.room_id = room_id
        return room_id

    # ---------------------------------------------------------------- 粉丝
    async def _follower_tick(self) -> None:
        if self._skip_for_cooldown():
            return
        subs = await self._due_subscriptions("follower", cfg(self._config, "follower_interval_seconds"))
        for sub in subs:
            if "follower" not in sub.category_set:
                continue
            await self._guard(sub, self._poll_follower)

    async def _poll_follower(self, sub: Subscription) -> None:
        stat = await self._api.fetch_relation_stat(sub.uid)
        follower = int(stat.get("follower") or 0)
        if follower <= 0:
            return
        cursor = await self._store.get_cursor(sub.id, "follower")
        previous = int(cursor.value) if cursor is not None and str(cursor.value or "").isdigit() else 0
        if previous <= 0:
            await self._store.set_cursor(sub.id, "follower", str(follower))
            return
        milestone = crossed_milestone(previous, follower, sub.follower_step)
        if milestone:
            event = build_follower_event(
                uid=sub.uid, uname=sub.uname or str(sub.uid), face=sub.face,
                follower=follower, milestone=milestone,
                space_url=f"https://space.bilibili.com/{sub.uid}",
            )
            if not await self._dispatch(sub, event):
                return
        await self._store.set_cursor(sub.id, "follower", str(follower))

    # ---------------------------------------------------------------- 分发
    async def _guard(self, sub: Subscription, poll) -> None:
        try:
            await poll(sub)
            self._failures.pop(sub.id, None)
        except asyncio.CancelledError:
            raise
        except BiliCooldownError as exc:
            # 冷却不是这个订阅的错：不计失败次数、不逐个订阅刷日志
            self.stats["cooldown_left"] = int(exc.retry_after)
        except BiliApiError as exc:
            self._failures[sub.id] = self._failures.get(sub.id, 0) + 1
            self.stats["failed"] += 1
            self.stats["last_error"] = f"{sub.display_name}: {exc}"
            self._log("warning", f"{sub.display_name} 轮询失败（{exc}），稍后重试")
        except Exception as exc:  # noqa: BLE001
            self._failures[sub.id] = self._failures.get(sub.id, 0) + 1
            self.stats["failed"] += 1
            self.stats["last_error"] = f"{sub.display_name}: {type(exc).__name__}"
            self._log("error", f"{sub.display_name} 轮询异常：{type(exc).__name__}: {exc}")

    def _skip_for_cooldown(self) -> bool:
        """风控冷却期间整轮跳过，避免把冷却越撞越长。"""
        remaining = float(getattr(self._api, "cooldown_remaining", 0.0) or 0.0)
        if remaining <= 0:
            return False
        self.stats["cooldown_left"] = int(remaining)
        now = time.monotonic()
        if now - self._cooldown_logged > 300:
            self._cooldown_logged = now
            self._log("warning", f"B 站风控冷却中（剩余 {remaining:.0f} 秒），本轮跳过")
        return True

    async def _dispatch(self, sub: Subscription, event: MonitorEvent) -> bool:
        """把单个事件推给订阅的所有目标；全部成功返回 True。"""
        targets = [(target.kind, target.target_id) for target in sub.targets]
        if not targets:
            self.stats["skipped"] += 1
            return True
        if not await self._store.claim_event(sub.id, event.kind, event.dedupe_key,
                                             f"{event.kind} {event.title or event.text[:40]}"):
            return True
        self.stats["events"] += 1
        custom = self._custom_templates(sub)
        ok = True
        for kind, target_id in targets:
            try:
                sent = await self._send(sub, event, kind, target_id, custom)
            except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其它目标
                self._log("error", f"推送到 {kind}:{target_id} 失败：{exc}")
                sent = False
            ok = ok and sent
        if ok:
            self.stats["pushed"] += 1
            self.stats["last_push_at"] = time.time()
        return ok

    def _proactive_kwargs(self, proactive: bool) -> dict[str, Any]:
        """宿主支持 proactive 时才传（a9 起官方通道需要它发主动消息）。"""
        if getattr(self._sender, "supports_proactive_kw", False):
            return {"proactive": proactive}
        return {}

    async def _send(self, sub: Subscription, event: MonitorEvent, kind: str, target_id: str,
                    custom: dict[str, str], *, proactive: bool = True) -> bool:
        at_all = bool(sub.at_all) and (
            not cfg(self._config, "at_all_on_live_only") or event.kind == "live_on"
        )
        mode = (sub.send_mode or self._config.send_mode or "auto").strip().lower()
        if mode == "auto":
            mode = "image" if event.images else "text"
        kwargs = self._proactive_kwargs(proactive)

        # 官方通道：优先发「图文卡片」（文字 + 封面 + 整卡可点），与内置开播推送同一套写法
        # 注意不走 _media_enabled()：卡片封面由适配器上传，不受「官方通道不发图」策略限制
        if (
            mode != "text"
            and getattr(self._sender, "channel", "") == OFFICIAL
            and callable(getattr(self._sender, "send_tuwen", None))
        ):
            tuwen = build_tuwen(event)
            if tuwen:
                try:
                    if await self._sender.send_tuwen(kind, target_id, tuwen, **kwargs):
                        return True
                except Exception as exc:  # noqa: BLE001 - 卡片不行就退回文本
                    self._log("warning", f"图文卡片发送失败，改发文本：{exc}")

        # 官方通道上传图片会卡在富媒体（QQ 拉不到本地文件），直接走文字
        if (
            mode == "card"
            and self._cards is not None
            and self._cards.available
            and self._media_enabled()
        ):
            data = None
            try:
                data = await self._cards.render(event, custom)
            except Exception as exc:  # noqa: BLE001 - 渲染失败不能丢提醒
                self._log("warning", f"卡片渲染失败，改发文本：{exc}")
            if data:
                await self._gate()
                sent = False
                try:
                    sent = await self._sender.send_image(
                        kind, target_id, data, f"bili-{event.kind}.png", **kwargs
                    )
                except Exception as exc:  # noqa: BLE001 - 上传失败退回文本
                    self._block_media(f"卡片上传失败：{exc}")
                if sent:
                    # 卡片是图片，补一条文字链接方便点开
                    if self._config.include_link and event.url:
                        await self._gate()
                        await self._send_link(kind, target_id, event, **kwargs)
                    return True

        # 文本 + 配图合成**一条**消息：图片下载成 base64 内联，不再把 URL 交给通道
        chain = build_chain(
            event, custom=custom,
            include_link=bool(self._config.include_link),
            at_all=at_all,
            at_ids=tuple(sub.at_ids),
            with_images=False,
            link_style=self._link_style(),
        )
        if mode == "image":
            for url in await self._inline_images(event):
                chain.image(file=url)
        await self._gate()
        try:
            return await self._sender.send_chain(kind, target_id, chain, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 带图发失败就退回纯文本，别丢提醒
            if mode != "image":
                raise
            self._block_media(f"图文合并发送失败：{exc}")
            fallback = build_chain(
                event, custom=custom,
                include_link=bool(self._config.include_link),
                at_all=at_all,
                at_ids=tuple(sub.at_ids),
                with_images=False,
                link_style=self._link_style(),
            )
            await self._gate()
            return await self._sender.send_chain(kind, target_id, fallback, **kwargs)

    async def _inline_images(self, event: MonitorEvent) -> list[str]:
        """把原图下载成 ``base64://``，供同一消息链内联发送。"""
        if not self._media_enabled():
            return []
        limit = max(0, int(cfg(self._config, "max_images") or 0))
        if not limit:
            return []
        inline: list[str] = []
        for url in event.images[:limit]:
            data = await self._api.download_image(url)
            if not data:
                self._log("warning", f"图片下载失败，跳过：{url[:60]}")
                continue
            inline.append("base64://" + base64.b64encode(data).decode("ascii"))
            if len(inline) >= limit:
                break
        return inline

    def _custom_templates(self, sub: Subscription) -> dict[str, str]:
        """合并文案模板：面板里的全局模板打底，订阅级 	pl 覆盖。"""
        merged: dict[str, str] = {}
        for kind in TEMPLATE_FIELDS:
            value = str(getattr(self._config, f"template_{kind}", "") or "").strip()
            if value:
                merged[kind] = value
        merged.update({k: v for k, v in sub.template_map.items() if v})
        return merged

    def _media_enabled(self) -> bool:
        """能不能发图片：熔断标记 + `media_mode` + 通道。"""
        if self._media_blocked:
            return False
        return media_allowed(self._config, self._sender)

    def _block_media(self, reason: str) -> None:
        if not self._media_blocked:
            self._media_blocked = True
            # 同步给发送器：帮助卡片等其它发图入口也一并停掉
            if hasattr(self._sender, "media_blocked"):
                self._sender.media_blocked = True
            self._log("warning", f"图片通道不可用（{reason}），后续只发文字提醒")

    def _link_style(self) -> str:
        """``html`` / ``markdown`` / ``url`` 三种写法。

        - 官方通道默认 ``html``：``<a href="…" target="_blank">点击进入直播间</a>``，
          QQ 官方 markdown 认这个标签，点文字直接跳转；
        - OneBot 默认 ``url``：裸网址，客户端会自动识别成可点链接。
        想固定写法可把 `link_style` 设成具体值。
        """
        style = str(cfg(self._config, "link_style") or "auto").lower()
        if style == "auto":
            return "html" if getattr(self._sender, "channel", "") == OFFICIAL else "url"
        return style

    async def _send_link(self, kind: str, target_id: str, event: MonitorEvent,
                         **kwargs: Any) -> bool:
        """补一条链接消息（文字链接，网址藏在链接里）。"""
        chain = link_chain(event, link_style=self._link_style())
        if len(chain) == 0:
            return True
        return await self._sender.send_chain(kind, target_id, chain, **kwargs)

    async def _gate(self) -> None:
        """全局推送节流（旧版「防屏蔽延迟」）。"""
        gap = float(cfg(self._config, "push_gap_seconds") or 0)
        if gap <= 0:
            return
        async with self._gate_lock:
            wait = gap - (time.monotonic() - self._last_push)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_push = time.monotonic()

    # ---------------------------------------------------------------- 对外
    async def bootstrap(self, sub: Subscription, *, profile: Any = None) -> dict[str, Any]:
        """新订阅建立基线：记录当前最新动态/直播状态/粉丝数，不推送历史内容。

        ``profile`` 是添加订阅时已经取到的资料：带上它就能少两次请求
        （粉丝数、直播间号不必重复查），被风控的概率也低一些。
        一旦触发风控冷却，剩下步骤直接跳过，等冷却结束由下一轮轮询补上。
        """
        result: dict[str, Any] = {"dynamic": 0, "live": "未知", "follower": 0}
        follower_hint = int(getattr(profile, "follower", 0) or 0) if profile is not None else 0
        if follower_hint and sub.id:
            await self._store.set_cursor(sub.id, "follower", str(follower_hint))
            result["follower"] = follower_hint
        try:
            items = await self._api.fetch_dynamic_feed(sub.uid)
            ids = [dynamic_id(item) for item in items if not is_pinned(item)]
            if ids:
                await self._store.set_cursor(sub.id, "dynamic", str(max(ids)))
                result["dynamic"] = len(ids)
        except BiliCooldownError:
            return result
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 建立动态基线失败：{exc}")
        try:
            room_id = sub.room_id or await self._resolve_room_id(sub)
            if room_id:
                snapshot = await self._api.fetch_live_room(room_id)
                await self._store.set_cursor(
                    sub.id, "live", str(snapshot.room_id),
                    {
                        "status": snapshot.live_status,
                        "peak": snapshot.online if snapshot.is_living else 0,
                        "start": snapshot.live_time if snapshot.is_living else 0,
                        "title": snapshot.title,
                        "check": int(time.time()),
                    },
                )
                result["live"] = "直播中" if snapshot.is_living else "未开播"
        except BiliCooldownError:
            return result
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 建立直播基线失败：{exc}")
        if follower_hint:
            return result
        try:
            stat = await self._api.fetch_relation_stat(sub.uid)
            follower = int(stat.get("follower") or 0)
            if follower:
                await self._store.set_cursor(sub.id, "follower", str(follower))
                result["follower"] = follower
        except BiliCooldownError:
            return result
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 建立粉丝基线失败：{exc}")
        return result

    async def poll_now(self, sub: Subscription) -> None:
        """立即检查一次（/b站管理 check 用），走与定时轮询相同的逻辑。"""
        if self._skip_for_cooldown():
            return
        if sub.category_set & set(DYNAMIC_KINDS):
            await self._guard(sub, self._poll_dynamic)
        if sub.category_set & {"live_on", "live_off"}:
            await self._guard(sub, self._poll_live)
        if "follower" in sub.category_set:
            await self._guard(sub, self._poll_follower)

    async def send_test(self, sub: Subscription, *, kind: str, target_id: str) -> bool:
        """发送一条测试提醒，用于验证推送链路。"""
        event = MonitorEvent(
            kind="test",
            uid=sub.uid,
            uname=sub.uname or str(sub.uid),
            face=sub.face,
            event_key=f"test:{time.time()}",
            url=f"https://space.bilibili.com/{sub.uid}",
            extra={"room_id": sub.room_id or ""},
        )
        # 测试提醒是命令触发的，还在被动回复窗口里，按被动消息发即可
        return await self._send(sub, event, kind, target_id, self._custom_templates(sub), proactive=False)

    async def preview_text(self, sub: Subscription, kind: str) -> str:
        """给配置命令回显一版示例文案。"""
        event = MonitorEvent(
            kind=kind,
            uid=sub.uid,
            uname=sub.uname or str(sub.uid),
            face=sub.face,
            event_key="preview",
            url=f"https://space.bilibili.com/{sub.uid}",
            title="示例标题",
            text="这里是动态正文示例。",
            extra={"duration": "12:34", "duration_text": "1 小时 2 分", "area": "虚拟主播",
                   "follower": 12345, "milestone": 10000, "peak_online": 45678,
                   "peak": 45678, "online": 1234, "play": 98765, "room_id": sub.room_id or 0},
        )
        return render_text(event, custom=self._custom_templates(sub), include_link=bool(self._config.include_link))

    def snapshot(self) -> dict[str, Any]:
        return dict(self.stats)


def summarize_cursor(cursor: Any) -> str:  # pragma: no cover - 仅排障用
    return json.dumps(cursor.extra_map, ensure_ascii=False) if cursor is not None else ""


async def wait_forever() -> None:  # pragma: no cover
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
