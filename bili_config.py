"""插件配置模型：字段说明直接渲染成网页面板表单。

注意 ``cookie`` 这个名字命中宿主的敏感字段规则（cookie/token/password/...），
面板里只会显示「已设置 / 未设置」，不会回显明文。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SendMode = Literal["auto", "text", "image", "card"]


class BiliMonitorConfig(BaseModel):
    """B 站监控助手配置。"""

    command_prefix: str = Field(default="/", title="命令前缀")

    # ---------------------------------------------------------------- 账号
    cookie: str = Field(
        default="",
        title="B 站登录 Cookie",
        description="可选。填入 SESSDATA=...; bili_jct=... 可显著降低风控概率；留空则匿名访问。",
        json_schema_extra={"single_line": True, "placeholder": "SESSDATA=xxx; bili_jct=yyy"},
    )
    user_agent: str = Field(
        default="",
        title="自定义 User-Agent",
        description="留空使用内置的 Chrome UA；一般不需要改。",
        json_schema_extra={"single_line": True},
    )
    proxy: str = Field(
        default="",
        title="请求代理",
        description="形如 http://127.0.0.1:7890；留空直连。",
        json_schema_extra={"single_line": True},
    )
    request_timeout_seconds: int = Field(
        default=15, title="请求超时（秒）", ge=5, le=120,
    )

    # ---------------------------------------------------------------- 轮询
    dynamic_interval_seconds: int = Field(
        default=120, title="动态轮询间隔（秒）",
        description="每个 UP 主每隔这么久检查一次新动态。太小会更快触发风控，建议 90~300。",
        ge=60, le=3600,
    )
    live_interval_seconds: int = Field(
        default=90, title="直播轮询间隔（秒）",
        description="检查开播/下播状态的间隔，建议 60~180。",
        ge=30, le=1800,
    )
    follower_interval_seconds: int = Field(
        default=1800, title="粉丝数轮询间隔（秒）",
        description="粉丝数变化慢，默认半小时一次即可。",
        ge=300, le=86400,
    )
    max_subscriptions: int = Field(
        default=30, title="订阅上限", description="同时监控的 UP 主数量上限。", ge=1, le=200,
    )

    # ---------------------------------------------------------------- 推送
    send_mode: SendMode = Field(
        default="auto", title="默认发送模式",
        description="auto=有配图就发图、否则纯文本；text=只发文本；image=文本+原图；card=渲染卡片图（依赖本体截图能力）。",
    )
    push_gap_seconds: float = Field(
        default=3.0, title="推送间隔（秒）",
        description="两条提醒之间的最小间隔，用于规避消息屏蔽。设为 0 表示不限速。",
        ge=0, le=120,
    )
    max_events_per_cycle: int = Field(
        default=6, title="单轮最多推送条数",
        description="一次轮询里最多推送多少条新动态，超出的会跳过并记日志，避免刷屏。",
        ge=1, le=20,
    )
    at_all: bool = Field(
        default=False, title="默认 @全体成员",
        description="新订阅默认开启 @全体；需要机器人有群管理员权限，否则发送会失败。",
    )
    at_all_on_live_only: bool = Field(
        default=True, title="仅开播提醒 @全体",
        description="开启后，@全体只作用于开播提醒，动态/下播提醒不带 @。",
    )
    include_link: bool = Field(default=True, title="附带原文链接")
    max_text_chars: int = Field(
        default=180, title="正文截断长度", description="动态正文超过这个长度会截断并加省略号。",
        ge=40, le=1000,
    )

    # ---------------------------------------------------------------- 其它
    qr_image_service: str = Field(
        default="https://api.qrserver.com/v1/create-qr-code/?size=360x360&data={data}",
        title="二维码图片服务",
        description="扫码登录时把二维码内容转成图片的地址模板，{data} 会被替换成编码后的内容；留空则只发链接。",
        json_schema_extra={"single_line": True},
    )
    event_retention_days: int = Field(
        default=7, title="事件记录保留天数",
        description="用于去重与排障的事件日志保留时长。",
        ge=1, le=90,
    )
