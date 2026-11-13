"""把消息链交给 NeoBot 宿主发送，并提供基于宿主截图端口的卡片渲染。

插件不自己持有连接：所有发送都走上下文（``ctx.send`` / ``ctx.send_image``），
这样机器人重连、协议适配由本体负责。
"""

from __future__ import annotations

import inspect
from typing import Any

from .bili_model import MonitorEvent, normalize_target_id
from .bili_render import card_html


def conversation_for(ctx: Any, kind: str, target_id: str) -> Any:
    """把 ``group`` / ``private`` 目标换成宿主会话对象。

    发送前统一归一化（去掉 ``group:`` 前缀等），确保推到的是真正的群号/QQ 号。
    """
    ident = normalize_target_id(target_id)
    try:
        numeric: Any = int(ident)
    except (TypeError, ValueError):
        numeric = ident
    if kind == "group":
        return ctx.conversation_from_event({"message_type": "group", "group_id": numeric})
    return ctx.conversation_from_event({"message_type": "private", "user_id": numeric})


def target_from_context(command_ctx: Any) -> tuple[str, str]:
    """从命令上下文推断「这条命令是在哪个群/哪个人发起的」。

    优先读原始消息里的 ``group_id``（最可靠），退回复用命令的 ``conv_id``；
    返回值统一成 ``("group"|"private", "纯数字ID")``。
    """
    kind = "group" if str(getattr(command_ctx, "kind", "")) == "group" else "private"
    raw_message = getattr(command_ctx, "message", None)
    data: dict[str, Any] = {}
    if raw_message is not None:
        if hasattr(raw_message, "model_dump"):
            try:
                data = dict(raw_message.model_dump(mode="python"))
            except Exception:  # pragma: no cover - 适配器字段千奇百怪
                data = {}
        elif isinstance(raw_message, dict):
            data = dict(raw_message)
        else:
            group_id = getattr(raw_message, "group_id", None)
            if group_id:
                data["group_id"] = group_id
    group_id = data.get("group_id") or data.get("group")
    if group_id:
        ident = normalize_target_id(group_id)
        if ident:
            return "group", ident
    conv_id = normalize_target_id(getattr(command_ctx, "conv_id", "") or "")
    if kind == "private":
        user_id = normalize_target_id(data.get("user_id") or getattr(command_ctx, "user_id", ""))
        if user_id:
            return "private", user_id
    return kind, conv_id


class HostSender:
    """推送发送器：一条消息一个会话。

    NeoBot 2.1.0a9 起 ``ctx.send`` / ``ctx.send_image`` 增加了 ``proactive`` 参数：
    官方通道在用户没有被动回复窗口时只能按**主动消息**发送，监控推送正是这种场景。
    这里探测宿主是否支持该参数，旧版本（a7/a8）保持原调用方式。
    """

    #: 宿主 send/send_image 是否接受 proactive（引擎据此决定是否传参）
    supports_proactive_kw = True

    def __init__(self, ctx: Any, logger: Any = None) -> None:
        self._ctx = ctx
        self._logger = logger
        #: 当前通道：official / onebot / mixed（决定发不发图、帮助卡片形态）
        self.channel = channel_kind(ctx)
        #: 图片通道失败过一次就置位，本进程不再尝试发图
        self.media_blocked = False
        #: 宿主是否接受 proactive（a9 起官方通道主动消息需要它）
        self.supports_proactive_kw = _accepts_kwarg(getattr(ctx, "send", None), "proactive")
        self._image_proactive = _accepts_kwarg(getattr(ctx, "send_image", None), "proactive")

    async def send_chain(self, kind: str, target_id: str, chain: Any, *, proactive: bool = True) -> bool:
        conversation = conversation_for(self._ctx, kind, target_id)
        if proactive and self.supports_proactive_kw:
            await self._ctx.send(conversation, chain, proactive=True)
        else:
            await self._ctx.send(conversation, chain)
        return True

    async def send_image(
        self, kind: str, target_id: str, data: bytes, filename: str, *, proactive: bool = True
    ) -> bool:
        conversation = conversation_for(self._ctx, kind, target_id)
        if proactive and self._image_proactive:
            await self._ctx.send_image(conversation, data=data, filename=filename, proactive=True)
        else:
            await self._ctx.send_image(conversation, data=data, filename=filename)
        return True


def _accepts_kwarg(func: Any, name: str) -> bool:
    """宿主方法是否接受某个关键字参数（兼容 a7/a8 没有 proactive 的版本）。"""
    if func is None:
        return False
    try:
        return name in inspect.signature(func).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/装饰器对象兜底
        return False


# ---------------------------------------------------------------- 通道能力
OFFICIAL = "official"
ONEBOT = "onebot"


def channel_kind(ctx: Any) -> str:
    """问本体现在走的是哪条通道：``official`` / ``onebot`` / ``mixed`` / ``""``。

    直接读适配器自己的标识（2.1.0a9 的 ``SwitchableAdapter.active_channels`` 与
    ``current_channel()``），只有老版本拿不到时才退回类名判断——不再靠字符串瞎猜。
    """
    adapter = getattr(ctx, "adapter", None)
    if adapter is None:
        return ""
    try:
        from neobot_adapter.switchable import current_channel  # type: ignore
    except Exception:  # pragma: no cover - 老版本没有这个模块
        current_channel = None
    try:
        active = tuple(getattr(adapter, "active_channels", ()) or ())
    except Exception:  # pragma: no cover
        active = ()
    if active:
        # 事件处理期间能拿到「当前通道」；后台任务里拿不到就按在跑的通道判断
        current = current_channel() if current_channel is not None else None
        if current in active:
            return str(current)
        if set(active) == {OFFICIAL}:
            return OFFICIAL
        if set(active) == {ONEBOT}:
            return ONEBOT
        return "mixed"
    name = type(adapter).__name__.lower()
    if "official" in name:
        return OFFICIAL
    if "onebot" in name or "local" in name:
        return ONEBOT
    return ""


def media_allowed(config: Any, sender: Any) -> bool:
    """本次能不能发图片：熔断标记 + `media_mode` + 通道。

    官方通道的富媒体上传是让 QQ 按 URL 去拉文件，而本体的图片在本地文件服务里，
    QQ 拉不到就会报「富媒体文件下载失败」，所以官方通道默认**不发图**。
    """
    if getattr(sender, "media_blocked", False):
        return False
    mode = str(getattr(config, "media_mode", "auto") or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    return getattr(sender, "channel", "") != OFFICIAL


class HtmlCardRenderer:
    """调用本体的截图端口把卡片 HTML 渲染成 PNG。

    宿主没开浏览器 / 截图不可用时 ``available`` 为 False，引擎会自动降级成
    「文本 + 原图」，不会因为渲染失败而丢掉提醒。
    """

    def __init__(self, ctx: Any, logger: Any = None, *, timeout: float = 25.0) -> None:
        self._ctx = ctx
        self._logger = logger
        self._timeout = timeout
        self._warned = False
        self._port = getattr(ctx, "screenshots", None)

    @property
    def available(self) -> bool:
        return self._port is not None

    def _warn(self, message: str) -> None:
        if self._warned or self._logger is None:
            return
        self._warned = True
        try:
            self._logger.warning(message)
        except Exception:  # pragma: no cover
            pass

    async def render(self, event: MonitorEvent, custom: dict[str, str] | None) -> bytes | None:
        return await self.render_html(card_html(event, custom=custom))

    async def render_html(self, html: str, *, width: int = 640, height: int = 900) -> bytes | None:
        """把任意自包含 HTML 渲染成 PNG；任何失败都返回 None，由调用方降级。"""
        port = getattr(self._ctx, "screenshots", self._port)
        if port is None:
            return None
        try:
            from neobot_modloader import RenderOptions, ScreenshotOptions

            result = await port.render(
                html=html,
                options=RenderOptions(
                    screenshot=ScreenshotOptions(
                        mode="full_page", width=width, height=height, format="png", scale=1.0
                    ),
                    wait_for_fonts=False,
                    wait_for_images=True,
                    timeout=self._timeout,
                ),
            )
        except ImportError:  # pragma: no cover - 老版本宿主没有截图契约
            self._warn("宿主未提供截图契约，卡片模式降级为图片模式")
            return None
        except Exception as exc:  # noqa: BLE001 - 渲染失败只降级不报错
            self._warn(f"图片渲染失败，降级为文本：{type(exc).__name__}: {exc}")
            return None
        data = getattr(result, "data", None)
        return bytes(data) if data else None
