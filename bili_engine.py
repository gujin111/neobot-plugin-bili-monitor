"""轮询引擎：动态 / 直播 / 粉丝三条独立循环，带限速、去重与风控退避。

设计要点（对应旧版插件的几个关键行为）：
- **首次订阅只记基线不推送**，避免把历史动态刷屏；
- **游标落库 + 事件去重**，重启或推送失败都不会重复提醒；
- **全局推送间隔** 对应旧版的「防屏蔽延迟」；
- 单个 UP 主失败不影响其它订阅，风控时整体退避。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from typing import Any, Protocol

from .bili_api import BiliApiError, BiliClient, LiveRoomSnapshot
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
from .bili_render import build_chain, render_text
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
        self.stats = {
            "cycles": 0, "events": 0, "pushed": 0, "skipped": 0,
            "failed": 0, "last_error": "", "last_push_at": 0.0,
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
            "follower": self._config.follower_interval_seconds,
        }[kind]
        # 每 15 秒唤醒一次，按各订阅自己的到期时间派发，避免同一刻并发打满
        step = max(5.0, min(15.0, interval / 4))
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
            await asyncio.sleep(max(1.0, step - elapsed))

    async def _due_subscriptions(self, kind: str, interval: float) -> list[Subscription]:
        subs = await self._store.list_subscriptions(only_enabled=True)
        now = time.monotonic()
        due: list[Subscription] = []
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
                self._due[key] = now + interval * random.uniform(0.9, 1.15)
            due.append(sub)
        return due

    # ---------------------------------------------------------------- 动态
    async def _dynamic_tick(self) -> None:
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
            event = parse_dynamic_item(item, text_limit=self._config.max_text_chars)
            if event is not None:
                events.append(event)

        limit = max(1, int(self._config.max_events_per_cycle))
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
        subs = await self._due_subscriptions("follower", self._config.follower_interval_seconds)
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
        custom = sub.template_map
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

    async def _send(self, sub: Subscription, event: MonitorEvent, kind: str, target_id: str,
                    custom: dict[str, str]) -> bool:
        at_all = bool(sub.at_all) and (
            not self._config.at_all_on_live_only or event.kind == "live_on"
        )
        mode = (sub.send_mode or self._config.send_mode or "auto").strip().lower()
        if mode == "auto":
            mode = "image" if event.images else "text"

        if mode == "card" and self._cards is not None and self._cards.available:
            data = await self._cards.render(event, custom)
            if data:
                await self._gate()
                return await self._sender.send_image(kind, target_id, data, f"bili-{event.kind}.png")

        chain = build_chain(
            event, custom=custom,
            include_link=bool(self._config.include_link),
            at_all=at_all,
            at_ids=tuple(sub.at_ids),
            with_images=mode in {"image", "card"},
            max_images=3 if mode == "image" else 1,
        )
        await self._gate()
        return await self._sender.send_chain(kind, target_id, chain)

    async def _gate(self) -> None:
        """全局推送节流（旧版「防屏蔽延迟」）。"""
        gap = float(self._config.push_gap_seconds or 0)
        if gap <= 0:
            return
        async with self._gate_lock:
            wait = gap - (time.monotonic() - self._last_push)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_push = time.monotonic()

    # ---------------------------------------------------------------- 对外
    async def bootstrap(self, sub: Subscription) -> dict[str, Any]:
        """新订阅建立基线：记录当前最新动态/直播状态/粉丝数，不推送历史内容。"""
        result: dict[str, Any] = {"dynamic": 0, "live": "未知", "follower": 0}
        try:
            items = await self._api.fetch_dynamic_feed(sub.uid)
            ids = [dynamic_id(item) for item in items if not is_pinned(item)]
            if ids:
                await self._store.set_cursor(sub.id, "dynamic", str(max(ids)))
                result["dynamic"] = len(ids)
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
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 建立直播基线失败：{exc}")
        try:
            stat = await self._api.fetch_relation_stat(sub.uid)
            follower = int(stat.get("follower") or 0)
            if follower:
                await self._store.set_cursor(sub.id, "follower", str(follower))
                result["follower"] = follower
        except BiliApiError as exc:
            self._log("warning", f"{sub.display_name} 建立粉丝基线失败：{exc}")
        return result

    async def poll_now(self, sub: Subscription) -> None:
        """立即检查一次（/b站管理 check 用），走与定时轮询相同的逻辑。"""
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
        return await self._send(sub, event, kind, target_id, sub.template_map)

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
        return render_text(event, custom=sub.template_map, include_link=bool(self._config.include_link))

    def snapshot(self) -> dict[str, Any]:
        return dict(self.stats)


def summarize_cursor(cursor: Any) -> str:  # pragma: no cover - 仅排障用
    return json.dumps(cursor.extra_map, ensure_ascii=False) if cursor is not None else ""


async def wait_forever() -> None:  # pragma: no cover
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()
