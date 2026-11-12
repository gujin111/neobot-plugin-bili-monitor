"""提醒文案与卡片渲染。

文本模板支持 ``{name}`` / ``{title}`` / ``{text}`` / ``{url}`` / ``{time}`` 等占位符，
缺字段会被替换成空串而不是抛异常（旧版插件的自定义文案因此可以直接迁移）。
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from neobot_modloader import MessageChain

from .bili_model import MonitorEvent, format_count, html_escape

#: 默认模板，占位符与旧版「bili 监控助手」的提醒字段保持一致
DEFAULT_TEMPLATES: dict[str, str] = {
    "dynamic_new": "【动态提醒】\n{name} 发布了新动态\n\n{text}\n\n{url}",
    "dynamic_repost": "【转发动态】\n{name} 转发了动态\n\n{text}\n\n{url}",
    "dynamic_video": "【新视频】\n{name} 投稿了新视频\n{title}\n时长 {duration} · 播放 {play_text}\n{url}",
    "dynamic_article": "【新专栏】\n{name} 发布了新专栏\n{title}\n\n{text}\n\n{url}",
    "dynamic_audio": "【新音频】\n{name} 发布了新音频\n{title}\n{url}",
    "live_on": "【开播提醒】\n{name} 开播啦~\n直播标题：{title}\n分区：{area}\n{url}",
    "live_off": "【下播提醒】\n{name} 下播啦！\n直播标题：{title}\n时长：{duration_text}\n最高人气：{peak_text}\n{url}",
    "follower": "【粉丝数提醒】\n恭喜 {name} 粉丝数突破 {milestone_text}！当前 {follower_text} 粉丝",
    "test": "【测试提醒】\n{name} 的提醒链路正常。\n{url}",
}

TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "dynamic_new": ("name", "uid", "text", "title", "url", "time"),
    "dynamic_repost": ("name", "uid", "text", "title", "url", "time", "orig_author", "orig_text"),
    "dynamic_video": ("name", "uid", "title", "text", "url", "duration", "play_text", "bvid", "time"),
    "dynamic_article": ("name", "uid", "title", "text", "url", "time"),
    "dynamic_audio": ("name", "uid", "title", "text", "url", "time"),
    "live_on": ("name", "uid", "title", "url", "area", "online_text", "room_id", "time"),
    "live_off": ("name", "uid", "title", "url", "duration_text", "peak_text", "room_id", "time"),
    "follower": ("name", "uid", "follower_text", "milestone_text", "url", "time"),
    "test": ("name", "uid", "url", "time"),
}


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # noqa: D105
        return ""


def template_for(event: MonitorEvent, custom: Mapping[str, str] | None = None) -> str:
    if custom:
        candidate = custom.get(event.kind)
        if candidate:
            return candidate
    return DEFAULT_TEMPLATES.get(event.kind, DEFAULT_TEMPLATES["dynamic_new"])


def build_fields(event: MonitorEvent) -> dict[str, str]:
    """把事件摊平成模板占位符；同时给出 ``*_text`` 的人性化数字。"""
    fields: dict[str, str] = {
        "name": event.uname or str(event.uid),
        "uid": str(event.uid),
        "title": event.title,
        "text": event.text,
        "url": event.url,
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


def render_text(event: MonitorEvent, *, custom: Mapping[str, str] | None = None, include_link: bool = True) -> str:
    template = template_for(event, custom)
    text = template.format_map(_SafeDict(build_fields(event)))
    # 聊天里没法直接输入换行，允许用户写 \n 表示换行（旧版自定义文案也是这么写的）
    text = text.replace("\\n", "\n")
    if not include_link:
        # 去掉模板里单独占一行的链接
        lines = [line for line in text.split("\n") if line.strip() != event.url]
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
) -> MessageChain:
    """组装一条提醒消息链：@ -> 文本 -> 配图。"""
    chain = MessageChain()
    if at_all:
        chain.at("all")
        chain.text("\n")
    for ident in at_ids:
        chain.at(str(ident))
        chain.text(" ")
    chain.text(render_text(event, custom=custom, include_link=include_link))
    if with_images:
        for url in event.images[:max_images]:
            chain.text("\n").image(url=url)
    return chain


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
    link_html = f'<div class="link">{link}</div>' if link else ""

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
  .foot {{ padding: 0 20px 18px; display: flex; justify-content: space-between; align-items: center;
           font-size: 12px; color: #98a2b3; }}
  .link {{ color: #00a1d6; max-width: 78%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
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
  </div>
  <div class="foot"><span>{when}</span>{link_html}</div>
</div></body></html>"""


def summary_line(event: MonitorEvent) -> str:
    """给 /bili status 之类的一行摘要。"""
    label = _KIND_BADGE.get(event.kind, ("", "#000", event.kind))[2]
    title = event.title or (event.text[:24] if event.text else "")
    return f"{label} {event.uname} {title}".strip()


def describe_template_fields(kind: str) -> str:
    fields = TEMPLATE_FIELDS.get(kind, ())
    return "、".join("{" + name + "}" for name in fields)


__all__ = [
    "DEFAULT_TEMPLATES",
    "TEMPLATE_FIELDS",
    "build_chain",
    "build_fields",
    "card_html",
    "describe_template_fields",
    "render_text",
    "summary_line",
    "template_for",
]


def _unused(_: Any) -> None:  # pragma: no cover
    return None
