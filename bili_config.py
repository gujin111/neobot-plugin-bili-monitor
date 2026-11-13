"""插件配置模型：只有**面板上真正需要调**的几项，其余用代码内默认值。

面板字段越少越好：能自动判断的（发不发图、链接写法、帮助卡片形态）都由插件
按宿主能力决定，不再让使用者去选。完整默认值见 :data:`DEFAULTS`，
用 :func:`cfg` 读取（配置里没有这一项时自动回落到默认值）。

``cookie`` 这个名字命中宿主的敏感字段规则（cookie/token/password/...），
面板里只会显示「已设置 / 未设置」，不会回显明文。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SendMode = Literal["auto", "text", "image", "card"]

#: 不在面板上暴露、但代码要用的默认值（旧配置里即使还留着也不会报错）
DEFAULTS: dict[str, Any] = {
    "user_agent": "",
    "proxy": "",
    "request_timeout_seconds": 15,
    "follower_interval_seconds": 1800,
    "max_events_per_cycle": 6,
    "push_gap_seconds": 3.0,
    "at_all": False,
    "at_all_on_live_only": True,
    "max_text_chars": 180,
    "media_mode": "auto",
    "max_images": 2,
    "help_format": "auto",
    "link_style": "auto",
    "help_image_theme": "default",
    "help_image_width": 760,
    "qr_image_service": "https://api.qrserver.com/v1/create-qr-code/?size=360x360&data={data}",
    "event_retention_days": 7,
}


def cfg(config: Any, key: str) -> Any:
    """读一个配置项：面板字段优先，否则用 :data:`DEFAULTS` 里的默认值。"""
    value = getattr(config, key, None)
    if value is None:
        return DEFAULTS.get(key)
    return value


class BiliMonitorConfig(BaseModel):
    """B 站监控助手配置（7 项，覆盖 95% 的使用场景）。"""

    #: 允许旧配置里残留已删除的字段（面板不会显示，也不报错）
    model_config = ConfigDict(extra="allow")

    command_prefix: str = Field(default="/", title="命令前缀")

    cookie: str = Field(
        default="",
        title="B 站登录 Cookie",
        description="可选。填 SESSDATA=...; bili_jct=... 可显著降低风控概率；留空则匿名访问。",
        json_schema_extra={"single_line": True, "placeholder": "SESSDATA=xxx; bili_jct=yyy"},
    )
    dynamic_interval_seconds: int = Field(
        default=120, title="动态轮询间隔（秒）",
        description="每个 UP 主每隔这么久检查一次；太小容易触发风控，建议 90~300。",
        ge=60, le=3600,
    )
    live_interval_seconds: int = Field(
        default=90, title="直播轮询间隔（秒）",
        description="检查开播/下播的间隔，建议 60~180。",
        ge=30, le=1800,
    )
    send_mode: SendMode = Field(
        default="auto", title="发送模式",
        description="auto=有配图就发图、否则纯文本；text=只发文本；image=文本+原图；card=渲染卡片图。",
    )
    include_link: bool = Field(
        default=True, title="附带原文链接",
        description="关掉后提醒里不带「点击进入直播间」那行。",
    )
    max_subscriptions: int = Field(
        default=30, title="订阅上限", description="同时监控的 UP 主数量上限。", ge=1, le=200,
    )
