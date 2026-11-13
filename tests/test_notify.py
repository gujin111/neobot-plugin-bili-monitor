"""通道识别、媒体策略与 proactive 兼容（不联网）。"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def notify(pkg):
    return importlib.import_module(f"{pkg.__name__}.bili_notify")


class _Ctx:
    def __init__(self, adapter=None):
        self.adapter = adapter


class _PlainAdapter:
    """OneBot 适配器（类名不带 official）。"""


class _OfficialAdapter:
    """官方适配器：本体 SingleAdapter 用类名区分。"""


class _Switchable:
    """a9 的可切换门面：靠 active_channels 报告在跑的通道。"""

    def __init__(self, *channels):
        self.active_channels = tuple(channels)


class _FakeCtxForSend:
    def __init__(self, channel=None):
        self.adapter = _Switchable(*channel) if channel else _PlainAdapter()
        self.calls: list[dict] = []

    def conversation_from_event(self, data):
        return SimpleNamespace(kind=data.get("message_type"), id=str(data.get("group_id") or data.get("user_id")))

    async def send(self, conversation, message, *, proactive=False):
        self.calls.append({"kind": "send", "proactive": proactive})
        return None

    async def send_image(self, conversation, *, data=None, filename=None, proactive=False):
        self.calls.append({"kind": "image", "proactive": proactive})
        return None


# ---------------------------------------------------------------- 通道识别
def test_channel_kind_empty(notify):
    assert notify.channel_kind(_Ctx(None)) == ""
    assert notify.channel_kind(_Ctx()) == ""


def test_channel_kind_from_switchable(notify):
    assert notify.channel_kind(_Ctx(_Switchable("official"))) == "official"
    assert notify.channel_kind(_Ctx(_Switchable("onebot"))) == "onebot"
    assert notify.channel_kind(_Ctx(_Switchable("onebot", "official"))) == "mixed"


def test_channel_kind_falls_back_to_class_name(notify):
    assert notify.channel_kind(_Ctx(_OfficialAdapter())) == "official"
    assert notify.channel_kind(_Ctx(_PlainAdapter())) == ""


# ---------------------------------------------------------------- 媒体策略
def _cfg(**kwargs):
    base = {"media_mode": "auto"}
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_media_allowed_by_channel(notify):
    assert notify.media_allowed(_cfg(), SimpleNamespace(channel="onebot")) is True
    assert notify.media_allowed(_cfg(), SimpleNamespace(channel="official")) is False
    assert notify.media_allowed(_cfg(), SimpleNamespace(channel="")) is True


def test_media_allowed_modes(notify):
    official = SimpleNamespace(channel="official")
    onebot = SimpleNamespace(channel="onebot")
    assert notify.media_allowed(_cfg(media_mode="never"), onebot) is False
    assert notify.media_allowed(_cfg(media_mode="always"), official) is True
    assert notify.media_allowed(_cfg(media_mode="auto"), official) is False


def test_media_allowed_respects_circuit_breaker(notify):
    sender = SimpleNamespace(channel="onebot", media_blocked=True)
    assert notify.media_allowed(_cfg(), sender) is False


# ---------------------------------------------------------------- 目标归一化
def test_conversation_for_normalizes_prefix(notify):
    ctx = _FakeCtxForSend()
    conversation = notify.conversation_for(ctx, "group", "group:123456")
    assert conversation.kind == "group" and conversation.id == "123456"


def test_target_from_context_prefers_message_group_id(notify):
    ctx = SimpleNamespace(
        kind="group",
        conv_id="111",
        message={"group_id": 999888, "user_id": 7},
    )
    assert notify.target_from_context(ctx) == ("group", "999888")


def test_target_from_context_private(notify):
    ctx = SimpleNamespace(kind="private", conv_id="abc", user_id=10001, message=None)
    assert notify.target_from_context(ctx) == ("private", "10001")


# ---------------------------------------------------------------- proactive 兼容
def test_accepts_kwarg_detects_proactive(notify):
    async def with_kw(conversation, message, *, proactive=False):
        return None

    async def without_kw(conversation, message):
        return None

    assert notify._accepts_kwarg(with_kw, "proactive") is True
    assert notify._accepts_kwarg(without_kw, "proactive") is False
    assert notify._accepts_kwarg(None, "proactive") is False


def test_host_sender_passes_proactive_on_new_host(notify):
    ctx = _FakeCtxForSend()
    sender = notify.HostSender(ctx)
    assert sender.supports_proactive_kw is True

    asyncio.run(sender.send_chain("group", "123", [{"type": "text", "data": {"text": "hi"}}]))
    asyncio.run(
        sender.send_chain("group", "123", [{"type": "text", "data": {"text": "hi"}}], proactive=False)
    )

    assert ctx.calls == [
        {"kind": "send", "proactive": True},
        {"kind": "send", "proactive": False},
    ]


def test_host_sender_works_without_proactive(notify):
    """a7/a8 的 ctx.send 没有 proactive，不能因为传参而报错。"""

    class _OldCtx:
        adapter = _PlainAdapter()

        def __init__(self):
            self.calls = 0

        def conversation_from_event(self, data):
            return SimpleNamespace(kind="group", id="1")

        async def send(self, conversation, message):
            self.calls += 1
            return None

    ctx = _OldCtx()
    sender = notify.HostSender(ctx)
    assert sender.supports_proactive_kw is False

    asyncio.run(sender.send_chain("group", "1", [{"type": "text", "data": {"text": "hi"}}]))
    assert ctx.calls == 1
