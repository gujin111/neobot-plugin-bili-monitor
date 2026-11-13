"""提醒文案与卡片渲染。

文本模板支持 ``{name}`` / ``{title}`` / ``{text}`` / ``{url}`` / ``{time}`` 等占位符，
缺字段会被替换成空串而不是抛异常（旧版插件的自定义文案因此可以直接迁移）。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from neobot_modloader import MessageChain

from .bili_model import MonitorEvent, format_count, html_escape

#: 链接按钮文案：卡片里画成按钮，文本里作为「按钮名：链接」一行
LINK_LABELS: dict[str, str] = {
    "live_on": "点击进入直播间",
    "live_off": "进入直播间",
    "dynamic_video": "观看视频",
    "dynamic_article": "阅读专栏",
    "dynamic_audio": "收听音频",
    "dynamic_new": "查看动态",
    "dynamic_repost": "查看原动态",
    "follower": "进入空间",
    "test": "查看空间",
}

#: 默认模板，占位符与旧版「bili 监控助手」的提醒字段保持一致
DEFAULT_TEMPLATES: dict[str, str] = {
    "dynamic_new": "【动态提醒】\n{name} 发布了新动态\n\n{text}\n\n{link}",
    "dynamic_repost": "【转发动态】\n{name} 转发了动态\n\n{text}\n\n{link}",
    "dynamic_video": "【新视频】\n{name} 投稿了新视频\n{title}\n时长 {duration} · 播放 {play_text}\n{link}",
    "dynamic_article": "【新专栏】\n{name} 发布了新专栏\n{title}\n\n{text}\n\n{link}",
    "dynamic_audio": "【新音频】\n{name} 发布了新音频\n{title}\n{link}",
    "live_on": "【开播提醒】\n{name} 开播啦~\n直播标题：{title}\n分区：{area}\n▶ {link}",
    "live_off": "【下播提醒】\n{name} 下播啦！\n直播标题：{title}\n时长：{duration_text}\n最高人气：{peak_text}\n▶ {link}",
    "follower": "【粉丝数提醒】\n恭喜 {name} 粉丝数突破 {milestone_text}！当前 {follower_text} 粉丝",
    "test": "【测试提醒】\n{name} 的提醒链路正常。\n▶ {link}",
}

TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "dynamic_new": ("name", "uid", "text", "title", "url", "link", "time"),
    "dynamic_repost": ("name", "uid", "text", "title", "url", "link", "time", "orig_author", "orig_text"),
    "dynamic_video": ("name", "uid", "title", "text", "url", "link", "duration", "play_text", "bvid", "time"),
    "dynamic_article": ("name", "uid", "title", "text", "url", "link", "time"),
    "dynamic_audio": ("name", "uid", "title", "text", "url", "link", "time"),
    "live_on": ("name", "uid", "title", "url", "link", "link_label", "area", "online_text", "room_id", "time"),
    "live_off": ("name", "uid", "title", "url", "link", "link_label", "duration_text", "peak_text", "room_id", "time"),
    "follower": ("name", "uid", "follower_text", "milestone_text", "url", "link", "time"),
    "test": ("name", "uid", "url", "link", "time"),
}


def link_label(kind: str) -> str:
    return LINK_LABELS.get(kind, "查看原文")


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def template_for(event: MonitorEvent, custom: Mapping[str, str] | None = None) -> str:
    if custom:
        candidate = custom.get(event.kind)
        if candidate:
            return candidate
    return DEFAULT_TEMPLATES.get(event.kind, DEFAULT_TEMPLATES["dynamic_new"])


def build_fields(event: MonitorEvent, *, link_style: str = "url") -> dict[str, str]:
    """把事件摊平成模板占位符；同时给出 ``*_text`` 的人性化数字。

    ``link_style`` 决定 ``{link}`` 的写法：
    - ``url``：``点击进入直播间：https://…``（所有通道都认，客户端会自动识别网址）
    - ``markdown``：``[点击进入直播间](https://…)``（QQ 官方通道按 markdown 发时可点）
    """
    label = link_label(event.kind)
    if not event.url:
        link_text = ""
    elif str(link_style).lower() == "markdown":
        link_text = f"[{label}]({event.url})"
    else:
        link_text = f"{label}：{event.url}"
    fields: dict[str, str] = {
        "name": event.uname or str(event.uid),
        "uid": str(event.uid),
        "title": event.title,
        "text": event.text,
        "url": event.url,
        "link": link_text,
        "link_label": label,
        "time": _format_time(event.published_at),
        "date": _format_time(event.published_at, with_time=False),
        "face": event.face,
        "kind": event.kind,
    }
    for key, value in event.extra.items():
        if isinstance(value, (str, int, float)):
            fields[str(key)] = str(value)
    if "play" in fields:
        fields["play_text"] = format_count(int(fields["play"] or 0))
    if "online" in fields:
        fields["online_text"] = format_count(int(fields["online"] or 0))
    if "peak_online" in fields:
        fields["peak_text"] = format_count(int(fields["peak_online"] or 0))
    if "follower" in fields:
        fields["follower_text"] = format_count(int(fields["follower"] or 0))
    if "milestone" in fields:
        fields["milestone_text"] = format_count(int(fields["milestone"] or 0))
    fields.setdefault("duration", "")
    fields.setdefault("duration_text", "")
    fields.setdefault("area", "")
    return fields


def render_text(
    event: MonitorEvent,
    *,
    custom: Mapping[str, str] | None = None,
    include_link: bool = True,
    link_style: str = "url",
) -> str:
    template = template_for(event, custom)
    text = template.format_map(_SafeDict(build_fields(event, link_style=link_style)))
    # 聊天里没法直接输入换行，允许用户写 \n 表示换行（旧版自定义文案也是这么写的）
    text = text.replace("\\n", "\n")
    if not include_link:
        # 去掉带链接的那一行（{url} / {link} 的两种写法都会命中）
        lines = [
            line
            for line in text.split("\n")
            if line.strip() != event.url and (not event.url or event.url not in line)
        ]
        text = "\n".join(lines)
    return text.strip()


def build_chain(
    event: MonitorEvent,
    *,
    custom: Mapping[str, str] | None = None,
    include_link: bool = True,
    at_all: bool = False,
    at_ids: tuple[str, ...] = (),
    with_images: bool = False,
    max_images: int = 3,
    link_style: str = "url",
) -> MessageChain:
    """组装一条提醒消息链：@ -> 文本 -> 配图。"""
    chain = MessageChain()
    if at_all:
        chain.at("all")
        chain.text("\n")
    for ident in at_ids:
        chain.at(str(ident))
        chain.text(" ")
    chain.text(render_text(event, custom=custom, include_link=include_link, link_style=link_style))
    if with_images:
        for url in event.images[:max_images]:
            chain.text("\n").image(url=url)
    return chain


def link_chain(event: MonitorEvent, *, link_style: str = "url") -> MessageChain:
    """单独一条「▶ 按钮名：链接」的文本消息。

    卡片是 PNG，图里的按钮点不了；补这条文本，QQ 客户端会把网址识别成可点链接，
    真正做到「点击进入直播间」。官方通道用 markdown 链接更接近「按钮」观感。
    """
    if not event.url:
        return MessageChain()
    label = link_label(event.kind)
    if str(link_style).lower() == "markdown":
        return MessageChain().text(f"▶ [{label}]({event.url})")
    return MessageChain().text(f"▶ {label}：{event.url}")


def _format_time(ts: int, *, with_time: bool = True) -> str:
    if not ts:
        return ""
    try:
        stamp = time.localtime(int(ts))
    except (OverflowError, OSError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d", stamp)


# ---------------------------------------------------------------- 卡片
_KIND_BADGE = {
    "dynamic_new": ("📝", "#00A1D6", "新动态"),
    "dynamic_repost": ("🔁", "#7C4DFF", "转发动态"),
    "dynamic_video": ("🎬", "#FB7299", "新视频"),
    "dynamic_article": ("📄", "#00A1D6", "新专栏"),
    "dynamic_audio": ("🎧", "#FF8A65", "新音频"),
    "live_on": ("🔴", "#F44336", "正在直播"),
    "live_off": ("🌙", "#607D8B", "直播结束"),
    "follower": ("🎉", "#FFB300", "粉丝里程碑"),
    "test": ("🧪", "#00A1D6", "测试"),
}


def card_html(event: MonitorEvent, *, custom: Mapping[str, str] | None = None,
              include_link: bool = True) -> str:
    """自包含 HTML 卡片：无脚本、无外链样式，交给本体截图端口渲染。"""
    icon, color, label = _KIND_BADGE.get(event.kind, ("🔔", "#00A1D6", "提醒"))
    fields = build_fields(event)
    name = html_escape(event.uname or str(event.uid))
    avatar = html_escape(event.face)
    title = html_escape(event.title) if event.title else ""
    body = html_escape(render_text(event, custom=custom, include_link=False))
    link = html_escape(event.url) if (include_link and event.url) else ""
    cover = html_escape(event.cover or (event.images[0] if event.images else ""))
    when = html_escape(fields.get("time", ""))
    extra_bits: list[str] = []
    if event.kind == "live_off":
        extra_bits.append(f"直播时长 {html_escape(fields.get('duration_text', ''))}")
        extra_bits.append(f"最高人气 {html_escape(fields.get('peak_text', ''))}")
    elif event.kind == "live_on":
        if fields.get("area"):
            extra_bits.append(f"分区 {html_escape(fields['area'])}")
        if fields.get("online_text"):
            extra_bits.append(f"人气 {html_escape(fields['online_text'])}")
    elif event.kind == "dynamic_video":
        if fields.get("duration"):
            extra_bits.append(f"时长 {html_escape(fields['duration'])}")
        if fields.get("play_text"):
            extra_bits.append(f"播放 {html_escape(fields['play_text'])}")
    elif event.kind == "follower":
        extra_bits.append(f"当前粉丝 {html_escape(fields.get('follower_text', ''))}")

    meta_html = "".join(f'<span class="chip">{bit}</span>' for bit in extra_bits)
    cover_html = f'<img class="cover" src="{cover}" alt="" />' if cover else ""
    title_html = f'<div class="title">{title}</div>' if title else ""
    avatar_html = (
        f'<img class="avatar" src="{avatar}" alt="" />' if avatar else '<div class="avatar ph"></div>'
    )
    button_label = html_escape(fields.get("link_label") or "查看原文")
    link_html = (
        '<div class="actions">'
        f'<a class="btn" href="{link}">▶ {button_label}</a>'
        f'<span class="url">{link}</span>'
        "</div>"
        if link
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ width: 640px; background: #f4f6f9; font-family: "Microsoft YaHei", "PingFang SC", sans-serif; }}
  .card {{ margin: 12px; border-radius: 18px; overflow: hidden; background: #fff;
           box-shadow: 0 6px 24px rgba(16, 24, 40, .10); }}
  .head {{ display: flex; align-items: center; gap: 12px; padding: 18px 20px;
           background: linear-gradient(135deg, {color} 0%, {color}cc 100%); color: #fff; }}
  .avatar {{ width: 52px; height: 52px; border-radius: 50%; object-fit: cover;
             border: 2px solid rgba(255,255,255,.75); background: rgba(255,255,255,.25); }}
  .avatar.ph {{ display: block; }}
  .who {{ flex: 1; min-width: 0; }}
  .name {{ font-size: 19px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
  .sub {{ font-size: 12px; opacity: .88; margin-top: 3px; }}
  .badge {{ font-size: 13px; font-weight: 600; padding: 6px 12px; border-radius: 999px;
            background: rgba(255,255,255,.22); white-space: nowrap; }}
  .body {{ padding: 18px 20px 20px; }}
  .title {{ font-size: 17px; font-weight: 600; color: #16181c; margin-bottom: 10px; line-height: 1.45; }}
  .text {{ font-size: 15px; line-height: 1.65; color: #3b4252; white-space: pre-wrap; word-break: break-word; }}
  .cover {{ display: block; width: 100%; border-radius: 12px; margin-top: 14px; }}
  .chips {{ margin-top: 14px; display: flex; flex-wrap: wrap; gap: 8px; }}
  .chip {{ font-size: 12px; color: #5a6472; background: #eef1f6; border-radius: 8px; padding: 5px 10px; }}
  .actions {{ margin-top: 16px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
  .btn {{ display: inline-flex; align-items: center; gap: 6px; text-decoration: none;
          padding: 11px 22px; border-radius: 999px; font-size: 15px; font-weight: 600;
          color: #fff; background: linear-gradient(135deg, #00A1D6 0%, #00c3ff 100%);
          box-shadow: 0 4px 12px rgba(0, 161, 214, .30); white-space: nowrap; }}
  .url {{ font-size: 11.5px; color: #98a2b3; word-break: break-all; }}
  .foot {{ padding: 0 20px 18px; display: flex; justify-content: space-between; align-items: center;
           font-size: 12px; color: #98a2b3; }}
</style></head>
<body><div class="card">
  <div class="head">
    {avatar_html}
    <div class="who">
      <div class="name">{name}</div>
      <div class="sub">UID {event.uid}</div>
    </div>
    <div class="badge">{icon} {label}</div>
  </div>
  <div class="body">
    {title_html}
    <div class="text">{body}</div>
    {cover_html}
    {f'<div class="chips">{meta_html}</div>' if meta_html else ''}
    {link_html}
  </div>
  <div class="foot"><span>{when}</span><span>bili_monitor</span></div>
</div></body></html>"""


def summary_line(event: MonitorEvent) -> str:
    """给 /bili status 之类的一行摘要。"""
    label = _KIND_BADGE.get(event.kind, ("", "#000", event.kind))[2]
    title = event.title or (event.text[:24] if event.text else "")
    return f"{label} {event.uname} {title}".strip()


def describe_template_fields(kind: str) -> str:
    fields = TEMPLATE_FIELDS.get(kind, ())
    return "、".join("{" + name + "}" for name in fields)


# ---------------------------------------------------------------- 命令速查
#: (分组标题, 强调色, [(命令写法, 一句话说明), ...])
HELP_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "所有人 · 查看",
        "#00A1D6",
        (
            ("/b站监控", "订阅列表（默认）"),
            ("/b站监控 info <uid>", "单个 UP 的配置与游标"),
            ("/b站监控 status", "运行状态、推送统计、风控冷却"),
            ("/b站监控 help", "本图（命令速查）"),
        ),
    ),
    (
        "管理员 · 订阅与提醒",
        "#FB7299",
        (
            ("/b站管理 add <uid|链接> [直播间号]", "添加监控，推到当前会话"),
            ("/b站管理 del <uid>", "取消监控"),
            ("/b站管理 on|off <uid>", "恢复 / 暂停"),
            ("/b站管理 cat <uid> 动态,视频,开播", "提醒类别（all / none）"),
            ("/b站管理 mode <uid> auto|text|image|card", "发送模式"),
            ("/b站管理 tpl <uid> <类别> [模板|clear|?]", "自定义文案"),
            ("/b站管理 at <uid> on|off", "开播 @全体（需群管理）"),
            ("/b站管理 atlist <uid> <qq,qq|clear>", "@ 指定成员"),
        ),
    ),
    (
        "管理员 · 推送目标与排障",
        "#7C4DFF",
        (
            ("/b站管理 targets [uid]", "看推送目标 + 当前群号"),
            ("/b站管理 bind <uid>", "把当前会话加为推送目标"),
            ("/b站管理 push <uid> <群号>", "追加群；私聊写 私聊 <QQ>"),
            ("/b站管理 unpush <uid> <群号>", "移除推送目标"),
            ("/b站管理 test <uid>", "发一条测试提醒"),
            ("/b站管理 check <uid>", "立即检查一次"),
            ("/b站管理 sync <uid>", "刷新昵称 / 头像 / 直播间"),
        ),
    ),
    (
        "超级管理员 · 账号",
        "#FFB300",
        (
            ("/b站登录 qr", "扫码登录（降低风控）"),
            ("/b站登录 status", "查看登录状态"),
            ("/b站登录 logout", "清除本地凭证"),
        ),
    ),
)


def help_text() -> str:
    """纯文本命令表（截图不可用时的回退）。"""
    lines = ["B 站监控助手 · 命令速查", ""]
    for title, _color, items in HELP_GROUPS:
        lines.append(f"【{title}】")
        for syntax, desc in items:
            lines.append(f"  {syntax}")
            lines.append(f"      {desc}")
        lines.append("")
    lines.append("群聊里记得先 @机器人；命令前缀默认 /。")
    return "\n".join(lines).rstrip()


def help_markdown(*, version: str = "") -> str:
    """命令速查的 **Markdown 版**：官方机器人通道默认按 markdown 发，不碰媒体上传。

    QQ 官方 markdown 支持有限（加粗、换行、链接），所以只用 `**加粗**` + 换行排版，
    不依赖列表、表格这些不一定渲染的语法。
    """
    lines = ["**B 站监控助手 · 命令速查**"]
    if version:
        lines.append(f"v{version} · 群里先 @机器人，前缀默认 /")
    for title, _color, items in HELP_GROUPS:
        lines.append("")
        lines.append(f"**{title}**")
        for syntax, desc in items:
            lines.append(f"{syntax} — {desc}")
    return "\n".join(lines).rstrip()


def help_html(*, version: str = "") -> str:
    """命令速查卡片：自包含 HTML，交给本体截图端口渲染成图片。"""
    blocks: list[str] = []
    for title, color, items in HELP_GROUPS:
        rows = "".join(
            f'<div class="row"><code>{html_escape(syntax)}</code>'
            f'<span class="desc">{html_escape(desc)}</span></div>'
            for syntax, desc in items
        )
        blocks.append(
            f'<section class="group">'
            f'<h2 style="border-left-color:{color}">{html_escape(title)}</h2>'
            f"{rows}</section>"
        )
    version_text = f"v{html_escape(version)}" if version else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8" />
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ width: 760px; background: #f4f6f9;
          font-family: "Microsoft YaHei", "PingFang SC", sans-serif; color: #1f2430; }}
  .card {{ margin: 14px; background: #fff; border-radius: 18px; overflow: hidden;
           box-shadow: 0 6px 24px rgba(16, 24, 40, .10); }}
  .head {{ padding: 22px 24px; color: #fff;
           background: linear-gradient(135deg, #00A1D6 0%, #7C4DFF 100%); }}
  .head h1 {{ font-size: 22px; font-weight: 700; letter-spacing: .5px; }}
  .head p {{ font-size: 12.5px; opacity: .9; margin-top: 6px; }}
  .body {{ padding: 18px 22px 22px; }}
  .group + .group {{ margin-top: 16px; }}
  .group h2 {{ font-size: 15px; font-weight: 700; color: #16181c; padding-left: 10px;
               border-left: 4px solid #00A1D6; margin-bottom: 8px; }}
  .row {{ display: flex; align-items: baseline; gap: 12px; padding: 7px 10px;
          border-radius: 8px; }}
  .row:nth-child(odd) {{ background: #f7f9fc; }}
  code {{ flex: 0 0 auto; font-family: Consolas, "Courier New", monospace;
          font-size: 13px; color: #0b7285; background: #e8f4f8;
          border-radius: 6px; padding: 3px 8px; }}
  .desc {{ font-size: 13px; color: #5a6472; line-height: 1.5; }}
  .foot {{ padding: 0 24px 18px; font-size: 11.5px; color: #98a2b3;
           display: flex; justify-content: space-between; }}
</style></head>
<body><div class="card">
  <div class="head">
    <h1>B 站监控助手 · 命令速查</h1>
    <p>群里先 @机器人，命令前缀默认 / · 分三类权限：所有人 / 管理员 / 超级管理员</p>
  </div>
  <div class="body">{''.join(blocks)}</div>
  <div class="foot"><span>bili_monitor</span><span>{version_text}</span></div>
</div></body></html>"""


__all__ = [
    "DEFAULT_TEMPLATES",
    "HELP_GROUPS",
    "TEMPLATE_FIELDS",
    "build_chain",
    "build_fields",
    "card_html",
    "describe_template_fields",
    "help_html",
    "help_markdown",
    "help_text",
    "render_text",
    "summary_line",
    "template_for",
]


def _unused(_: Any) -> None:  # pragma: no cover
    return None
