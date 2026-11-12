"""文案渲染与卡片：占位符、缺字段、@ 与配图组合。"""

from __future__ import annotations

from neobot_modloader import MessageChain


def _event(model, **overrides):
    base = dict(
        kind="dynamic_new",
        uid=546195,
        uname="老番茄",
        event_key="dyn:1",
        url="https://t.bilibili.com/1",
        text="今天的更新来了",
        face="https://i0.hdslb.com/bfs/face/x.jpg",
        published_at=1793955619,
    )
    base.update(overrides)
    return model.MonitorEvent(**base)


def test_default_template_contains_name_text_url(model, render):
    event = _event(model)
    text = render.render_text(event)
    assert "【动态提醒】" in text
    assert "老番茄" in text
    assert "今天的更新来了" in text
    assert event.url in text


def test_custom_template_overrides_default(model, render):
    event = _event(model)
    text = render.render_text(event, custom={"dynamic_new": "{name} → {text}"})
    assert text == "老番茄 → 今天的更新来了"


def test_unknown_placeholder_becomes_empty(model, render):
    event = _event(model)
    text = render.render_text(event, custom={"dynamic_new": "[{not_a_field}]{name}"})
    assert text == "[]老番茄"


def test_backslash_n_means_newline(model, render):
    event = _event(model)
    text = render.render_text(event, custom={"dynamic_new": "{name}\\n{url}"})
    assert text == "老番茄\nhttps://t.bilibili.com/1"


def test_include_link_false_drops_url_line(model, render):
    event = _event(model)
    text = render.render_text(event, include_link=False)
    assert event.url not in text


def test_video_fields_are_humanized(model, render):
    event = _event(
        model,
        kind="dynamic_video",
        title="新视频标题",
        extra={"duration": "12:34", "play": 2367000},
    )
    text = render.render_text(event)
    assert "新视频标题" in text
    assert "12:34" in text
    assert "236.7万" in text


def test_live_off_fields(model, render):
    event = _event(
        model,
        kind="live_off",
        title="直播标题",
        extra={"duration_text": "1 小时 2 分", "peak_online": 45678},
    )
    text = render.render_text(event)
    assert "下播" in text
    assert "1 小时 2 分" in text
    assert "4.6万" in text


def test_follower_event_fields(model, render):
    event = _event(model, kind="follower", extra={"follower": 12345, "milestone": 10000})
    text = render.render_text(event)
    assert "粉丝数突破" in text
    assert "1.0万" in text
    assert "1.2万" in text


def test_build_chain_adds_at_and_images(model, render):
    event = _event(model, kind="dynamic_video", images=("https://i0.hdslb.com/a.jpg", "https://i0.hdslb.com/b.jpg"))
    chain = render.build_chain(event, at_all=True, at_ids=("123",), with_images=True, max_images=2)
    assert isinstance(chain, MessageChain)
    segments = chain.segments
    assert segments[0].type == "at" and segments[0].data["qq"] == "all"
    assert any(seg.type == "at" and seg.data.get("qq") == "123" for seg in segments)
    images = [seg for seg in segments if seg.type == "image"]
    assert len(images) == 2
    assert images[0].data["url"] == "https://i0.hdslb.com/a.jpg"


def test_build_chain_without_images(model, render):
    event = _event(model)
    chain = render.build_chain(event, with_images=True)
    assert [seg for seg in chain.segments if seg.type == "image"] == []


def test_card_html_escapes_user_content(model, render):
    event = _event(model, text='<script>alert("x")</script>', title="<b>标题</b>")
    html = render.card_html(event)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&lt;b&gt;标题&lt;/b&gt;" in html
    assert "老番茄" in html


def test_card_html_includes_cover(model, render):
    event = _event(model, kind="dynamic_video", cover="https://i2.hdslb.com/cover.jpg", title="标题")
    html = render.card_html(event)
    assert 'src="https://i2.hdslb.com/cover.jpg"' in html
    assert "新视频" in html


def test_template_fields_documented(render):
    fields = render.describe_template_fields("dynamic_video")
    assert "{title}" in fields
    assert "{play_text}" in fields


def test_summary_line(render, model):
    event = _event(model, title="标题")
    assert "新动态" in render.summary_line(event)
    assert "老番茄" in render.summary_line(event)
