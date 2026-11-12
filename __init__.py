"""B 站监控助手（NeoBot 插件）：UP 主动态 / 直播 / 粉丝里程碑提醒。

移植自桌面版「bili 监控助手 1.7.20」（易语言 DLL，支持 MyQQ / IR / NTQQ 等多框架），
把原来的窗口配置改成命令 + 网页面板配置，多框架发送改由 NeoBot 适配器统一完成。

命令分三级（权限由宿主在处理器之前检查）：
- ``/b站监控``  所有人：查看订阅、状态、帮助
- ``/b站管理``  次级管理员：增删订阅、开关提醒、推送目标、测试
- ``/b站登录``  超级管理员：扫码登录 B 站账号（降低风控）
"""

from __future__ import annotations

import asyncio
import json
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neobot_modloader import Migration, MessageChain, Plugin, Reply

from .bili_api import BiliApiError, BiliClient
from .bili_config import BiliMonitorConfig
from .bili_engine import MonitorEngine
from .bili_model import DYNAMIC_KINDS, format_count, format_duration
from .bili_notify import HostSender, HtmlCardRenderer, conversation_for
from .bili_render import DEFAULT_TEMPLATES, describe_template_fields, render_text
from .bili_store import (
    CATEGORY_LABELS,
    DEFAULT_CATEGORIES,
    BiliStore,
    Subscription,
    create_schema,
)

PLUGIN_NAME = "bili_monitor"
VERSION = "1.0.0"

plugin = Plugin(
    PLUGIN_NAME,
    version=VERSION,
    description="B 站 UP 主动态 / 开播 / 粉丝里程碑监控（移植自 bili 监控助手 1.7.20）",
    usage="/b站监控 查看订阅；/b站管理 add <uid> 添加监控",
    author="ported for NeoBot",
    config=BiliMonitorConfig,
    hot_reload=True,
    config_hot_reload=False,
)

database = plugin.sqlite_database(
    "main",
    filename="bili_monitor.db",
    migrations=[Migration(1, "initial-schema", create_schema)],
)

#: 类别中文别名 -> 内部键
CATEGORY_ALIASES: dict[str, str] = {
    "动态": "dynamic_new", "新动态": "dynamic_new", "dynamic": "dynamic_new", "dynamic_new": "dynamic_new",
    "转发": "dynamic_repost", "转发动态": "dynamic_repost", "repost": "dynamic_repost",
    "dynamic_repost": "dynamic_repost",
    "视频": "dynamic_video", "新视频": "dynamic_video", "video": "dynamic_video",
    "dynamic_video": "dynamic_video",
    "专栏": "dynamic_article", "新专栏": "dynamic_article", "article": "dynamic_article",
    "dynamic_article": "dynamic_article",
    "音频": "dynamic_audio", "新音频": "dynamic_audio", "audio": "dynamic_audio",
    "dynamic_audio": "dynamic_audio",
    "开播": "live_on", "开播提醒": "live_on", "live_on": "live_on",
    "下播": "live_off", "下播提醒": "live_off", "live_off": "live_off",
    "粉丝": "follower", "粉丝数": "follower", "fans": "follower", "follower": "follower",
}

ALL_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_LABELS.keys())

SEND_MODES = ("auto", "text", "image", "card")
ADMIN_PERMISSION = 1
OWNER_PERMISSION = 2


# ---------------------------------------------------------------- 运行时
@dataclass
class _Runtime:
    ctx: Any
    config: BiliMonitorConfig
    store: BiliStore
    api: BiliClient
    sender: HostSender
    cards: HtmlCardRenderer
    engine: MonitorEngine
    cookie_path: Path
    started_at: float = field(default_factory=time.time)

    def save_cookie(self, raw: str) -> None:
        payload = {"cookie": raw, "updated_at": int(time.time())}
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_cookie(self) -> str:
        try:
            data = json.loads(self.cookie_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        return str(data.get("cookie") or "")


_RUNTIME: _Runtime | None = None


def _runtime() -> _Runtime:
    if _RUNTIME is None:
        raise RuntimeError("插件尚未加载完成")
    return _RUNTIME


def _log(level: str, message: str) -> None:
    runtime = _RUNTIME
    if runtime is None:
        return
    try:
        getattr(runtime.ctx.logger, level)(f"[bili] {message}")
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------- 解析工具
def parse_uid(token: str) -> int | None:
    """从 ``123`` / ``uid:123`` / 空间链接里取出 UID。"""
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits and ("space.bilibili.com" in raw or "bilibili.com" in raw or raw.startswith("uid")):
        return int(digits.split("/")[0])
    return int(digits) if digits and digits == raw.strip("/").split("/")[-1] else None


def parse_categories(tokens: str) -> set[str] | None:
    """``动态,视频 开播`` -> 类别集合；无法识别返回 None。"""
    raw = str(tokens or "").strip()
    if not raw:
        return None
    lowered = raw.lower()
    if lowered in {"all", "全部", "全开"}:
        return set(ALL_CATEGORIES)
    if lowered in {"none", "无", "全关", "off"}:
        return set()
    selected: set[str] = set()
    for chunk in raw.replace("，", ",").replace("、", ",").replace(" ", ",").split(","):
        key = chunk.strip()
        if not key:
            continue
        mapped = CATEGORY_ALIASES.get(key) or CATEGORY_ALIASES.get(key.lower())
        if mapped is None:
            return None
        selected.add(mapped)
    return selected


def validate_template(template: str) -> str | None:
    """返回错误说明；模板合法返回 None。"""
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        return f"模板语法错误：{exc}"
    if not parsed:
        return "模板不能为空"
    for _literal, field_name, _spec, _conv in parsed:
        if field_name is None:
            continue
        root = field_name.split(".")[0].split("[")[0]
        if not root.isidentifier() and not root.isdigit():
            return f"占位符 {{{field_name}}} 不合法"
    return None


def _conv_kind(command_ctx: Any) -> str:
    return "group" if str(getattr(command_ctx, "kind", "")) == "group" else "private"


def _conv_id(command_ctx: Any) -> str:
    return str(getattr(command_ctx, "conv_id", "") or "")


def _sub_title(sub: Subscription) -> str:
    state = "监控中" if sub.enabled else "已暂停"
    kinds = "、".join(CATEGORY_LABELS.get(key, key) for key in sorted(sub.category_set))
    room = f" | 直播间 {sub.room_id}" if sub.room_id else ""
    return (
        f"· {sub.display_name}（UID {sub.uid}{room}）{state}\n"
        f"  提醒：{kinds or '无'}\n"
        f"  模式：{sub.send_mode or '跟随全局'} | @全体：{'是' if sub.at_all else '否'} | "
        f"目标：{_targets_text(sub)}"
    )


def _targets_text(sub: Subscription) -> str:
    if not sub.targets:
        return "未设置"
    parts = []
    for target in sub.targets:
        label = "群" if target.kind == "group" else "私聊"
        parts.append(f"{label}{target.target_id}")
    return "、".join(parts)


async def _list_text(runtime: _Runtime) -> str:
    subs = await runtime.store.list_subscriptions()
    if not subs:
        return (
            "还没有任何 B 站监控订阅。\n"
            "用 /b站管理 add <UID或空间链接> 添加，例如：/b站管理 add 2"
        )
    lines = [f"当前监控 {len(subs)} 个 UP 主：", ""]
    lines.extend(_sub_title(sub) for sub in subs)
    return "\n".join(lines)


async def _send_payload(ctx: Any, kind: str, conv_id: str, payload: Any) -> None:
    """向指定会话发消息；会话号非数字时退回通用 send。"""
    try:
        numeric = int(conv_id)
    except (TypeError, ValueError):
        await ctx.send(conversation_for(ctx, kind, conv_id), payload)
        return
    if kind == "group":
        await ctx.send_group(numeric, payload)
    else:
        await ctx.send_private(numeric, payload)


async def _resolve_sub(runtime: _Runtime, token: str) -> tuple[Subscription | None, str]:
    uid = parse_uid(token)
    if uid is None:
        return None, "没找到 UID：请填数字 UID 或空间链接，例如 /b站管理 add 546195"
    sub = await runtime.store.get_subscription(uid)
    if sub is None:
        return None, f"没有监控 UID {uid}，先用 /b站管理 add {uid} 添加。"
    return sub, ""


# ---------------------------------------------------------------- 帮助
def _help_text() -> str:
    return (
        "B 站监控助手（动态 / 开播 / 粉丝里程碑）\n"
        "\n"
        "【所有人】/b站监控\n"
        "  list            查看订阅列表\n"
        "  info <uid>      查看某个 UP 主的监控配置\n"
        "  status          查看轮询与推送状态\n"
        "\n"
        "【管理员】/b站管理\n"
        "  add <uid|链接> [直播间号]   添加监控（默认推送到当前会话）\n"
        "  del <uid>                   取消监控\n"
        "  on|off <uid>                启用 / 暂停\n"
        "  cat <uid> <类别,...>        设置提醒类别（动态,转发,视频,专栏,音频,开播,下播,粉丝 / all / none）\n"
        "  mode <uid> <auto|text|image|card>  发送模式\n"
        "  at <uid> <on|off>           开播时 @全体成员\n"
        "  atlist <uid> <qq,qq|clear>  提醒时 @指定成员\n"
        "  tpl <uid> <类别> <模板|clear>  自定义文案，占位符见 /b站管理 tpl <uid> <类别> ?\n"
        "  push <uid> <group|private> <id>   追加推送目标\n"
        "  pushhere <uid>              把当前会话加为推送目标\n"
        "  unpush <uid> <group|private> <id> 移除推送目标\n"
        "  test <uid>                  发一条测试提醒\n"
        "  check <uid>                 立即检查一次\n"
        "  sync <uid>                  刷新昵称/头像/直播间号\n"
        "\n"
        "【超级管理员】/b站登录\n"
        "  qr                          扫码登录 B 站账号（降低风控）\n"
        "  status                      查看登录状态\n"
        "  logout                      清除已保存的登录凭证"
    )


# ---------------------------------------------------------------- 命令注册
def register_commands(ctx: Any) -> None:
    """在 on_load 里注册三条命令，权限由宿主统一检查。"""
    registrar = getattr(ctx, "app_commands", None)
    if registrar is None or not getattr(registrar, "available", False):
        _log("warning", "宿主命令表不可用，跳过命令注册")
        return

    async def _monitor(command_ctx: Any) -> str:
        runtime = _RUNTIME
        if runtime is None:
            return "插件尚未加载完成，请稍后再试。"
        args = _split_args(command_ctx.raw_args)
        action = (args[0].lower() if args else "list")
        if action in {"list", "列表", "ls", ""}:
            return await _list_text(runtime)
        if action in {"help", "帮助", "?"}:
            return _help_text()
        if action in {"status", "状态"}:
            return await _status_text(runtime)
        if action in {"info", "详情"}:
            if len(args) < 2:
                return "用法：/b站监控 info <uid>"
            sub, error = await _resolve_sub(runtime, args[1])
            if sub is None:
                return error
            return await _info_text(runtime, sub)
        return f"未知子命令：{action}\n\n{_help_text()}"

    async def _admin(command_ctx: Any) -> str:
        runtime = _RUNTIME
        if runtime is None:
            return "插件尚未加载完成，请稍后再试。"
        args = _split_args(command_ctx.raw_args)
        if not args:
            return _help_text()
        action = args[0].lower()
        handler = _ADMIN_HANDLERS.get(action)
        if handler is None:
            return f"未知子命令：{args[0]}\n\n{_help_text()}"
        return await handler(runtime, command_ctx, args[1:])

    async def _login(command_ctx: Any) -> str:
        runtime = _RUNTIME
        if runtime is None:
            return "插件尚未加载完成，请稍后再试。"
        args = _split_args(command_ctx.raw_args)
        action = (args[0].lower() if args else "status")
        if action in {"status", "状态", ""}:
            name = await runtime.api.check_login()
            return f"B 站登录状态：{name}" if name else "当前以匿名方式访问 B 站（可能被风控），可用 /b站登录 qr 扫码登录。"
        if action in {"logout", "退出"}:
            runtime.cookie_path.unlink(missing_ok=True)
            runtime.api._cookies.pop("SESSDATA", None)  # noqa: SLF001 - 主动退出登录
            return "已清除本地保存的 B 站登录凭证（面板 cookie 配置不受影响）。"
        if action in {"qr", "扫码", "登录"}:
            return await _start_qr_login(runtime, command_ctx)
        return "用法：/b站登录 [qr|status|logout]"

    registrar.register(
        "b站监控",
        description="查看 B 站监控订阅、UP 主详情与运行状态",
        usage="[list|info <uid>|status|help]",
        permission=0,
        aliases=("bili监控", "b站在看"),
        params=(("list", "订阅列表"), ("info <uid>", "订阅详情"), ("status", "运行状态")),
    )(_monitor)

    registrar.register(
        "b站管理",
        description="增删 B 站监控订阅、设置提醒类别与推送目标（管理员）",
        usage="[add|del|on|off|cat|mode|at|atlist|tpl|push|pushhere|unpush|test|check|sync] ...",
        permission=ADMIN_PERMISSION,
        aliases=("bili管理",),
        params=(
            ("add <uid> [直播间号]", "添加监控"),
            ("del <uid>", "取消监控"),
            ("cat <uid> <类别>", "设置提醒类别"),
            ("mode <uid> <模式>", "设置发送模式"),
            ("tpl <uid> <类别> <模板>", "自定义文案"),
            ("pushhere <uid>", "推送到当前会话"),
            ("test <uid>", "发送测试提醒"),
        ),
    )(_admin)

    registrar.register(
        "b站登录",
        description="扫码登录 B 站账号，降低动态/直播接口风控概率（超级管理员）",
        usage="[qr|status|logout]",
        permission=OWNER_PERMISSION,
        aliases=("bili登录",),
    )(_login)


def _split_args(raw: str | None) -> list[str]:
    return [chunk for chunk in str(raw or "").replace("\u3000", " ").split(" ") if chunk.strip()]


# ---------------------------------------------------------------- 管理员子命令
async def _admin_add(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if not args:
        return "用法：/b站管理 add <UID或空间链接> [直播间号]"
    uid = parse_uid(args[0])
    if uid is None:
        return "没找到 UID：请填数字 UID 或空间链接，例如 /b站管理 add 546195"
    if await runtime.store.get_subscription(uid) is not None:
        return f"UID {uid} 已经在监控列表里了，用 /b站管理 sync {uid} 刷新资料。"

    limit = int(runtime.config.max_subscriptions)
    if await runtime.store.count_subscriptions() >= limit:
        return f"订阅数已达上限 {limit}，请先取消一些或调大「订阅上限」配置。"

    try:
        profile = await runtime.api.resolve_profile(uid)
    except BiliApiError as exc:
        return f"查询 UP 主资料失败：{exc}"
    if not profile.name:
        return f"没有找到 UID {uid} 对应的 UP 主（可能不存在或接口风控），请稍后重试。"

    room_id: int | None = profile.room_id
    if len(args) > 1 and args[1].isdigit():
        room_id = int(args[1])

    sub = await runtime.store.create_subscription(
        uid=uid,
        uname=profile.name,
        face=profile.face,
        room_id=room_id,
        created_by=str(getattr(command_ctx, "user_id", "") or ""),
        at_all=bool(runtime.config.at_all),
    )
    await runtime.store.add_target(sub.id, _conv_kind(command_ctx), _conv_id(command_ctx))
    baseline = await runtime.engine.bootstrap(sub)

    lines = [
        f"已开始监控 {profile.name}（UID {uid}）。",
        f"头像：{'已获取' if profile.face else '无'} | 直播间：{room_id or '无'}",
        f"提醒类别：{'、'.join(CATEGORY_LABELS[key] for key in sorted(sub.category_set))}",
        f"推送目标：{_conv_kind(command_ctx)} {_conv_id(command_ctx)}",
        f"基线：动态 {baseline.get('dynamic', 0)} 条 / 直播状态 {baseline.get('live', '未知')}"
        f" / 粉丝 {format_count(baseline.get('follower', 0))}",
        "从现在开始的新动态会推送到这里；用 /b站管理 test 发一条测试提醒验证链路。",
    ]
    return "\n".join(lines)


async def _admin_del(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    await runtime.store.delete_subscription(sub.uid)
    return f"已取消对 {sub.display_name}（UID {sub.uid}）的监控。"


async def _admin_on(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    return await _set_enabled(runtime, args, True)


async def _admin_off(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    return await _set_enabled(runtime, args, False)


async def _set_enabled(runtime: _Runtime, args: list[str], enabled: bool) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    await runtime.store.update_subscription(sub.uid, enabled=enabled)
    return f"{sub.display_name} 已{'恢复' if enabled else '暂停'}监控。"


async def _admin_cat(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 2:
        return "用法：/b站管理 cat <uid> <动态,转发,视频,专栏,音频,开播,下播,粉丝 | all | none>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    selected = parse_categories(" ".join(args[1:]))
    if selected is None:
        return "无法识别的类别。可选：动态、转发、视频、专栏、音频、开播、下播、粉丝，或用 all / none。"
    await runtime.store.update_subscription(sub.uid, categories=json.dumps(sorted(selected)))
    labels = "、".join(CATEGORY_LABELS[key] for key in sorted(selected)) or "无"
    return f"{sub.display_name} 的提醒类别已设为：{labels}"


async def _admin_mode(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 2:
        return "用法：/b站管理 mode <uid> <auto|text|image|card|default>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    mode = args[1].lower()
    if mode in {"default", "默认"}:
        await runtime.store.update_subscription(sub.uid, send_mode="")
        return f"{sub.display_name} 的发送模式已改为跟随全局（{runtime.config.send_mode}）。"
    if mode not in SEND_MODES:
        return f"发送模式只能是 {' / '.join(SEND_MODES)}，或 default 表示跟随全局。"
    await runtime.store.update_subscription(sub.uid, send_mode=mode)
    return f"{sub.display_name} 的发送模式已设为 {mode}。"


async def _admin_at(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 2:
        return "用法：/b站管理 at <uid> <on|off>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    flag = args[1].lower() in {"on", "开", "true", "1", "是"}
    await runtime.store.update_subscription(sub.uid, at_all=flag)
    hint = "（@全体需要机器人有群管理员权限）" if flag else ""
    return f"{sub.display_name} 的开播提醒 @全体 已{'开启' if flag else '关闭'}{hint}。"


async def _admin_atlist(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 2:
        return "用法：/b站管理 atlist <uid> <qq,qq|clear>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    raw = " ".join(args[1:]).strip()
    if raw.lower() in {"clear", "清空", "none"}:
        await runtime.store.update_subscription(sub.uid, at_list="")
        return f"{sub.display_name} 的 @ 列表已清空。"
    ids = [chunk.strip() for chunk in raw.replace("，", ",").split(",") if chunk.strip()]
    if not all(item.isdigit() for item in ids):
        return "只能填 QQ 号，用逗号分隔，例如 123456,789012。"
    await runtime.store.update_subscription(sub.uid, at_list=",".join(ids))
    return f"{sub.display_name} 的 @ 列表已设为：{', '.join(ids)}"


async def _admin_tpl(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 2:
        return (
            "用法：/b站管理 tpl <uid> <类别> <模板|clear>\n"
            "类别：" + "、".join(sorted(CATEGORY_ALIASES, key=len))[:80] + "…"
        )
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    kind = CATEGORY_ALIASES.get(args[1]) or CATEGORY_ALIASES.get(args[1].lower())
    if kind is None:
        return "未知类别。可用：动态、转发、视频、专栏、音频、开播、下播、粉丝。"
    body = " ".join(args[2:]).strip()
    if body in {"?", "占位符"}:
        return (
            f"{CATEGORY_LABELS.get(kind, kind)} 可用占位符：\n{describe_template_fields(kind)}\n\n"
            f"当前默认模板：\n{DEFAULT_TEMPLATES.get(kind, '')}"
        )
    if body in {"clear", "重置", "default"}:
        await runtime.store.update_subscription(sub.uid, templates=_set_template(sub, kind, ""))
        return f"{sub.display_name} 的「{CATEGORY_LABELS.get(kind, kind)}」文案已恢复默认。"
    if not body:
        return "模板不能为空；要恢复默认请用 clear。"
    problem = validate_template(body)
    if problem:
        return problem
    await runtime.store.update_subscription(sub.uid, templates=_set_template(sub, kind, body))
    preview = render_text(
        _preview_event(sub, kind), custom={kind: body},
        include_link=bool(runtime.config.include_link),
    )
    return f"已更新「{CATEGORY_LABELS.get(kind, kind)}」文案，效果预览：\n\n{preview}"


def _set_template(sub: Subscription, kind: str, template: str) -> str:
    values = sub.template_map
    if template:
        values[kind] = template
    else:
        values.pop(kind, None)
    return json.dumps(values, ensure_ascii=False)


def _preview_event(sub: Subscription, kind: str):
    from .bili_model import MonitorEvent

    return MonitorEvent(
        kind=kind,
        uid=sub.uid,
        uname=sub.uname or str(sub.uid),
        face=sub.face,
        event_key="preview",
        url=f"https://space.bilibili.com/{sub.uid}",
        title="示例标题",
        text="这里是正文示例。",
        extra={
            "duration": "12:34", "duration_text": "1 小时 2 分", "area": "虚拟主播",
            "follower": 12345, "milestone": 10000, "peak_online": 45678,
            "peak": 45678, "online": 1234, "play": 98765, "room_id": sub.room_id or 0,
        },
    )


async def _admin_push(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 3:
        return "用法：/b站管理 push <uid> <group|private> <群号或QQ>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    kind = args[1].lower()
    if kind in {"群", "qun"}:
        kind = "group"
    if kind in {"私聊", "好友"}:
        kind = "private"
    if kind not in {"group", "private"}:
        return "第二个参数只能是 group 或 private。"
    if not args[2].isdigit():
        return "目标 ID 必须是数字。"
    added = await runtime.store.add_target(sub.id, kind, args[2])
    if not added:
        return f"{sub.display_name} 已经推送到 {kind} {args[2]} 了。"
    return f"{sub.display_name} 新增推送目标：{kind} {args[2]}"


async def _admin_pushhere(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    kind, conv_id = _conv_kind(command_ctx), _conv_id(command_ctx)
    added = await runtime.store.add_target(sub.id, kind, conv_id)
    if not added:
        return f"{sub.display_name} 已经推送到当前会话了。"
    return f"{sub.display_name} 已加为当前会话（{kind} {conv_id}）的推送。"


async def _admin_unpush(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    if len(args) < 3:
        return "用法：/b站管理 unpush <uid> <group|private> <群号或QQ>"
    sub, error = await _resolve_sub(runtime, args[0])
    if sub is None:
        return error
    kind = "group" if args[1].lower() in {"group", "群", "qun"} else "private"
    removed = await runtime.store.remove_target(sub.id, kind, args[2])
    return (
        f"已移除推送目标 {kind} {args[2]}。" if removed else f"没有找到推送目标 {kind} {args[2]}。"
    )


async def _admin_test(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    if not sub.targets:
        return f"{sub.display_name} 还没有推送目标，先 /b站管理 pushhere {sub.uid}。"
    sent = 0
    for target in sub.targets:
        if await runtime.engine.send_test(sub, kind=target.kind, target_id=target.target_id):
            sent += 1
    return f"已向 {sent}/{len(sub.targets)} 个目标发送测试提醒。"


async def _admin_check(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    await runtime.engine.poll_now(sub)
    sub = await runtime.store.get_subscription(sub.uid) or sub
    return f"已立即检查一次。\n{await _info_text(runtime, sub)}"


async def _admin_sync(runtime: _Runtime, command_ctx: Any, args: list[str]) -> str:
    sub, error = await _resolve_sub(runtime, args[0] if args else "")
    if sub is None:
        return error
    try:
        profile = await runtime.api.resolve_profile(sub.uid)
    except BiliApiError as exc:
        return f"刷新失败：{exc}"
    if not profile.name:
        return "没有取到最新资料，可能触发风控，请稍后再试。"
    await runtime.store.update_subscription(
        sub.uid, uname=profile.name, face=profile.face,
        room_id=profile.room_id or sub.room_id,
    )
    return (
        f"已刷新：{profile.name}（UID {sub.uid}）\n"
        f"直播间：{profile.room_id or sub.room_id or '无'} | "
        f"粉丝：{format_count(profile.follower or 0)}"
    )


_ADMIN_HANDLERS = {
    "add": _admin_add, "添加": _admin_add,
    "del": _admin_del, "delete": _admin_del, "删除": _admin_del, "取消": _admin_del,
    "on": _admin_on, "启用": _admin_on, "恢复": _admin_on,
    "off": _admin_off, "停用": _admin_off, "暂停": _admin_off,
    "cat": _admin_cat, "类别": _admin_cat, "提醒": _admin_cat,
    "mode": _admin_mode, "模式": _admin_mode,
    "at": _admin_at, "atall": _admin_at,
    "atlist": _admin_atlist,
    "tpl": _admin_tpl, "template": _admin_tpl, "模板": _admin_tpl,
    "push": _admin_push, "推送": _admin_push,
    "pushhere": _admin_pushhere, "推送到此": _admin_pushhere,
    "unpush": _admin_unpush,
    "test": _admin_test, "测试": _admin_test,
    "check": _admin_check, "检查": _admin_check,
    "sync": _admin_sync, "刷新": _admin_sync,
}


# ---------------------------------------------------------------- 查询文本
async def _info_text(runtime: _Runtime, sub: Subscription) -> str:
    lines = [
        f"{sub.display_name}（UID {sub.uid}）",
        f"状态：{'监控中' if sub.enabled else '已暂停'}",
        f"直播间：{sub.room_id or '未获取'}",
        f"提醒类别：{'、'.join(CATEGORY_LABELS.get(key, key) for key in sorted(sub.category_set)) or '无'}",
        f"发送模式：{sub.send_mode or '跟随全局（' + str(runtime.config.send_mode) + '）'}",
        f"@全体：{'是' if sub.at_all else '否'} | @指定：{', '.join(sub.at_ids) or '无'}",
        f"推送目标：{_targets_text(sub)}",
    ]
    dynamic_cursor = await runtime.store.get_cursor(sub.id, "dynamic")
    live_cursor = await runtime.store.get_cursor(sub.id, "live")
    follower_cursor = await runtime.store.get_cursor(sub.id, "follower")
    if dynamic_cursor is not None and dynamic_cursor.value:
        lines.append(f"最近动态 ID：{dynamic_cursor.value}")
    if live_cursor is not None:
        extra = live_cursor.extra_map
        status = "直播中" if int(extra.get("status") or 0) == 1 else "未开播"
        peak = extra.get("peak") or 0
        lines.append(f"直播状态：{status}" + (f"（本轮最高人气 {format_count(int(peak))}）" if peak else ""))
    if follower_cursor is not None and follower_cursor.value:
        lines.append(f"粉丝基线：{format_count(int(follower_cursor.value or 0))}")
    if sub.template_map:
        lines.append("自定义文案：" + "、".join(
            CATEGORY_LABELS.get(key, key) for key in sub.template_map
        ))
    return "\n".join(lines)


async def _status_text(runtime: _Runtime) -> str:
    subs = await runtime.store.list_subscriptions()
    enabled = [sub for sub in subs if sub.enabled]
    stats = runtime.engine.snapshot()
    api_stats = dict(runtime.api.stats)
    uptime = format_duration(int(time.time() - runtime.started_at))
    login = "已登录" if runtime.api.has_login else "匿名"
    last_push = stats.get("last_push_at") or 0
    last_push_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_push)) if last_push else "还没有推送"
    lines = [
        "B 站监控运行状态",
        f"插件版本：{VERSION} | 已运行 {uptime} | B 站账号：{login}",
        f"订阅：{len(subs)} 个（启用 {len(enabled)} 个）",
        f"轮询：动态 {runtime.config.dynamic_interval_seconds}s / "
        f"直播 {runtime.config.live_interval_seconds}s / "
        f"粉丝 {runtime.config.follower_interval_seconds}s",
        f"本轮统计：检查 {stats.get('cycles', 0)} 轮，事件 {stats.get('events', 0)} 条，"
        f"成功推送 {stats.get('pushed', 0)} 次，失败 {stats.get('failed', 0)} 次",
        f"最近推送：{last_push_text}",
        f"接口请求：{api_stats.get('requests', 0)} 次，风控命中 {api_stats.get('risk', 0)} 次",
    ]
    if stats.get("last_error"):
        lines.append(f"最近错误：{stats['last_error']}")
    if not enabled:
        lines.append("提示：当前没有启用的订阅，用 /b站管理 add <uid> 添加。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 扫码登录
async def _start_qr_login(runtime: _Runtime, command_ctx: Any) -> str:
    try:
        session, image_url = await runtime.api.qr_generate(runtime.config.qr_image_service)
    except BiliApiError as exc:
        return f"生成二维码失败：{exc}"

    kind, conv_id = _conv_kind(command_ctx), _conv_id(command_ctx)
    chain = MessageChain().text("请用 B 站手机客户端扫码登录（3 分钟内有效）。")
    if image_url:
        chain.text("\n").image(url=image_url)
    else:
        chain.text(f"\n二维码内容：{session.url}")
    try:
        await _send_payload(runtime.ctx, kind, conv_id, chain)
    except Exception as exc:  # noqa: BLE001 - 发图失败退回纯文本
        _log("warning", f"二维码图片发送失败，改为纯文本：{exc}")
        await _send_payload(runtime.ctx, kind, conv_id, f"请用 B 站客户端打开并扫描：{session.url}")

    runtime.ctx.create_task(_poll_qr_login(runtime, session, kind, conv_id), name="bili-qr-login")
    return "二维码已发送，登录成功后我会在这里回复。"


async def _poll_qr_login(runtime: _Runtime, session: Any, kind: str, conv_id: str) -> None:
    deadline = time.time() + 180
    last_state = ""
    while time.time() < deadline:
        await asyncio.sleep(2.5)
        try:
            state, cookies = await runtime.api.qr_poll(session)
        except BiliApiError as exc:
            _log("warning", f"扫码轮询失败：{exc}")
            continue
        if state == "ok":
            name = await runtime.api.check_login()
            await _send_payload(
                runtime.ctx, kind, conv_id,
                f"B 站登录成功：{name}。已保存凭证，动态接口更稳定。",
            )
            return
        if state == "expired":
            await _send_payload(runtime.ctx, kind, conv_id, "二维码已失效，请重新发送 /b站登录 qr。")
            return
        if state in {"scanned", "waiting"} and state != last_state:
            last_state = state
            if state == "scanned":
                await _send_payload(runtime.ctx, kind, conv_id, "已扫码，请在手机上确认登录。")
    await _send_payload(runtime.ctx, kind, conv_id, "扫码登录超时，请重新发送 /b站登录 qr。")


# ---------------------------------------------------------------- 生命周期
@plugin.on_load
async def _on_load(ctx: Any) -> None:
    global _RUNTIME
    config = _validated_config(BiliMonitorConfig.model_validate(dict(ctx.config or {})))
    store = BiliStore(database)
    cookie_path = Path(ctx.data_dir) / "cookie.json"

    holder: dict[str, _Runtime] = {}

    def _load_cookie() -> str:
        runtime = holder.get("rt")
        return runtime.load_cookie() if runtime is not None else ""

    def _save_cookie(raw: str) -> None:
        runtime = holder.get("rt")
        if runtime is not None:
            runtime.save_cookie(raw)

    api = BiliClient(
        cookie=config.cookie,
        user_agent=config.user_agent,
        proxy=config.proxy,
        timeout=float(config.request_timeout_seconds),
        logger=ctx.logger,
        cookie_provider=lambda: asyncio.sleep(0, result=_load_cookie()),
        cookie_saver=lambda raw: asyncio.sleep(0, result=_save_cookie(raw)),
    )
    sender = HostSender(ctx, ctx.logger)
    cards = HtmlCardRenderer(ctx, ctx.logger)
    engine = MonitorEngine(
        api=api, store=store, sender=sender, cards=cards, config=config, logger=ctx.logger,
    )
    runtime = _Runtime(
        ctx=ctx, config=config, store=store, api=api, sender=sender, cards=cards,
        engine=engine, cookie_path=cookie_path,
    )
    holder["rt"] = runtime
    _RUNTIME = runtime

    await api.start()
    register_commands(ctx)
    sub_count = await store.count_subscriptions()
    ctx.logger.info(f"B 站监控插件已加载：{sub_count} 个订阅，登录态={'有' if api.has_login else '无'}")
    if sub_count and not cards.available:
        ctx.logger.info("本体截图能力不可用，card 模式会自动降级为文本+原图")


def _validated_config(config: BiliMonitorConfig) -> BiliMonitorConfig:
    """兜底：模型校验失败时退回默认值，避免整插件进入 ERROR。"""
    return config


@plugin.on_startup
async def _on_startup(ctx: Any) -> None:
    runtime = _RUNTIME
    if runtime is None:
        return
    try:
        await runtime.store.prune_events(runtime.config.event_retention_days)
    except Exception as exc:  # noqa: BLE001 - 清理失败不影响启动
        ctx.logger.warning(f"清理历史事件失败：{exc}")
    await runtime.engine.start()
    ctx.create_task(_daily_maintenance(runtime), name="bili-maintenance")


async def _daily_maintenance(runtime: _Runtime) -> None:
    while True:
        await asyncio.sleep(24 * 3600)
        try:
            await runtime.store.prune_events(runtime.config.event_retention_days)
        except Exception as exc:  # noqa: BLE001
            _log("warning", f"定时清理事件失败：{exc}")


@plugin.on_shutdown
async def _on_shutdown(ctx: Any) -> None:
    global _RUNTIME
    runtime = _RUNTIME
    if runtime is None:
        return
    try:
        await runtime.engine.stop()
    finally:
        await runtime.api.close()
        _RUNTIME = None


# ---------------------------------------------------------------- Agent 工具
@plugin.tool("bili_list", description="列出当前正在监控的 B 站 UP 主")
async def _tool_list() -> str:
    runtime = _RUNTIME
    if runtime is None:
        return "B 站监控插件尚未加载。"
    return await _list_text(runtime)


@plugin.tool("bili_up_info", description="查询某个 B 站 UP 主的昵称、粉丝数与直播间号")
async def _tool_up_info(uid: str) -> str:
    runtime = _RUNTIME
    if runtime is None:
        return "B 站监控插件尚未加载。"
    parsed = parse_uid(uid)
    if parsed is None:
        return "UID 不合法，请传数字 UID 或空间链接。"
    try:
        profile = await runtime.api.resolve_profile(parsed)
    except BiliApiError as exc:
        return f"查询失败：{exc}"
    if not profile.name:
        return f"没有找到 UID {parsed}。"
    return (
        f"{profile.name}（UID {parsed}）\n"
        f"粉丝：{format_count(profile.follower or 0)}\n"
        f"直播间：{profile.room_id or '无'}\n"
        f"签名：{profile.sign or '无'}"
    )


@plugin.tool("bili_latest", description="查看某个 B 站 UP 主最新的几条动态标题")
async def _tool_latest(uid: str, limit: int = 3) -> str:
    runtime = _RUNTIME
    if runtime is None:
        return "B 站监控插件尚未加载。"
    parsed = parse_uid(uid)
    if parsed is None:
        return "UID 不合法，请传数字 UID 或空间链接。"
    try:
        items = await runtime.api.fetch_dynamic_feed(parsed)
    except BiliApiError as exc:
        return f"获取动态失败：{exc}"
    from .bili_model import parse_dynamic_item

    lines: list[str] = []
    for item in items:
        event = parse_dynamic_item(item)
        if event is None:
            continue
        head = event.title or event.text[:40] or event.kind
        lines.append(f"- [{event.uname}] {head}")
        if len(lines) >= max(1, min(int(limit or 3), 10)):
            break
    return "\n".join(lines) or "没有取到动态（可能没有内容或被风控）。"


# ---------------------------------------------------------------- 使用说明
plugin.guide(
    "first-use",
    title="从零配置 B 站监控",
    steps=[
        {
            "target": "config",
            "form": "start",
            "chapter": "打开配置",
            "text": "在插件管理里选择本插件并打开配置表单。下面演示的是公开默认值，不会修改你的真实配置。",
            "example": {"dynamic_interval_seconds": 120, "live_interval_seconds": 90},
        },
        {
            "target": "config.cookie",
            "form": "edit",
            "chapter": "填写登录 Cookie（可选）",
            "text": "填 SESSDATA=...; bili_jct=... 可以显著降低被风控的概率；留空则匿名访问。也可以稍后用 /b站登录 qr 扫码获取。",
            "example": {"cookie": "SESSDATA=example; bili_jct=example"},
        },
        {
            "target": "config",
            "form": "save",
            "chapter": "保存配置",
            "text": "保存后插件会重载一次，新的轮询间隔立即生效。",
            "example": {"dynamic_interval_seconds": 120, "live_interval_seconds": 90},
        },
        {
            "target": "config",
            "chapter": "添加第一个订阅",
            "text": "在群里 @机器人 发送 /b站管理 add <UID>，机器人会把提醒推送到当前群。UID 可以从 space.bilibili.com/123456 里取。",
            "command": {"text": "/b站管理 add 2", "context": "在群聊中 @机器人 发送", "reply": "已开始监控 …（UID 2）"},
        },
        {
            "target": "config",
            "chapter": "查看订阅与状态",
            "text": "用 /b站监控 list 看列表，/b站监控 status 看轮询与推送统计。",
            "command": {"text": "/b站监控 list", "context": "在群聊中 @机器人 发送", "reply": "当前监控 1 个 UP 主：…"},
        },
    ],
    demo={"config": {"dynamic_interval_seconds": 120, "live_interval_seconds": 90}},
)
