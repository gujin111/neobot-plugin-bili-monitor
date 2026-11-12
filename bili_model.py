"""把 B 站原始 JSON 归一化成可推送的监控事件（纯函数，便于单测）。

分类规则与旧版「bili 监控助手」一致：动态流里的每一条按 ``type`` /
``major.type`` 归到 发布新动态 / 转发动态 / 新视频 / 新专栏 / 新音频，
直播与粉丝里程碑由各自的轮询任务产生。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

DYNAMIC_KINDS = ("dynamic_new", "dynamic_repost", "dynamic_video", "dynamic_article", "dynamic_audio")
LIVE_KINDS = ("live_on", "live_off")
FOLLOWER_KINDS = ("follower",)

#: 旧版插件把「置顶」动态当作历史内容跳过，这里沿用
PINNED_TAGS = frozenset({"置顶", "Pinned", "pinned"})

_WS_RE = re.compile(r"[ \t\u3000]+")


@dataclass(slots=True)
class MonitorEvent:
    """一条待推送的监控事件。"""

    kind: str
    uid: int
    uname: str
    event_key: str
    url: str = ""
    title: str = ""
    text: str = ""
    face: str = ""
    images: tuple[str, ...] = ()
    published_at: int = 0
    cover: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.uid}:{self.event_key}"


# ---------------------------------------------------------------- 动态解析
_COUNT_RE = re.compile(r"([\d.]+)\s*(亿|万)?")


def parse_count_value(value: Any) -> int:
    """B 站的播放量/人气经常是 ``"237.2万"`` 这种字符串，统一换成整数。"""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").replace(",", "").replace(" ", "").strip()
    if not text:
        return 0
    match = _COUNT_RE.match(text)
    if not match:
        return 0
    try:
        number = float(match.group(1))
    except ValueError:  # pragma: no cover - 正则已保证可转
        return 0
    unit = match.group(2)
    multiplier = 100_000_000 if unit == "亿" else 10_000 if unit == "万" else 1
    return int(number * multiplier)


def is_pinned(item: dict[str, Any]) -> bool:
    tag = ((item.get("modules") or {}).get("module_tag") or {}).get("text")
    return str(tag or "") in PINNED_TAGS


def dynamic_id(item: dict[str, Any]) -> int:
    try:
        return int(str(item.get("id_str") or "0"))
    except (TypeError, ValueError):
        return 0


def _author(item: dict[str, Any]) -> dict[str, Any]:
    return dict(((item.get("modules") or {}).get("module_author")) or {})


def _dynamic_module(item: dict[str, Any]) -> dict[str, Any]:
    return dict(((item.get("modules") or {}).get("module_dynamic")) or {})


def _major(item: dict[str, Any]) -> dict[str, Any]:
    return dict(_dynamic_module(item).get("major") or {})


def normalize_text(value: Any, *, limit: int = 0) -> str:
    """压掉多余空白，按字符上限截断（中文按 1 个字符算）。"""
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if limit and len(text) > limit:
        text = text[: max(1, limit - 1)].rstrip() + "…"
    return text


def html_escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _pics(values: Iterable[Any]) -> tuple[str, ...]:
    out: list[str] = []
    for entry in values or ():
        if isinstance(entry, str):
            url = entry
        elif isinstance(entry, dict):
            url = str(entry.get("url") or entry.get("src") or "")
        else:
            continue
        url = _https(url)
        if url and url not in out:
            out.append(url)
    return tuple(out)


def _https(url: str) -> str:
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _dynamic_url(item: dict[str, Any]) -> str:
    ident = str(item.get("id_str") or "")
    if ident:
        return f"https://t.bilibili.com/{ident}"
    return ""


def parse_dynamic_item(item: dict[str, Any], *, text_limit: int = 180) -> MonitorEvent | None:
    """把一条动态解析成事件；无法识别或空内容返回 None。"""
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "")
    if item_type in {"DYNAMIC_TYPE_NONE", "DYNAMIC_TYPE_BLOCKED"}:
        return None
    author = _author(item)
    dynamic = _dynamic_module(item)
    major = _major(item)
    major_type = str(major.get("type") or "")

    uid = int(author.get("mid") or 0)
    uname = str(author.get("name") or "")
    face = _https(str(author.get("face") or ""))
    published_at = int(author.get("pub_ts") or 0)
    desc_text = normalize_text((dynamic.get("desc") or {}).get("text"), limit=text_limit)
    ident = str(item.get("id_str") or "")

    kind = "dynamic_new"
    title = ""
    text = desc_text
    images: tuple[str, ...] = ()
    cover = ""
    url = _dynamic_url(item)
    extra: dict[str, Any] = {"dynamic_id": ident, "dynamic_type": item_type}

    if item_type == "DYNAMIC_TYPE_FORWARD":
        kind = "dynamic_repost"
        orig = item.get("orig") or {}
        if isinstance(orig, dict) and orig.get("type") not in (None, "", "DYNAMIC_TYPE_NONE"):
            orig_author = _author(orig)
            orig_major = _major(orig)
            orig_text = normalize_text((_dynamic_module(orig).get("desc") or {}).get("text"), limit=text_limit)
            extra["orig_author"] = str(orig_author.get("name") or "")
            extra["orig_text"] = orig_text
            orig_archive = orig_major.get("archive") or {}
            if isinstance(orig_archive, dict) and orig_archive.get("title"):
                extra["orig_title"] = str(orig_archive.get("title"))
            if not url:
                url = _dynamic_url(orig)
    elif major_type == "MAJOR_TYPE_ARCHIVE":
        kind = "dynamic_video"
        archive = major.get("archive") or {}
        title = normalize_text(archive.get("title"), limit=120)
        extra["bvid"] = str(archive.get("bvid") or "")
        extra["duration"] = str(archive.get("duration_text") or "")
        extra["play"] = parse_count_value((archive.get("stat") or {}).get("play"))
        cover = _https(str(archive.get("cover") or ""))
        images = (cover,) if cover else ()
        if extra["bvid"]:
            url = f"https://www.bilibili.com/video/{extra['bvid']}"
        text = desc_text
    elif major_type == "MAJOR_TYPE_ARTICLE":
        kind = "dynamic_article"
        article = major.get("article") or {}
        title = normalize_text(article.get("title"), limit=120)
        covers = _pics(article.get("covers") or ())
        images = covers
        cover = covers[0] if covers else ""
        article_id = str(article.get("id") or "")
        if article_id:
            url = f"https://www.bilibili.com/read/cv{article_id}"
        text = normalize_text(article.get("desc") or desc_text, limit=text_limit)
    elif major_type == "MAJOR_TYPE_MUSIC":
        kind = "dynamic_audio"
        music = major.get("music") or {}
        title = normalize_text(music.get("title"), limit=120)
        cover = _https(str(music.get("cover") or ""))
        images = (cover,) if cover else ()
        url = _https(str(music.get("jump_url") or "")) or url
    elif major_type == "MAJOR_TYPE_OPUS":
        opus = major.get("opus") or {}
        title = normalize_text(opus.get("title"), limit=120)
        text = normalize_text((opus.get("summary") or {}).get("text") or desc_text, limit=text_limit)
        images = _pics(opus.get("pics") or ())
        cover = images[0] if images else ""
        url = _https(str(opus.get("jump_url") or "")) or url
    elif major_type == "MAJOR_TYPE_DRAW":
        draw = major.get("draw") or {}
        images = _pics(draw.get("items") or ())
        cover = images[0] if images else ""
    elif major_type == "MAJOR_TYPE_LIVE_RCMD":
        # 动态流里的直播推荐，交给直播轮询处理，避免重复推送
        return None
    elif major_type in {"MAJOR_TYPE_PGC", "MAJOR_TYPE_UGC_SEASON", "MAJOR_TYPE_MEDIALIST"}:
        kind = "dynamic_video"
        for key in ("pgc", "ugc_season", "medialist"):
            block = major.get(key) or {}
            if isinstance(block, dict) and block.get("title"):
                title = normalize_text(block.get("title"), limit=120)
                cover = _https(str(block.get("cover") or block.get("cover_left_icon") or ""))
                break
        images = (cover,) if cover else ()

    if not text and not title and not images:
        return None

    return MonitorEvent(
        kind=kind,
        uid=uid,
        uname=uname,
        event_key=f"dyn:{ident}",
        url=url,
        title=title,
        text=text,
        face=face,
        images=images[:9],
        published_at=published_at,
        cover=cover,
        extra=extra,
    )


# ---------------------------------------------------------------- 直播与粉丝
def format_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


def format_count(value: int) -> str:
    """12345 -> 1.2万，用于人气/播放数展示。"""
    value = int(value or 0)
    if value >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if value >= 10_000:
        return f"{value / 10_000:.1f}万"
    return str(value)


def build_live_on(
    *, uid: int, uname: str, face: str, room_id: int, title: str, cover: str,
    area: str, online: int, start_ts: int,
) -> MonitorEvent:
    key = f"live:{start_ts}:on"
    return MonitorEvent(
        kind="live_on",
        uid=uid,
        uname=uname,
        face=face,
        event_key=key,
        url=f"https://live.bilibili.com/{room_id}",
        title=normalize_text(title, limit=120),
        cover=_https(cover),
        images=(_https(cover),) if cover else (),
        published_at=int(start_ts or 0),
        extra={
            "room_id": room_id,
            "area": area,
            "online": int(online or 0),
            "live_time": int(start_ts or 0),
        },
    )


def build_live_off(
    *, uid: int, uname: str, face: str, room_id: int, title: str, cover: str,
    start_ts: int, end_ts: int, peak_online: int,
) -> MonitorEvent:
    duration = max(0, int(end_ts) - int(start_ts)) if start_ts else 0
    return MonitorEvent(
        kind="live_off",
        uid=uid,
        uname=uname,
        face=face,
        event_key=f"live:{start_ts}:off",
        url=f"https://live.bilibili.com/{room_id}",
        title=normalize_text(title, limit=120),
        cover=_https(cover),
        images=(_https(cover),) if cover else (),
        published_at=int(end_ts or 0),
        extra={
            "room_id": room_id,
            "duration": duration,
            "duration_text": format_duration(duration),
            "peak_online": int(peak_online or 0),
            "start_time": int(start_ts or 0),
            "end_time": int(end_ts or 0),
        },
    )


def build_follower_event(
    *, uid: int, uname: str, face: str, follower: int, milestone: int, space_url: str = "",
) -> MonitorEvent:
    return MonitorEvent(
        kind="follower",
        uid=uid,
        uname=uname,
        face=face,
        event_key=f"fans:{milestone}",
        url=space_url or f"https://space.bilibili.com/{uid}",
        extra={"follower": int(follower), "milestone": int(milestone)},
    )


def crossed_milestone(previous: int, current: int, step: int) -> int:
    """返回本次跨越的最高里程碑（含 step 倍数）；没跨越返回 0。"""
    step = max(1, int(step or 1000))
    if current <= 0:
        return 0
    current_level = current // step
    previous_level = max(0, int(previous or 0)) // step
    if current_level <= previous_level:
        return 0
    return current_level * step
