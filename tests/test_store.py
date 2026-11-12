"""持久化层：订阅、目标、游标与事件去重（真实 SQLite）。"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class ShimDatabase:
    """模拟宿主 PluginDatabase 的 session()/transaction() 语义。"""

    def __init__(self, url: str, **engine_options) -> None:
        self.engine = create_async_engine(url, **engine_options)
        # 与宿主 PluginDatabase 一致：事务会话提交后让实体过期
        self._maker = async_sessionmaker(self.engine, expire_on_commit=True)

    @asynccontextmanager
    async def session(self):
        async with self._maker() as session:
            yield session

    @asynccontextmanager
    async def transaction(self):
        async with self._maker() as session:
            async with session.begin():
                yield session

    async def dispose(self) -> None:
        await self.engine.dispose()


@pytest.fixture
async def store(store_mod):
    # 内存库 + StaticPool：测试不需要文件，也不受临时目录权限影响
    shim = ShimDatabase(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with shim.engine.begin() as connection:
        await connection.run_sync(store_mod.Base.metadata.create_all)
    yield store_mod.BiliStore(shim)
    await shim.dispose()


async def test_create_and_list(store):
    sub = await store.create_subscription(
        uid=546195, uname="老番茄", face="https://x/face.jpg", room_id=21852,
        created_by="10001", at_all=False,
    )
    assert sub.id > 0
    assert sub.category_set == set(store_mod_categories())
    assert await store.count_subscriptions() == 1
    listed = await store.list_subscriptions()
    assert listed[0].uid == 546195
    assert listed[0].display_name == "老番茄"
    assert listed[0].room_id == 21852


def store_mod_categories():
    from bili_monitor.bili_store import DEFAULT_CATEGORIES

    return DEFAULT_CATEGORIES


async def test_subscription_is_unique_per_uid(store):
    await store.create_subscription(uid=1, uname="a", face="", room_id=None, created_by="", at_all=False)
    with pytest.raises(Exception):
        await store.create_subscription(uid=1, uname="b", face="", room_id=None, created_by="", at_all=False)


async def test_update_subscription_fields(store):
    sub = await store.create_subscription(uid=2, uname="a", face="", room_id=None, created_by="", at_all=False)
    assert await store.update_subscription(2, enabled=False, send_mode="card", at_all=True,
                                           at_list="1,2", categories=json.dumps(["live_on"]))
    refreshed = await store.get_subscription(2)
    assert refreshed is not None
    assert refreshed.enabled is False
    assert refreshed.send_mode == "card"
    assert refreshed.at_all is True
    assert refreshed.at_ids == ["1", "2"]
    assert refreshed.category_set == {"live_on"}
    assert sub.id == refreshed.id


async def test_update_ignores_unknown_fields(store):
    await store.create_subscription(uid=3, uname="a", face="", room_id=None, created_by="", at_all=False)
    assert await store.update_subscription(3, nonsense_field=999) is False
    assert await store.update_subscription(3, uname="b") is True


async def test_targets_add_dedupe_remove(store):
    sub = await store.create_subscription(uid=4, uname="a", face="", room_id=None, created_by="", at_all=False)
    assert await store.add_target(sub.id, "group", "123") is True
    assert await store.add_target(sub.id, "group", "123") is False
    assert await store.add_target(sub.id, "private", "456") is True
    refreshed = await store.get_subscription(4)
    assert {(t.kind, t.target_id) for t in refreshed.targets} == {("group", "123"), ("private", "456")}
    assert await store.remove_target(sub.id, "group", "123") is True
    assert await store.remove_target(sub.id, "group", "123") is False
    refreshed = await store.get_subscription(4)
    assert [t.target_id for t in refreshed.targets] == ["456"]


async def test_set_targets_replaces(store):
    sub = await store.create_subscription(uid=5, uname="a", face="", room_id=None, created_by="", at_all=False)
    await store.add_target(sub.id, "group", "1")
    await store.set_targets(sub.id, [("group", "2"), ("private", "3")])
    refreshed = await store.get_subscription(5)
    assert {t.target_id for t in refreshed.targets} == {"2", "3"}


async def test_delete_subscription_cascades(store):
    sub = await store.create_subscription(uid=6, uname="a", face="", room_id=None, created_by="", at_all=False)
    await store.add_target(sub.id, "group", "1")
    await store.set_cursor(sub.id, "dynamic", "123")
    await store.claim_event(sub.id, "dynamic_new", "6:dyn:123")
    assert await store.delete_subscription(6) is True
    assert await store.get_subscription(6) is None
    assert await store.get_cursor(sub.id, "dynamic") is None
    # 事件日志按 sub_id 清理，重复事件可以再次抢占
    assert await store.claim_event(sub.id, "dynamic_new", "6:dyn:123") is True


async def test_cursor_roundtrip(store):
    sub = await store.create_subscription(uid=7, uname="a", face="", room_id=None, created_by="", at_all=False)
    assert await store.get_cursor(sub.id, "live") is None
    await store.set_cursor(sub.id, "live", "21852", {"status": 1, "peak": 100})
    cursor = await store.get_cursor(sub.id, "live")
    assert cursor.value == "21852"
    assert cursor.extra_map == {"status": 1, "peak": 100}
    await store.set_cursor(sub.id, "live", "21852", {"status": 0, "peak": 0})
    cursor = await store.get_cursor(sub.id, "live")
    assert cursor.extra_map["status"] == 0


async def test_claim_event_is_idempotent(store):
    sub = await store.create_subscription(uid=8, uname="a", face="", room_id=None, created_by="", at_all=False)
    assert await store.claim_event(sub.id, "dynamic_new", "8:dyn:1", "摘要") is True
    assert await store.claim_event(sub.id, "dynamic_new", "8:dyn:1", "摘要") is False
    assert await store.claim_event(sub.id, "dynamic_new", "8:dyn:2") is True


async def test_prune_events(store, store_mod):
    sub = await store.create_subscription(uid=9, uname="a", face="", room_id=None, created_by="", at_all=False)
    await store.claim_event(sub.id, "dynamic_new", "old")
    async with store.database.transaction() as session:
        from sqlalchemy import update

        await session.execute(
            update(store_mod.EventLog)
            .where(store_mod.EventLog.event_key == "old")
            .values(created_at=datetime.now(timezone.utc) - timedelta(days=30))
        )
    await store.claim_event(sub.id, "dynamic_new", "new")
    removed = await store.prune_events(7)
    assert removed == 1
    keys = {row.event_key for row in await store.recent_events(10)}
    assert keys == {"new"}


async def test_template_map_helpers(store):
    sub = await store.create_subscription(uid=10, uname="a", face="", room_id=None, created_by="", at_all=False)
    sub.set_template("live_on", "{name} 开播了")
    sub.set_template("dynamic_new", "{name} 发动态")
    sub.set_template("live_on", "")
    assert sub.template_map == {"dynamic_new": "{name} 发动态"}


async def test_invalid_template_json_falls_back(store):
    sub = await store.create_subscription(uid=11, uname="a", face="", room_id=None, created_by="", at_all=False)
    sub.templates = "not-json"
    assert sub.template_map == {}
    sub.categories = "not-json"
    assert sub.category_set == set()
