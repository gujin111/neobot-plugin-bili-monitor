"""轮询引擎：基线、去重、类别过滤、直播状态机、粉丝里程碑与失败重试。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

LOGGER = logging.getLogger("bili-test")


# ---------------------------------------------------------------- 测试替身
class FakeCursor:
    def __init__(self, value="", extra=None):
        self.value = value
        self.extra = extra or {}

    @property
    def extra_map(self):
        return dict(self.extra)


class FakeStore:
    def __init__(self):
        self.cursors: dict[tuple[int, str], FakeCursor] = {}
        self.claimed: set[str] = set()
        self.updated: list[dict] = []
        self.subs: list = []

    async def get_cursor(self, sub_id, kind):
        return self.cursors.get((sub_id, kind))

    async def set_cursor(self, sub_id, kind, value, extra=None):
        self.cursors[(sub_id, kind)] = FakeCursor(str(value), extra or {})

    async def claim_event(self, sub_id, kind, event_key, summary=""):
        if event_key in self.claimed:
            return False
        self.claimed.add(event_key)
        return True

    async def update_subscription(self, uid, **fields):
        self.updated.append({"uid": uid, **fields})
        for sub in self.subs:
            if sub.uid == uid:
                for key, value in fields.items():
                    setattr(sub, key, value)
        return True

    async def list_subscriptions(self, only_enabled=False):
        return [sub for sub in self.subs if sub.enabled or not only_enabled]


class FakeSender:
    def __init__(self, *, fail=False):
        self.sent: list[tuple[str, str, object]] = []
        self.images: list[tuple[str, str, bytes]] = []
        self.fail = fail

    async def send_chain(self, kind, target_id, chain):
        if self.fail:
            raise RuntimeError("发送失败")
        self.sent.append((kind, target_id, chain))
        return True

    async def send_image(self, kind, target_id, data, filename):
        if self.fail:
            raise RuntimeError("发送失败")
        self.images.append((kind, target_id, data))
        return True


class FakeCards:
    """假的卡片渲染器：总是渲染成功。"""

    available = True

    async def render(self, event, custom):
        return b"\x89PNG-fake"


class FakeApi:
    def __init__(self):
        self.dynamics: dict[int, list] = {}
        self.rooms: dict[int, object] = {}
        self.followers: dict[int, int] = {}
        self.feed_calls = 0
        self.stats = {"requests": 0, "errors": 0, "risk": 0}
        #: 模拟风控冷却（秒），>0 时引擎应整轮跳过
        self.cooldown_remaining = 0.0
        #: 模拟接口抛 BiliCooldownError
        self.raise_cooldown = False

    async def fetch_dynamic_feed(self, mid):
        from bili_monitor.bili_api import BiliCooldownError

        self.feed_calls += 1
        if self.raise_cooldown:
            raise BiliCooldownError("风控冷却中，还需等待 90 秒", retry_after=90)
        return list(self.dynamics.get(mid, []))

    async def fetch_live_room(self, room_id):
        return self.rooms[room_id]

    async def fetch_live_master(self, uid):
        return {"room_id": 555, "follower_num": 100}

    async def fetch_relation_stat(self, mid):
        self.relation_calls = getattr(self, "relation_calls", 0) + 1
        return {"follower": self.followers.get(mid, 0)}

    async def download_image(self, url, *, max_bytes=None):
        self.downloaded = getattr(self, "downloaded", [])
        self.downloaded.append(url)
        if getattr(self, "fail_download", False):
            return None
        return b"\xff\xd8\xff\xe0jpeg-bytes"


def make_config(**overrides):
    base = dict(
        dynamic_interval_seconds=120,
        live_interval_seconds=90,
        follower_interval_seconds=1800,
        max_text_chars=180,
        max_events_per_cycle=6,
        at_all_on_live_only=True,
        send_mode="auto",
        include_link=True,
        media_mode="auto",
        link_style="url",
        max_images=2,
        push_gap_seconds=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_sub(store_mod, *, uid=546195, room_id=555, categories=None, sub_id=1, at_all=False, at_list=""):
    from bili_monitor.bili_store import Subscription, Target

    sub = Subscription(
        uid=uid,
        uname="老番茄",
        face="https://i0.hdslb.com/face.jpg",
        room_id=room_id,
        enabled=True,
        at_all=at_all,
        at_list=at_list,
    )
    sub.id = sub_id
    sub.set_categories(categories if categories is not None else store_mod.DEFAULT_CATEGORIES)
    sub.targets = [Target(sub_id=sub_id, kind="group", target_id="123456")]
    return sub


def make_item(major_type="MAJOR_TYPE_OPUS", ident="200", text="新动态", item_type="DYNAMIC_TYPE_DRAW", tag=None):
    major = {"type": major_type}
    if major_type == "MAJOR_TYPE_OPUS":
        major["opus"] = {"summary": {"text": text}, "pics": [{"url": "http://i0.hdslb.com/p.jpg"}]}
    return {
        "id_str": ident,
        "type": item_type,
        "modules": {
            "module_tag": {"text": tag} if tag else None,
            "module_author": {"mid": 546195, "name": "老番茄", "face": "http://x/face.jpg", "pub_ts": 1700000000},
            "module_dynamic": {"desc": {"text": text}, "major": major},
        },
    }


def build_engine(engine_mod, store, api, sender, config, cards=None):
    return engine_mod.MonitorEngine(
        api=api, store=store, sender=sender, cards=cards, config=config, logger=LOGGER,
    )


# ---------------------------------------------------------------- 动态
async def test_baseline_only_records_cursor(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.dynamics[546195] = [make_item(ident="900"), make_item(ident="800")]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)

    await engine._poll_dynamic(sub)

    assert sender.sent == []
    assert store.cursors[(1, "dynamic")].value == "900"


async def test_new_dynamics_are_pushed_in_order(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident="300"), make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)

    await engine._poll_dynamic(sub)

    assert len(sender.sent) == 2
    assert store.cursors[(1, "dynamic")].value == "300"
    assert engine.stats["events"] == 2


async def test_pinned_and_old_items_are_skipped(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [
        make_item(ident="999", tag="置顶"),
        make_item(ident="100"),
        make_item(ident="150"),
    ]
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_dynamic(make_sub(store_mod))

    assert len(sender.sent) == 1
    # 置顶（999）不算新动态，游标停在真实的最新一条
    assert store.cursors[(1, "dynamic")].value == "150"


async def test_category_filter_skips_disabled_kind(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, categories=["live_on"])

    await engine._poll_dynamic(sub)

    assert sender.sent == []
    assert store.cursors[(1, "dynamic")].value == "200"


async def test_max_events_per_cycle_keeps_newest(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident=str(i)) for i in (110, 120, 130, 140, 150)]
    engine = build_engine(engine_mod, store, api, sender, make_config(max_events_per_cycle=2))

    await engine._poll_dynamic(make_sub(store_mod))

    assert len(sender.sent) == 2
    assert engine.stats["skipped"] == 3


async def test_duplicate_event_is_not_resent(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    store.claimed.add("546195:dyn:200")
    api.dynamics[546195] = [make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_dynamic(make_sub(store_mod))

    assert sender.sent == []
    assert store.cursors[(1, "dynamic")].value == "200"


async def test_push_failure_keeps_cursor(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender(fail=True)
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_dynamic(make_sub(store_mod))

    # 推送失败不推进游标，下一轮会重试（事件去重兜底，不会重复发送）
    assert store.cursors[(1, "dynamic")].value == "100"


async def test_no_targets_advances_without_sending(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.targets = []

    await engine._poll_dynamic(sub)

    assert sender.sent == []
    assert store.cursors[(1, "dynamic")].value == "200"


# ---------------------------------------------------------------- 直播
async def test_live_on_then_off(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "live")] = FakeCursor("555", {"status": 0, "peak": 0, "start": 0, "title": ""})
    api.rooms[555] = SimpleNamespace(
        room_id=555, live_status=1, title="今晚打游戏", cover="http://i0.hdslb.com/c.jpg",
        keyframe="", online=1200, live_time=1700000000, area_name="单机", parent_area_name="游戏",
        is_living=True,
    )
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, categories=["live_on", "live_off"])

    await engine._poll_live(sub)
    assert len(sender.sent) == 1
    assert store.cursors[(1, "live")].extra_map["status"] == 1

    api.rooms[555] = SimpleNamespace(
        room_id=555, live_status=0, title="今晚打游戏", cover="", keyframe="http://i0.hdslb.com/k.jpg",
        online=0, live_time=0, area_name="单机", parent_area_name="游戏", is_living=False,
    )
    store.cursors[(1, "live")] = FakeCursor("555", {"status": 1, "peak": 3000, "start": 1700000000, "title": "今晚打游戏"})

    await engine._poll_live(sub)
    assert len(sender.sent) == 2
    assert store.cursors[(1, "live")].extra_map["status"] == 0


async def test_live_peak_is_tracked_while_live(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "live")] = FakeCursor("555", {"status": 1, "peak": 1000, "start": 1700000000, "title": ""})
    api.rooms[555] = SimpleNamespace(
        room_id=555, live_status=1, title="直播中", cover="", keyframe="", online=5000,
        live_time=1700000000, area_name="", parent_area_name="", is_living=True,
    )
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, categories=["live_on"])

    await engine._poll_live(sub)
    assert sender.sent == []
    assert store.cursors[(1, "live")].extra_map["peak"] == 5000


async def test_room_id_is_resolved_when_missing(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.rooms[555] = SimpleNamespace(
        room_id=555, live_status=0, title="", cover="", keyframe="", online=0,
        live_time=0, area_name="", parent_area_name="", is_living=False,
    )
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, room_id=None)

    await engine._poll_live(sub)

    assert store.updated and store.updated[0]["room_id"] == 555
    assert store.cursors[(1, "live")].value == "555"


# ---------------------------------------------------------------- 粉丝
async def test_follower_milestone_pushes(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "follower")] = FakeCursor("900")
    api.followers[546195] = 1200
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_follower(make_sub(store_mod))

    assert len(sender.sent) == 1
    assert store.cursors[(1, "follower")].value == "1200"


async def test_follower_without_milestone_only_updates(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "follower")] = FakeCursor("1200")
    api.followers[546195] = 1500
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_follower(make_sub(store_mod))

    assert sender.sent == []
    assert store.cursors[(1, "follower")].value == "1500"


async def test_follower_baseline_without_cursor(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.followers[546195] = 88888
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._poll_follower(make_sub(store_mod))

    assert sender.sent == []
    assert store.cursors[(1, "follower")].value == "88888"


# ---------------------------------------------------------------- 发送模式
async def test_text_mode_has_no_image_segments(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(include_link=False, send_mode="text"))
    sub = make_sub(store_mod)
    sub.send_mode = "text"
    event = _event_from(engine_mod, api)
    await engine._dispatch(sub, event)

    assert len(sender.sent) == 1
    chain = sender.sent[0][2]
    assert [seg for seg in chain.segments if seg.type == "image"] == []


async def test_image_mode_inlines_image_into_one_message(engine_mod, store_mod):
    """图片以 base64 内联进同一条消息链，不再单独发一条。"""
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "image"
    event = _event_from(engine_mod, api)

    await engine._dispatch(sub, event)

    assert len(sender.sent) == 1, "只发一条消息"
    assert sender.images == [], "不再单独发图片消息"
    chain = sender.sent[0][2]
    images = [seg for seg in chain.segments if seg.type == "image"]
    assert len(images) == 1
    assert images[0].data["file"].startswith("base64://")
    assert chain.plain_text.strip(), "同一条消息里还带着文案"


async def test_image_download_failure_keeps_text(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.fail_download = True
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "image"

    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert len(sender.sent) == 1, "文本照发"
    assert [s for s in sender.sent[0][2].segments if s.type == "image"] == []


async def test_image_send_failure_falls_back_to_text(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "image"

    calls = {"n": 0}
    original = sender.send_chain

    async def flaky(kind, target_id, chain, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("官方富媒体上传失败")
        return await original(kind, target_id, chain, **kwargs)

    sender.send_chain = flaky  # type: ignore[assignment]

    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert calls["n"] == 2, "带图失败后重发纯文本"
    assert [s for s in sender.sent[-1][2].segments if s.type == "image"] == []
    assert engine._media_blocked is True


async def test_media_mode_never_disables_images(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(media_mode="never"))
    sub = make_sub(store_mod)
    sub.send_mode = "image"

    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert len(sender.sent) == 1
    assert [s for s in sender.sent[0][2].segments if s.type == "image"] == []


async def test_media_mode_always_forces_images_on_official(engine_mod, store_mod):
    """文件服务公网可达的用户可以强制发图。"""
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    sender.channel = "official"
    engine = build_engine(engine_mod, store, api, sender, make_config(media_mode="always"))
    sub = make_sub(store_mod)
    sub.send_mode = "image"

    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert [s for s in sender.sent[0][2].segments if s.type == "image"]


async def test_panel_template_is_used_by_default(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender,
                          make_config(template_live_on="【自定义】{name} 开播：{title}"))
    sub = make_sub(store_mod)
    sub.send_mode = "text"

    await engine._dispatch(sub, _live_event(engine_mod))

    text = sender.sent[0][2].plain_text
    assert text.startswith("【自定义】老番茄 开播：")
    assert "【开播提醒】" not in text


async def test_subscription_template_overrides_panel(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender,
                          make_config(template_live_on="【面板】{name}"))
    sub = make_sub(store_mod)
    sub.send_mode = "text"
    sub.set_template("live_on", "【订阅】{name}")

    await engine._dispatch(sub, _live_event(engine_mod))

    assert sender.sent[0][2].plain_text.startswith("【订阅】老番茄")


async def test_official_channel_never_sends_images(engine_mod, store_mod):
    """官方通道不支持本地文件上传：image 模式也只会发文字。"""
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    sender.channel = "official"
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "image"

    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert len(sender.sent) == 1
    assert sender.images == []
    assert getattr(api, "downloaded", []) == [], "连下载都不该发起"


async def test_official_channel_skips_card_mode(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    sender.channel = "official"
    engine = build_engine(engine_mod, store, api, sender, make_config(), cards=FakeCards())
    sub = make_sub(store_mod)
    sub.send_mode = "card"
    rendered = []

    async def fake_render(event, custom):
        rendered.append(1)
        return b"png"

    engine._cards.render = fake_render  # type: ignore[union-attr]

    await engine._dispatch(sub, _live_event(engine_mod))

    assert rendered == [], "官方通道连渲染都不用做"
    assert len(sender.sent) == 1
    assert sender.images == []


class FakeTuwenSender(FakeSender):
    """支持官方图文卡片的发送器。"""

    def __init__(self, *, tuwen_ok=True):
        super().__init__()
        self.tuwen_ok = tuwen_ok
        self.tuwens: list[dict] = []

    async def send_tuwen(self, kind, target_id, content, **kwargs):
        if not self.tuwen_ok:
            return False
        self.tuwens.append(content)
        return True


async def test_official_channel_sends_tuwen_card(engine_mod, store_mod):
    """官方通道优先发图文卡片：文字 + 封面 + 整卡可点，一条消息。"""
    store, api, sender = FakeStore(), FakeApi(), FakeTuwenSender()
    sender.channel = "official"
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "auto"

    await engine._dispatch(sub, _live_event(engine_mod))

    assert len(sender.tuwens) == 1
    card = sender.tuwens[0]
    assert card["title"].endswith("开播了")
    assert card["url"] == "https://live.bilibili.com/555"
    assert card["pic_url"].startswith("https://")
    assert sender.sent == [], "图文卡片已经是完整一条消息，不再补文本"


async def test_tuwen_failure_falls_back_to_text(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeTuwenSender(tuwen_ok=False)
    sender.channel = "official"
    engine = build_engine(engine_mod, store, api, sender, make_config(link_style="auto"))
    sub = make_sub(store_mod)
    sub.send_mode = "auto"

    await engine._dispatch(sub, _live_event(engine_mod))

    assert sender.tuwens == []
    assert len(sender.sent) == 1, "卡片失败要退回文本"
    assert "<a href" in sender.sent[0][2].plain_text


async def test_media_failure_blocks_following_images(engine_mod, store_mod):
    """带图消息发送失败一次后熔断，后续只发文字。"""
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    sub.send_mode = "image"
    calls = {"n": 0}
    original = sender.send_chain

    async def flaky(kind, target_id, chain, **kwargs):
        calls["n"] += 1
        if any(seg.type == "image" for seg in chain.segments):
            raise RuntimeError("MediaUploadError: 上传官方富媒体失败")
        return await original(kind, target_id, chain, **kwargs)

    sender.send_chain = flaky  # type: ignore[assignment]

    await engine._dispatch(sub, _event_from(engine_mod, api))
    assert engine._media_blocked is True, "失败一次后熔断，不再尝试发图"

    downloaded_before = len(getattr(api, "downloaded", []))
    store.claimed.clear()
    await engine._dispatch(sub, _event_from(engine_mod, api))

    assert calls["n"] == 3, "第一条：带图失败 + 纯文本重发；第二条：只发文本"
    assert len(getattr(api, "downloaded", [])) == downloaded_before, "熔断后不再下载图片"


async def test_due_subscriptions_tight_jitter_in_fast_mode(engine_mod, store_mod, monkeypatch):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.subs = [make_sub(store_mod)]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    captured = {}

    def fake_uniform(low, high):
        captured["args"] = (low, high)
        return 1.0

    monkeypatch.setattr(engine_mod.random, "uniform", fake_uniform)

    await engine._due_subscriptions("live", 3)
    assert captured["args"] == (1.0, 1.02), "3 秒档不能被抖动拉长"

    engine._due.clear()
    await engine._due_subscriptions("live", 90)
    assert captured["args"] == (0.9, 1.15)


def _event_from(engine_mod, api):
    from bili_monitor.bili_model import parse_dynamic_item

    return parse_dynamic_item(make_item(ident="777"))


async def test_at_all_only_for_live_when_configured(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(at_all_on_live_only=True))
    sub = make_sub(store_mod, at_all=True)

    await engine._dispatch(sub, _event_from(engine_mod, api))
    chain = sender.sent[-1][2]
    assert not [seg for seg in chain.segments if seg.type == "at"]

    store.claimed.clear()
    live_event = _live_event(engine_mod)
    await engine._dispatch(sub, live_event)
    chain = sender.sent[-1][2]
    assert any(seg.type == "at" and seg.data.get("qq") == "all" for seg in chain.segments)


async def test_at_list_is_applied(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, at_list="111,222")

    await engine._dispatch(sub, _event_from(engine_mod, api))

    chain = sender.sent[-1][2]
    ats = [seg.data.get("qq") for seg in chain.segments if seg.type == "at"]
    assert ats == ["111", "222"]


def _live_event(engine_mod):
    from bili_monitor.bili_model import build_live_on

    return build_live_on(
        uid=546195, uname="老番茄", face="", room_id=555, title="直播",
        cover="https://i0.hdslb.com/cover.jpg", area="游戏", online=1, start_ts=1700000000,
    )


# ---------------------------------------------------------------- 对外方法
async def test_poll_now_respects_categories(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    api.dynamics[546195] = [make_item(ident="200")]
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod, categories=["live_on"])

    await engine.poll_now(sub)

    assert sender.sent == []


async def test_send_test_uses_sender(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())

    assert await engine.send_test(make_sub(store_mod), kind="group", target_id="1") is True
    assert len(sender.sent) == 1
    assert "测试提醒" in sender.sent[0][2].plain_text


async def test_push_gap_is_respected(engine_mod, store_mod):
    import time

    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(push_gap_seconds=0.25))
    sub = make_sub(store_mod)

    start = time.monotonic()
    await engine._dispatch(sub, _event_from(engine_mod, api))
    store.claimed.clear()
    await engine._dispatch(sub, _event_from(engine_mod, api))
    # 两次发送之间至少间隔一个 push_gap（留一点调度误差）
    assert time.monotonic() - start >= 0.2


async def test_snapshot_reports_stats(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config())
    await engine._dispatch(make_sub(store_mod), _event_from(engine_mod, api))
    snapshot = engine.snapshot()
    assert snapshot["events"] == 1
    assert snapshot["pushed"] == 1


@pytest.mark.parametrize("mode", ["auto", "text", "image", "card"])
async def test_all_send_modes_work_without_cards(engine_mod, store_mod, mode):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(send_mode=mode))
    sub = make_sub(store_mod)
    sub.send_mode = mode
    await engine._dispatch(sub, _event_from(engine_mod, api))
    assert len(sender.sent) == 1


# ---------------------------------------------------------------- 风控冷却
async def test_tick_skips_whole_cycle_during_cooldown(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.cooldown_remaining = 120.0
    api.dynamics[546195] = [make_item(ident="200")]
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    store.subs = [make_sub(store_mod)]
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._dynamic_tick()

    assert api.feed_calls == 0           # 整轮跳过，不再请求
    assert sender.sent == []
    assert engine.snapshot()["cooldown_left"] == 120


async def test_cooldown_error_is_not_counted_as_failure(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.raise_cooldown = True
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine._guard(make_sub(store_mod), engine._poll_dynamic)

    assert engine.snapshot()["failed"] == 0
    assert engine.snapshot()["cooldown_left"] == 90


async def test_bootstrap_skips_remaining_steps_on_cooldown(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.raise_cooldown = True
    engine = build_engine(engine_mod, store, api, sender, make_config())

    result = await engine.bootstrap(make_sub(store_mod))

    assert result["dynamic"] == 0
    assert (1, "dynamic") not in store.cursors
    assert api.rooms == {}                # 冷却后没有再打直播接口


async def test_bootstrap_reuses_profile_hints(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.rooms[555] = SimpleNamespace(
        room_id=555, live_status=0, title="", cover="", keyframe="", online=0,
        live_time=0, area_name="", parent_area_name="", is_living=False,
    )
    engine = build_engine(engine_mod, store, api, sender, make_config())
    sub = make_sub(store_mod)
    profile = SimpleNamespace(follower=8888, room_id=555, name="老番茄", face="")

    result = await engine.bootstrap(sub, profile=profile)

    assert result["follower"] == 8888
    assert store.cursors[(1, "follower")].value == "8888"
    assert getattr(api, "relation_calls", 0) == 0     # 资料里已有粉丝数，不再查关系接口


async def test_poll_now_respects_cooldown(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    api.cooldown_remaining = 30.0
    store.cursors[(1, "dynamic")] = FakeCursor("100")
    engine = build_engine(engine_mod, store, api, sender, make_config())

    await engine.poll_now(make_sub(store_mod))

    assert api.feed_calls == 0


# ---------------------------------------------------------------- 卡片 + 链接
async def test_card_mode_sends_clickable_link_text(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(engine_mod, store, api, sender, make_config(), cards=FakeCards())
    sub = make_sub(store_mod)
    sub.send_mode = "card"

    await engine._send(sub, _live_event(engine_mod), "group", "1", {})

    assert len(sender.images) == 1, "先发卡片图"
    assert len(sender.sent) == 1, "再补一条可点链接文本"
    text = sender.sent[0][2].plain_text
    assert "▶ 点击进入直播间" in text
    assert "https://live.bilibili.com/555" in text


async def test_card_mode_skips_link_text_when_disabled(engine_mod, store_mod):
    store, api, sender = FakeStore(), FakeApi(), FakeSender()
    engine = build_engine(
        engine_mod, store, api, sender, make_config(include_link=False), cards=FakeCards()
    )
    sub = make_sub(store_mod)
    sub.send_mode = "card"

    await engine._send(sub, _live_event(engine_mod), "group", "1", {})

    assert len(sender.images) == 1
    assert sender.sent == []


# ---------------------------------------------------------------- 官方通道按钮
