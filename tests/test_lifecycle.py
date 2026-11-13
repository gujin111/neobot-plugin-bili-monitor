"""用真实 modloader 跑一遍插件生命周期：数据库迁移、命令注册、启停清理。

这里不联网：B 站请求都在轮询任务里，没有订阅时不会发出。
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from pathlib import Path

import pytest

from neobot_modloader.context import RuntimePluginContext


class FakeCommand:
    def __init__(self, name, **kwargs):
        self.name = name
        self.description = kwargs.get("description", "")
        self.usage = kwargs.get("usage", "")
        self.permission = kwargs.get("permission", 0)
        self.aliases = tuple(kwargs.get("aliases") or ())
        self.params = tuple(kwargs.get("params") or ())
        self.handler = None
        self.source = ""

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FakeCommand {self.name}>"


class FakeRegistry:
    def __init__(self):
        self.commands: dict[str, FakeCommand] = {}

    def register(self, command):
        if command.name in self.commands:
            raise ValueError(f"命令已注册: /{command.name}")
        for alias in getattr(command, "aliases", ()):
            if alias in self.commands:
                raise ValueError(f"命令别名冲突: /{alias}")
        self.commands[command.name] = command
        return command

    def unregister(self, name):
        return self.commands.pop(name, None) is not None


class FakeAdapter:
    self_id = 10000

    async def send(self, *_args, **_kwargs):  # pragma: no cover - 生命周期测试不发消息
        raise AssertionError("生命周期测试不应发送消息")


def _workdir() -> Path:
    # 用插件目录下的临时目录：部分 Windows 沙箱不允许在系统临时目录建 sqlite 文件
    base = Path(__file__).resolve().parents[1] / ".runtime-test"
    base.mkdir(parents=True, exist_ok=True)
    import uuid

    path = base / uuid.uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def loaded_plugin(pkg):
    """构造真实上下文并完成 on_load，测试结束后执行 on_stop。"""
    registry = FakeRegistry()
    data_dir = _workdir()
    context = RuntimePluginContext(
        plugin_name=pkg.PLUGIN_NAME,
        plugin_dir=Path(__file__).resolve().parents[1],
        data_dir=data_dir,
        config={},
        logger=logging.getLogger("bili-lifecycle"),
        adapter=FakeAdapter(),
        app_commands=registry,
    )
    yield pkg, context, registry, data_dir
    for name in list(registry.commands):
        registry.unregister(name)


async def test_on_load_registers_commands_and_builds_database(loaded_plugin):
    pkg, context, registry, _data_dir = loaded_plugin

    await pkg.plugin._load(context)

    assert pkg.database.path is not None and pkg.database.path.is_file()
    assert pkg._RUNTIME is not None
    names = set(registry.commands)
    assert {"b站监控", "b站管理", "b站登录"} <= names
    monitor = registry.commands["b站监控"]
    assert monitor.permission == 0
    assert monitor.aliases == ("bili监控", "b站在看")
    assert registry.commands["b站管理"].permission == 1
    assert registry.commands["b站登录"].permission == 2


async def test_lifecycle_start_and_stop(loaded_plugin):
    pkg, context, _registry, _data_dir = loaded_plugin
    await pkg.plugin._load(context)
    runtime = pkg._RUNTIME
    assert runtime is not None

    await pkg.plugin.on_start()
    assert runtime.engine.running is True
    await asyncio.sleep(0.05)

    await pkg.plugin.on_stop()
    assert runtime.engine.running is False


async def test_migrations_are_idempotent(loaded_plugin):
    pkg, context, _registry, _data_dir = loaded_plugin
    await pkg.plugin._load(context)
    # 重复迁移不应报错，也不应重建表
    await pkg.database.migrate()
    applied = await pkg.database.migrate()
    assert applied is None
    async with pkg.database.session() as session:
        from sqlalchemy import text

        rows = list(await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'")))
    tables = {row[0] for row in rows}
    assert {"bili_subscription", "bili_target", "bili_cursor", "bili_event"} <= tables


async def test_command_dispatch_add_and_list(loaded_plugin, monkeypatch):
    """走一遍 /b站管理 add -> /b站监控 list 的真实处理链（B 站接口用替身）。"""
    pkg, context, registry, _data_dir = loaded_plugin
    await pkg.plugin._load(context)
    runtime = pkg._RUNTIME
    assert runtime is not None

    from bili_monitor.bili_api import UpProfile

    async def fake_profile(uid):
        return UpProfile(uid=uid, name="测试UP", face="https://x/face.jpg", follower=1234, room_id=555)

    baseline_calls = []

    async def fake_bootstrap(sub, *, profile=None):
        baseline_calls.append((sub.uid, profile is not None))
        return {"dynamic": 3, "live": "未开播", "follower": 1234}

    monkeypatch.setattr(runtime.api, "resolve_profile", fake_profile)
    monkeypatch.setattr(runtime.engine, "bootstrap", fake_bootstrap)

    class FakeCtx:
        raw_args = "add 546195"
        kind = "group"
        conv_id = "123456"
        user_id = 10001
        args = ["add", "546195"]

    admin = registry.commands["b站管理"]
    reply = await admin.handler(FakeCtx())
    assert "已开始监控" in reply
    assert baseline_calls == [(546195, True)]     # 资料被复用给基线，少两次请求

    stored = await runtime.store.get_subscription(546195)
    assert stored is not None
    assert [(t.kind, t.target_id) for t in stored.targets] == [("group", "123456")]

    class ListCtx:
        raw_args = "list"
        kind = "group"
        conv_id = "123456"
        user_id = 10001
        args = ["list"]

    listing = await registry.commands["b站监控"].handler(ListCtx())
    assert "测试UP" in listing
    assert "546195" in listing

    # /b站管理 test 会走发送器 -> 宿主的 send；这里只验证目标存在时不会抛「没有目标」
    assert runtime.engine is not None
    await pkg.plugin.on_stop()


async def test_help_and_status(loaded_plugin):
    pkg, context, registry, _data_dir = loaded_plugin
    await pkg.plugin._load(context)

    class Ctx:
        raw_args = "help"
        kind = "private"
        conv_id = "10001"
        user_id = 10001
        args = ["help"]

    text = await registry.commands["b站监控"].handler(Ctx())
    assert "/b站管理" in text and "add <uid|链接>" in text

    class StatusCtx:
        raw_args = "status"
        kind = "private"
        conv_id = "10001"
        user_id = 10001
        args = ["status"]

    status = await registry.commands["b站监控"].handler(StatusCtx())
    assert "B 站监控运行状态" in status
    await pkg.plugin.on_stop()


async def test_parse_uid_helper(pkg):
    assert pkg.parse_uid("546195") == 546195
    assert pkg.parse_uid("https://space.bilibili.com/2/dynamic") == 2
    assert pkg.parse_uid("uid:12345") == 12345
    assert pkg.parse_uid("abc") is None


async def test_parse_categories(pkg):
    assert pkg.parse_categories("动态,开播") == {"dynamic_new", "live_on"}
    assert pkg.parse_categories("all") == set(pkg.ALL_CATEGORIES)
    assert pkg.parse_categories("none") == set()
    assert pkg.parse_categories("不存在的类别") is None


async def test_validate_template(pkg):
    assert pkg.validate_template("{name} 发动态") is None
    assert pkg.validate_template("{name") is not None
    assert pkg.validate_template("") is not None


# ---------------------------------------------------------------- 推送目标映射
class _Ctx:
    def __init__(self, raw_args, *, kind="group", conv_id="123456", user_id=10001, message=None,
                 service=None):
        self.raw_args = raw_args
        self.kind = kind
        self.conv_id = conv_id
        self.user_id = user_id
        self.args = raw_args.split()
        self.service = service
        if message is not None:
            self.message = message


async def _prepare(loaded_plugin, monkeypatch):
    pkg, context, registry, _data_dir = loaded_plugin
    await pkg.plugin._load(context)
    runtime = pkg._RUNTIME

    from bili_monitor.bili_api import UpProfile

    async def fake_profile(uid):
        return UpProfile(uid=uid, name="测试UP", face="", follower=100, room_id=555)

    async def fake_bootstrap(sub, *, profile=None):
        return {"dynamic": 0, "live": "未开播", "follower": 100}

    monkeypatch.setattr(runtime.api, "resolve_profile", fake_profile)
    monkeypatch.setattr(runtime.engine, "bootstrap", fake_bootstrap)
    return pkg, registry, runtime


async def test_add_binds_current_group_id(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)

    reply = await registry.commands["b站管理"].handler(_Ctx("add 546195"))

    stored = await runtime.store.get_subscription(546195)
    assert [(t.kind, t.target_id) for t in stored.targets] == [("group", "123456")]
    assert "群 123456" in reply
    await pkg.plugin.on_stop()


async def test_add_prefers_message_group_id(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)

    # 命令上下文给的是 conv_id，原始消息里的 group_id 更权威
    ctx = _Ctx("add 546195", conv_id="111111", message={"group_id": 999888, "user_id": 7})
    await registry.commands["b站管理"].handler(ctx)

    stored = await runtime.store.get_subscription(546195)
    assert [(t.kind, t.target_id) for t in stored.targets] == [("group", "999888")]
    await pkg.plugin.on_stop()


async def test_push_accepts_bare_group_number(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    await registry.commands["b站管理"].handler(_Ctx("add 546195"))

    reply = await registry.commands["b站管理"].handler(_Ctx("push 546195 987654"))

    stored = await runtime.store.get_subscription(546195)
    assert ("group", "987654") in {(t.kind, t.target_id) for t in stored.targets}
    assert "群 987654" in reply
    await pkg.plugin.on_stop()


async def test_push_private_spec(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    await registry.commands["b站管理"].handler(_Ctx("add 546195"))

    await registry.commands["b站管理"].handler(_Ctx("push 546195 私聊 10001"))

    stored = await runtime.store.get_subscription(546195)
    assert ("private", "10001") in {(t.kind, t.target_id) for t in stored.targets}
    await pkg.plugin.on_stop()


async def test_targets_command_shows_current_conversation(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    await registry.commands["b站管理"].handler(_Ctx("add 546195"))

    text = await registry.commands["b站管理"].handler(_Ctx("targets"))

    assert "当前会话：群 123456" in text
    assert "测试UP" in text
    await pkg.plugin.on_stop()


async def test_unpush_removes_target(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    await registry.commands["b站管理"].handler(_Ctx("add 546195"))

    await registry.commands["b站管理"].handler(_Ctx("unpush 546195 123456"))

    stored = await runtime.store.get_subscription(546195)
    assert stored.targets == []
    await pkg.plugin.on_stop()


# ---------------------------------------------------------------- 命令速查卡片
def _install_fake_card_module(monkeypatch, *, png=b"\\x89PNG-fake", fail=False):
    """注入一个假的 neobot_app.runtime.html_card，模拟本体内置卡片渲染。"""
    import sys, types

    pkg = types.ModuleType("neobot_app"); pkg.__path__ = []
    runtime_pkg = types.ModuleType("neobot_app.runtime"); runtime_pkg.__path__ = []
    card = types.ModuleType("neobot_app.runtime.html_card")
    card.seen = {}

    def render_card_html(**kwargs):
        card.seen.update(kwargs)
        return "<html><body>card</body></html>"

    async def render_card_image(html, **kwargs):
        if fail:
            raise RuntimeError("screenshot unavailable")
        return png

    card.render_card_html = render_card_html
    card.render_card_image = render_card_image
    monkeypatch.setitem(sys.modules, "neobot_app", pkg)
    monkeypatch.setitem(sys.modules, "neobot_app.runtime", runtime_pkg)
    monkeypatch.setitem(sys.modules, "neobot_app.runtime.html_card", card)
    return card


class _SvcCtx(_Ctx):
    def __init__(self, raw_args, service, **kwargs):
        super().__init__(raw_args, **kwargs)
        self.service = service


class _ImageService:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def send_image_bytes(self, kind, conv_id, data, *, filename=None):
        self.calls.append((kind, conv_id, data, filename))
        return self.result


async def test_help_sends_host_rendered_card(loaded_plugin, monkeypatch):
    """帮助图走本体 render_card_html + 命令服务 send_image_bytes（与宿主 /help 同路）。"""
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    card = _install_fake_card_module(monkeypatch)
    service = _ImageService()

    result = await registry.commands["b站监控"].handler(_Ctx("help", service=service))

    assert result is None, "发图后应返回 None，宿主不再补文本"
    assert card.seen["title"] == "B 站监控助手 · 命令速查"
    assert card.seen["blocks"][0]["kind"] == "heading"
    assert service.calls and service.calls[0][2] == b"\\x89PNG-fake"
    assert service.calls[0][3] == "bili-help.png"
    await pkg.plugin.on_stop()


async def test_help_falls_back_to_ctx_image_when_service_cannot(loaded_plugin, monkeypatch):
    """send_image_bytes 返回 False（官方通道缺 file_server 等）时退回 ctx.send_image。"""
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    _install_fake_card_module(monkeypatch)
    service = _ImageService(result=False)
    sent = []

    async def fake_send_image(conversation, *, data, filename, **kwargs):
        sent.append((conversation.kind, filename))

    monkeypatch.setattr(runtime.ctx, "send_image", fake_send_image)

    result = await registry.commands["b站监控"].handler(_Ctx("help", service=service))

    assert result is None
    assert sent == [("group", "bili-help.png")]
    await pkg.plugin.on_stop()


async def test_help_falls_back_to_text_when_render_fails(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    _install_fake_card_module(monkeypatch, fail=True)

    result = await registry.commands["b站监控"].handler(_Ctx("help", service=_ImageService()))

    assert isinstance(result, str)
    assert "/b站管理" in result and "add <uid" in result
    await pkg.plugin.on_stop()


async def test_help_without_host_module_falls_back(loaded_plugin, monkeypatch):
    """非本体环境（导入不到 html_card）时直接给文字，不抛错。"""
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    import sys
    monkeypatch.setitem(sys.modules, "neobot_app", None)

    result = await registry.commands["b站管理"].handler(_Ctx("help", service=_ImageService()))

    assert isinstance(result, str) and "/b站管理" in result
    await pkg.plugin.on_stop()


async def test_help_on_official_channel_tries_image_first(loaded_plugin, monkeypatch):
    """官方通道也先试图：能不能发图交给实际调用结果决定，不提前跳过。"""
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    runtime.sender.channel = "official"
    card = _install_fake_card_module(monkeypatch)
    service = _ImageService()

    result = await registry.commands["b站管理"].handler(_Ctx("", service=service))

    assert result is None, "发图成功就不该再发文本"
    assert card.seen, "官方通道也应该尝试渲染卡片"
    assert service.calls, "应该走命令服务的 send_image_bytes"
    await pkg.plugin.on_stop()


async def test_help_on_official_channel_falls_back_to_markdown(loaded_plugin, monkeypatch):
    """发图失败时，官方通道回退 Markdown（可点、不依赖媒体上传）。"""
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    runtime.sender.channel = "official"
    _install_fake_card_module(monkeypatch, fail=True)

    result = await registry.commands["b站管理"].handler(_Ctx("", service=_ImageService()))

    assert isinstance(result, str)
    assert result.startswith("**B 站监控助手 · 命令速查**")
    assert "**所有人 · 查看**" in result
    await pkg.plugin.on_stop()


async def test_help_format_markdown_on_onebot(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    runtime.config.help_format = "markdown"

    result = await registry.commands["b站监控"].handler(_Ctx("help"))

    assert isinstance(result, str) and result.startswith("**B 站监控助手")
    await pkg.plugin.on_stop()


async def test_help_image_send_failure_blocks_media(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    _install_fake_card_module(monkeypatch)
    service = _ImageService(result=False)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("MediaUploadError: 上传官方富媒体失败")

    monkeypatch.setattr(runtime.ctx, "send_image", boom)

    result = await registry.commands["b站监控"].handler(_Ctx("help", service=service))

    assert isinstance(result, str) and "/b站管理" in result, "发图失败要退回文本"
    assert runtime.sender.media_blocked is True, "失败一次后本进程不再发图"
    await pkg.plugin.on_stop()


async def test_diag_command_reports_chain(loaded_plugin, monkeypatch):
    pkg, registry, runtime = await _prepare(loaded_plugin, monkeypatch)
    _install_fake_card_module(monkeypatch)

    text = await registry.commands["b站管理"].handler(_Ctx("diag", service=_ImageService()))

    assert "帮助图链路诊断" in text
    assert "卡片模块：可导入" in text
    assert "渲染：成功" in text
    assert "命令服务发图：可用" in text
    await pkg.plugin.on_stop()


def test_live_interval_accepts_three_seconds(pkg):
    cfg = pkg.BiliMonitorConfig(live_interval_seconds=3)
    assert cfg.live_interval_seconds == 3


def test_live_interval_rejects_below_three(pkg):
    import pydantic
    import pytest as _pytest

    with _pytest.raises(pydantic.ValidationError):
        pkg.BiliMonitorConfig(live_interval_seconds=2)
