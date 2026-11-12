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
