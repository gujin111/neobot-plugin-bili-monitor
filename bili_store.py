"""订阅、推送目标、游标与去重事件表的持久化层（SQLAlchemy Async）。

表由迁移 v1 用 ``Base.metadata.create_all`` 建出，宿主负责引擎、WAL 与外键约束。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: 事件类别：动态相关、直播相关、粉丝里程碑
CATEGORY_LABELS: dict[str, str] = {
    "dynamic_new": "发布新动态",
    "dynamic_repost": "转发动态",
    "dynamic_video": "投稿新视频",
    "dynamic_article": "发布新专栏",
    "dynamic_audio": "发布新音频",
    "live_on": "开播提醒",
    "live_off": "下播提醒",
    "follower": "粉丝数里程碑",
}

DEFAULT_CATEGORIES: tuple[str, ...] = (
    "dynamic_new",
    "dynamic_repost",
    "dynamic_video",
    "dynamic_article",
    "live_on",
    "live_off",
    "follower",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Subscription(Base):
    __tablename__ = "bili_subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uid: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    uname: Mapped[str] = mapped_column(String(128), default="")
    face: Mapped[str] = mapped_column(String(512), default="")
    room_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    categories: Mapped[str] = mapped_column(Text, default=lambda: json.dumps(list(DEFAULT_CATEGORIES)))
    send_mode: Mapped[str] = mapped_column(String(16), default="")
    at_all: Mapped[bool] = mapped_column(Boolean, default=False)
    at_list: Mapped[str] = mapped_column(String(512), default="")
    templates: Mapped[str] = mapped_column(Text, default="{}")
    follower_step: Mapped[int] = mapped_column(Integer, default=1000)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    targets: Mapped[list["Target"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan", lazy="selectin"
    )

    # ---------------------------------------------------------------- 便捷读取
    @property
    def category_set(self) -> set[str]:
        try:
            values = json.loads(self.categories or "[]")
        except ValueError:
            values = []
        return {str(value) for value in values if value}

    def set_categories(self, values: Iterable[str]) -> None:
        self.categories = json.dumps(sorted({str(value) for value in values}))

    @property
    def template_map(self) -> dict[str, str]:
        try:
            values = json.loads(self.templates or "{}")
        except ValueError:
            return {}
        return {str(key): str(value) for key, value in values.items()} if isinstance(values, dict) else {}

    def set_template(self, kind: str, template: str) -> None:
        values = self.template_map
        if template:
            values[kind] = template
        else:
            values.pop(kind, None)
        self.templates = json.dumps(values, ensure_ascii=False)

    @property
    def at_ids(self) -> list[str]:
        return [item.strip() for item in (self.at_list or "").split(",") if item.strip()]

    @property
    def display_name(self) -> str:
        return self.uname or str(self.uid)


class Target(Base):
    __tablename__ = "bili_target"
    __table_args__ = (UniqueConstraint("sub_id", "kind", "target_id", name="uq_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sub_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("bili_subscription.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), default="group")
    target_id: Mapped[str] = mapped_column(String(32), default="")

    subscription: Mapped[Subscription] = relationship(back_populates="targets")


class Cursor(Base):
    __tablename__ = "bili_cursor"

    sub_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    value: Mapped[str] = mapped_column(String(64), default="")
    extra: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    @property
    def extra_map(self) -> dict[str, Any]:
        try:
            data = json.loads(self.extra or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class EventLog(Base):
    __tablename__ = "bili_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sub_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="")
    event_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    summary: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


async def create_schema(connection: Any) -> None:
    """迁移 v1：建表。"""
    await connection.run_sync(Base.metadata.create_all)


class BiliStore:
    """对 PluginDatabase 的薄封装，所有方法都是协程。"""

    def __init__(self, database: Any) -> None:
        self._db = database

    @property
    def database(self) -> Any:
        return self._db

    # ---------------------------------------------------------------- 订阅
    async def list_subscriptions(self, *, only_enabled: bool = False) -> list[Subscription]:
        async with self._db.session() as session:
            statement = select(Subscription).order_by(Subscription.id)
            if only_enabled:
                statement = statement.where(Subscription.enabled.is_(True))
            rows = list(await session.scalars(statement))
            for row in rows:
                _ = row.targets  # 触发 selectin 预载，避免会话关闭后懒加载
            return rows

    async def count_subscriptions(self) -> int:
        async with self._db.session() as session:
            return int(await session.scalar(select(func.count()).select_from(Subscription)) or 0)

    async def get_subscription(self, uid: int) -> Subscription | None:
        async with self._db.session() as session:
            row = await session.scalar(select(Subscription).where(Subscription.uid == uid))
            if row is not None:
                _ = row.targets
            return row

    async def create_subscription(self, *, uid: int, uname: str, face: str, room_id: int | None,
                                  created_by: str, at_all: bool) -> Subscription:
        async with self._db.transaction() as session:
            session.add(
                Subscription(
                    uid=uid, uname=uname, face=face, room_id=room_id,
                    categories=json.dumps(list(DEFAULT_CATEGORIES)),
                    at_all=at_all, created_by=created_by,
                )
            )
        # 宿主的事务会话 expire_on_commit=True，提交后实体属性会失效；
        # 用只读会话重新取一份已加载的对象返回，调用方才能安全读 uid / id / targets。
        created = await self.get_subscription(uid)
        if created is None:  # pragma: no cover - 刚写入就查不到属于异常
            raise RuntimeError(f"订阅写入后读取失败: uid={uid}")
        return created

    async def delete_subscription(self, uid: int) -> bool:
        async with self._db.transaction() as session:
            row = await session.scalar(select(Subscription).where(Subscription.uid == uid))
            if row is None:
                return False
            sub_id = row.id
            # 推送目标由 relationship 的 delete-orphan 级联删除；游标与事件没有关系映射
            await session.execute(delete(Cursor).where(Cursor.sub_id == sub_id))
            await session.execute(delete(EventLog).where(EventLog.sub_id == sub_id))
            await session.delete(row)
            return True

    async def update_subscription(self, uid: int, **fields: Any) -> bool:
        allowed = {
            "uname", "face", "room_id", "enabled", "send_mode", "at_all",
            "at_list", "categories", "templates", "follower_step",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return False
        async with self._db.transaction() as session:
            row = await session.scalar(select(Subscription).where(Subscription.uid == uid))
            if row is None:
                return False
            for key, value in updates.items():
                setattr(row, key, value)
            return True

    # ---------------------------------------------------------------- 推送目标
    async def add_target(self, sub_id: int, kind: str, target_id: str) -> bool:
        async with self._db.transaction() as session:
            exists = await session.scalar(
                select(Target).where(
                    Target.sub_id == sub_id, Target.kind == kind, Target.target_id == target_id
                )
            )
            if exists is not None:
                return False
            session.add(Target(sub_id=sub_id, kind=kind, target_id=str(target_id)))
            return True

    async def remove_target(self, sub_id: int, kind: str, target_id: str) -> bool:
        async with self._db.transaction() as session:
            row = await session.scalar(
                select(Target).where(
                    Target.sub_id == sub_id, Target.kind == kind, Target.target_id == target_id
                )
            )
            if row is None:
                return False
            await session.delete(row)
            return True

    async def set_targets(self, sub_id: int, targets: Sequence[tuple[str, str]]) -> None:
        """整体替换推送目标（订阅初始化时用）。"""
        async with self._db.transaction() as session:
            await session.execute(delete(Target).where(Target.sub_id == sub_id))
            for kind, target_id in targets:
                session.add(Target(sub_id=sub_id, kind=kind, target_id=str(target_id)))

    async def normalize_targets(self) -> int:
        """把历史遗留的目标 ID 归一化（``group:123`` → ``123``），并合并重复项。

        旧版本可能把 ``group:123`` 之类带前缀的会话号存进库，导致推送时找不到群；
        插件加载时跑一次即可修正。返回被修改/删除的条数。
        """
        from .bili_model import normalize_target_id

        changed = 0
        async with self._db.transaction() as session:
            rows = list(await session.scalars(select(Target).order_by(Target.id)))
            seen: set[tuple[int, str, str]] = set()
            updates: list[tuple[Target, str]] = []
            for row in rows:
                ident = normalize_target_id(row.target_id)
                key = (row.sub_id, row.kind, ident)
                if key in seen:
                    await session.delete(row)
                    changed += 1
                    continue
                seen.add(key)
                if ident != row.target_id:
                    updates.append((row, ident))
            # 先落 DELETE，再把旧 ID 改掉，避免 UPDATE 撞上还没删掉的重复行唯一约束
            await session.flush()
            for row, ident in updates:
                row.target_id = ident
                changed += 1
        return changed

    # ---------------------------------------------------------------- 游标
    async def get_cursor(self, sub_id: int, kind: str) -> Cursor | None:
        async with self._db.session() as session:
            return await session.scalar(
                select(Cursor).where(Cursor.sub_id == sub_id, Cursor.kind == kind)
            )

    async def set_cursor(self, sub_id: int, kind: str, value: str, extra: dict[str, Any] | None = None) -> None:
        payload = json.dumps(extra or {}, ensure_ascii=False)
        async with self._db.transaction() as session:
            row = await session.scalar(
                select(Cursor).where(Cursor.sub_id == sub_id, Cursor.kind == kind)
            )
            if row is None:
                session.add(Cursor(sub_id=sub_id, kind=kind, value=str(value), extra=payload))
            else:
                row.value = str(value)
                row.extra = payload
                row.updated_at = _now()

    # ---------------------------------------------------------------- 去重
    async def claim_event(self, sub_id: int, kind: str, event_key: str, summary: str = "") -> bool:
        """抢占事件：返回 True 表示第一次见到（应当推送）。"""
        async with self._db.transaction() as session:
            exists = await session.scalar(select(EventLog.id).where(EventLog.event_key == event_key))
            if exists is not None:
                return False
            session.add(EventLog(sub_id=sub_id, kind=kind, event_key=event_key, summary=summary[:256]))
            return True

    async def prune_events(self, retention_days: int) -> int:
        cutoff = _now() - timedelta(days=max(1, int(retention_days)))
        async with self._db.transaction() as session:
            result = await session.execute(delete(EventLog).where(EventLog.created_at < cutoff))
            return int(result.rowcount or 0)

    async def recent_events(self, limit: int = 20) -> list[EventLog]:
        async with self._db.session() as session:
            return list(
                await session.scalars(select(EventLog).order_by(EventLog.id.desc()).limit(limit))
            )
