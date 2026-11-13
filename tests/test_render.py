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


# ---------------------------------------------------------------- 链接按钮
def test_live_link_is_button_label_in_text(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    text = render.render_text(event)
    assert "▶ 点击进入直播间：https://live.bilibili.com/21852" in text


def test_video_link_uses_own_label(model, render):
    event = _event(model, kind="dynamic_video", url="https://www.bilibili.com/video/BV1xx")
    assert "观看视频：https://www.bilibili.com/video/BV1xx" in render.render_text(event)


def test_html_link_style(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    text = render.render_text(event, link_style="html")
    assert (
        '<a href="https://live.bilibili.com/21852" target="_blank" rel="noopener noreferrer">'
        "点击进入直播间</a>"
    ) in text

    chain = render.link_chain(event, link_style="html")
    assert chain.plain_text.startswith("▶ <a href=")


def test_html_link_escapes_url(model, render):
    event = _event(model, kind="live_on", url='https://x/"onmouseover="alert(1)')
    text = render.render_text(event, link_style="html")
    assert 'onmouseover="alert(1)"' not in text
    assert "&quot;" in text


def test_markdown_link_style(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    text = render.render_text(event, link_style="markdown")
    assert "▶ [点击进入直播间](https://live.bilibili.com/21852)" in text

    chain = render.link_chain(event, link_style="markdown")
    assert chain.plain_text == "▶ [点击进入直播间](https://live.bilibili.com/21852)"


def test_link_chain_carries_clickable_url(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    chain = render.link_chain(event)
    assert chain.plain_text == "▶ 点击进入直播间：https://live.bilibili.com/21852"
    assert len(chain) == 1


def test_link_chain_empty_without_url(model, render):
    event = _event(model, url="")
    assert len(render.link_chain(event)) == 0


def test_card_renders_button(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    html = render.card_html(event)
    assert 'class="btn"' in html
    assert "▶ 点击进入直播间" in html
    assert 'href="https://live.bilibili.com/21852"' in html


def test_card_button_hidden_when_link_disabled(model, render):
    event = _event(model, kind="live_on", url="https://live.bilibili.com/21852")
    html = render.card_html(event, include_link=False)
    assert 'class="btn"' not in html


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


# ---------------------------------------------------------------- 命令速查卡片
def test_help_blocks_follow_host_card_dsl(render):
    """区块直接交给本体内置 render_card_html（heading / rows / note）。"""
    blocks = render.help_blocks(version="9.9.9")
    kinds = [block["kind"] for block in blocks]
    assert kinds[0] == "heading"
    assert kinds[-1] == "note"
    assert "9.9.9" in blocks[-1]["text"]

    rows = [block for block in blocks if block["kind"] == "rows"]
    assert len(rows) == len(render.HELP_GROUPS)
    for block, (_title, _color, items) in zip(rows, render.HELP_GROUPS):
        assert block["columns"] == ["命令", "作用"]
        assert [list(row) for row in block["rows"]] == [[s, d] for s, d in items]

    for block in blocks:
        assert set(block) <= {"kind", "text", "columns", "widths", "rows"}


def test_help_text_lists_all_groups(render):
    text = render.help_text()
    for title, _color, items in render.HELP_GROUPS:
        assert title in text
        for syntax, _desc in items:
            assert syntax in text
    assert "@机器人" in text


def test_help_markdown_is_marked_up(render):
    md = render.help_markdown(version="9.9.9")
    assert md.startswith("**B 站监控助手 · 命令速查**")
    assert "v9.9.9" in md
    for title, _color, items in render.HELP_GROUPS:
        assert f"**{title}**" in md
        for syntax, desc in items:
            assert f"{syntax} — {desc}" in md
    # 只用加粗 + 换行，不含 markdown 表格（命令里的 | 是语法占位符，不算）
    assert "\n|" not in md
    assert "---|" not in md

def test_build_tuwen_card(model, render):
    event = _event(
        model, kind="live_on", title="今晚八点，决战",
        cover="http://i0.hdslb.com/c.jpg", url="https://live.bilibili.com/21852",
        extra={"area": "单机游戏", "online": 12000},
    )
    card = render.build_tuwen(event)
    assert card is not None
    assert card["title"] == "老番茄 开播了"
    assert "今晚八点，决战" in card["description"]
    assert "单机游戏" in card["description"]
    assert card["url"] == "https://live.bilibili.com/21852"
    assert card["pic_url"].startswith("https://")
    assert len(card["title"]) <= 50 and len(card["description"]) <= 200


def test_build_tuwen_requires_https_cover(model, render):
    event = _event(model, kind="live_on", cover="", images=())
    assert render.build_tuwen(event) is None
