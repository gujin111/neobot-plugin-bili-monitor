"""把消息链交给 NeoBot 宿主发送，并提供基于宿主截图端口的卡片渲染。

插件不自己持有连接：所有发送都走上下文（``ctx.send`` / ``ctx.send_image``），
这样机器人重连、协议适配由本体负责。
"""

from __future__ import annotations

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
    """推送发送器：一条消息一个会话。"""

    def __init__(self, ctx: Any, logger: Any = None) -> None:
        self._ctx = ctx
        self._logger = logger

    async def send_chain(self, kind: str, target_id: str, chain: Any) -> bool:
        conversation = conversation_for(self._ctx, kind, target_id)
        await self._ctx.send(conversation, chain)
        return True

    async def send_image(self, kind: str, target_id: str, data: bytes, filename: str) -> bool:
        conversation = conversation_for(self._ctx, kind, target_id)
        await self._ctx.send_image(conversation, data=data, filename=filename)
        return True


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
