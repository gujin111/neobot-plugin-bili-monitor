"""动态解析与分类：用真实接口字段形状构造样例。"""

from __future__ import annotations


def _author(mid=546195, name="老番茄", face="http://i0.hdslb.com/bfs/face/x.jpg", pub_ts=1793955619):
    return {"mid": mid, "name": name, "face": face, "pub_ts": pub_ts, "pub_time": "11-06"}


def _item(item_type="DYNAMIC_TYPE_AV", major=None, desc="", tag=None, ident="1267518804128170040"):
    return {
        "id_str": ident,
        "type": item_type,
        "modules": {
            "module_tag": {"text": tag} if tag else None,
            "module_author": _author(),
            "module_dynamic": {"desc": {"text": desc} if desc else None, "major": major},
            "module_stat": {"comment": {"count": 10}, "forward": {"count": 2}, "like": {"count": 100}},
        },
    }


def test_video_dynamic(model):
    item = _item(
        "DYNAMIC_TYPE_AV",
        {
            "type": "MAJOR_TYPE_ARCHIVE",
            "archive": {
                "bvid": "BV1iP4i68ETP",
                "title": "新的一集",
                "cover": "http://i2.hdslb.com/bfs/archive/a.jpg",
                "duration_text": "56:35",
                "stat": {"play": 2367000},
            },
        },
        desc="简介",
    )
    event = model.parse_dynamic_item(item)
    assert event is not None
    assert event.kind == "dynamic_video"
    assert event.title == "新的一集"
    assert event.url == "https://www.bilibili.com/video/BV1iP4i68ETP"
    assert event.images[0].startswith("https://")
    assert event.extra["duration"] == "56:35"
    assert event.extra["play"] == 2367000
    assert event.event_key == "dyn:1267518804128170040"
    assert event.uid == 546195


def test_opus_dynamic_uses_summary_and_pics(model):
    item = _item(
        "DYNAMIC_TYPE_DRAW",
        {
            "type": "MAJOR_TYPE_OPUS",
            "opus": {
                "title": "",
                "jump_url": "//www.bilibili.com/opus/1267441415275675648",
                "summary": {"text": "今天的 vlog 更新啦"},
                "pics": [{"url": "http://i0.hdslb.com/bfs/new_dyn/p1.jpg"}],
            },
        },
    )
    event = model.parse_dynamic_item(item)
    assert event is not None
    assert event.kind == "dynamic_new"
    assert event.text == "今天的 vlog 更新啦"
    assert event.url == "https://www.bilibili.com/opus/1267441415275675648"
    assert event.images == ("https://i0.hdslb.com/bfs/new_dyn/p1.jpg",)


def test_repost_carries_original_context(model):
    item = _item("DYNAMIC_TYPE_FORWARD", None, desc="转发理由")
    item["orig"] = {
        "id_str": "1",
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": _author(mid=9, name="被转发的人"),
            "module_dynamic": {
                "desc": {"text": "原动态正文"},
                "major": {"type": "MAJOR_TYPE_ARCHIVE", "archive": {"title": "原视频标题"}},
            },
        },
    }
    event = model.parse_dynamic_item(item)
    assert event is not None
    assert event.kind == "dynamic_repost"
    assert event.text == "转发理由"
    assert event.extra["orig_author"] == "被转发的人"
    assert event.extra["orig_title"] == "原视频标题"


def test_article_dynamic(model):
    item = _item(
        "DYNAMIC_TYPE_ARTICLE",
        {
            "type": "MAJOR_TYPE_ARTICLE",
            "article": {
                "id": "1234",
                "title": "专栏标题",
                "desc": "摘要",
                "covers": ["http://i0.hdslb.com/bfs/article/c.jpg"],
            },
        },
    )
    event = model.parse_dynamic_item(item)
    assert event is not None
    assert event.kind == "dynamic_article"
    assert event.title == "专栏标题"
    assert event.url == "https://www.bilibili.com/read/cv1234"


def test_pure_text_dynamic(model):
    event = model.parse_dynamic_item(_item("DYNAMIC_TYPE_WORD", None, desc="只有一段文字"))
    assert event is not None
    assert event.kind == "dynamic_new"
    assert event.text == "只有一段文字"


def test_live_rcmd_is_skipped(model):
    item = _item("DYNAMIC_TYPE_LIVE_RCMD", {"type": "MAJOR_TYPE_LIVE_RCMD", "live_rcmd": {"content": "{}"}})
    assert model.parse_dynamic_item(item) is None


def test_empty_dynamic_is_skipped(model):
    assert model.parse_dynamic_item(_item("DYNAMIC_TYPE_WORD", None, desc="")) is None
    assert model.parse_dynamic_item({"type": "DYNAMIC_TYPE_NONE"}) is None


def test_pinned_detection(model):
    assert model.is_pinned(_item(tag="置顶"))
    assert not model.is_pinned(_item())


def test_text_is_truncated(model):
    long_text = "字" * 500
    event = model.parse_dynamic_item(_item("DYNAMIC_TYPE_WORD", None, desc=long_text), text_limit=50)
    assert event is not None
    assert len(event.text) == 50
    assert event.text.endswith("…")


def test_crossed_milestone(model):
    assert model.crossed_milestone(900, 1200, 1000) == 1000
    assert model.crossed_milestone(1200, 1500, 1000) == 0
    assert model.crossed_milestone(0, 25000, 1000) == 25000
    assert model.crossed_milestone(999, 3000, 1000) == 3000
    assert model.crossed_milestone(1500, 0, 1000) == 0


def test_format_helpers(model):
    assert model.format_count(999) == "999"
    assert model.format_count(12345) == "1.2万"
    assert model.format_count(123_000_000) == "1.2亿"
    assert model.format_duration(3725) == "1 小时 2 分"
    assert model.format_duration(125) == "2 分 5 秒"
    assert model.format_duration(42) == "42 秒"


def test_live_events(model):
    on = model.build_live_on(
        uid=1, uname="主播", face="", room_id=6, title="直播标题",
        cover="http://i0.hdslb.com/k.jpg", area="虚拟主播", online=1200, start_ts=1700000000,
    )
    assert on.kind == "live_on"
    assert on.url == "https://live.bilibili.com/6"
    assert on.images[0].startswith("https://")
    assert on.event_key.endswith(":on")

    off = model.build_live_off(
        uid=1, uname="主播", face="", room_id=6, title="直播标题", cover="",
        start_ts=1700000000, end_ts=1700003600, peak_online=5000,
    )
    assert off.kind == "live_off"
    assert off.extra["duration"] == 3600
    assert off.extra["duration_text"] == "1 小时 0 分"
    assert off.extra["peak_online"] == 5000
