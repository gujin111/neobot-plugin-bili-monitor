"""B 站网页端接口客户端：buvid 指纹、WBI 签名、风控退避与扫码登录。

只依赖 httpx（NeoBot 本体已安装）。所有请求都带浏览器同源头，缺一不可：
- ``Referer`` 必须是空间/直播间页面，否则动态接口返回 412；
- ``buvid3`` / ``buvid4`` 来自 ``/x/frontend/finger/spi``，否则直播接口返回 -352；
- ``feed/space`` 等接口还要求 WBI 签名（``w_rid`` + ``wts``）。

协议错误统一抛 :class:`BiliApiError`，带 ``code`` 与可读 ``message``。
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

API = "https://api.bilibili.com"
LIVE_API = "https://api.live.bilibili.com"
PASSPORT = "https://passport.bilibili.com"

#: WBI mixin 表的固定置换
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
)

#: 需要整体退避的错误码：触发风控/频率限制
RISK_CODES = frozenset({-352, -412, -799, -509, -10403})

#: 伪造的浏览器环境参数，缺了会被判定为脚本
_DM_IMG_STR = "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ"
_DM_COVER_IMG_STR = (
    "QU5HTEUgKEludGVsLCBJbnRlbChSKSBVSEQgR3JhcGhpY3MgNjMwICgweDAwMDAzRTlCKSBEaXJlY3QzRDExIHZzXzVfMCBwc181XzApLCBvciBzaW1pbGFy"
)
_DM_IMG_INTER = '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}'


class BiliApiError(RuntimeError):
    """接口返回非 0 code，或网络层失败。"""

    def __init__(self, message: str, *, code: int | None = None, endpoint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.endpoint = endpoint

    @property
    def is_risk(self) -> bool:
        return self.code is not None and self.code in RISK_CODES


class BiliCooldownError(BiliApiError):
    """风控冷却中：调用方应等待，而不是换接口继续重试。

    ``retry_after`` 是需要等待的秒数。批量轮询遇到它应当整轮跳过，
    逐个订阅重试只会让冷却时间更长。
    """

    def __init__(self, message: str, *, retry_after: float = 0.0, endpoint: str = "") -> None:
        super().__init__(message, code=-412, endpoint=endpoint)
        self.retry_after = float(retry_after)


@dataclass(slots=True)
class LiveRoomSnapshot:
    room_id: int
    live_status: int
    title: str = ""
    cover: str = ""
    keyframe: str = ""
    online: int = 0
    live_time: int = 0
    area_name: str = ""
    parent_area_name: str = ""
    uid: int = 0

    @property
    def is_living(self) -> bool:
        return self.live_status == 1


@dataclass(slots=True)
class UpProfile:
    uid: int
    name: str = ""
    face: str = ""
    sign: str = ""
    follower: int | None = None
    room_id: int | None = None


@dataclass(slots=True)
class QrLoginSession:
    """扫码登录会话：把 ``qrcode_key`` 与展示用链接一起交给调用方。"""

    qrcode_key: str
    url: str
    created_at: float = field(default_factory=time.time)


def mixin_key(img_key: str, sub_key: str) -> str:
    """把 img/sub key 按固定表重排成 32 位 mixin key。"""
    raw = img_key + sub_key
    return "".join(raw[index] for index in MIXIN_KEY_ENC_TAB if index < len(raw))[:32]


def sign_wbi(params: dict[str, Any], img_key: str, sub_key: str) -> dict[str, Any]:
    """返回带 ``wts`` / ``w_rid`` 的新字典，不修改入参。"""
    signed = {key: value for key, value in params.items() if value is not None}
    signed["wts"] = int(time.time())
    items: list[tuple[str, str]] = []
    for key in sorted(signed):
        value = str(signed[key])
        # 官方前端的过滤规则：去掉 !'()* 这几个字符
        value = value.replace("!", "").replace("'", "").replace("(", "").replace(")", "").replace("*", "")
        items.append((key, value))
    query = urllib.parse.urlencode(items)
    signed["w_rid"] = hashlib.md5((query + mixin_key(img_key, sub_key)).encode()).hexdigest()
    return signed


def parse_cookie_string(raw: str) -> dict[str, str]:
    """解析 ``SESSDATA=xx; bili_jct=yy`` 形式的 Cookie，忽略噪音字段。"""
    cookies: dict[str, str] = {}
    for chunk in str(raw or "").replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


def extract_cookie_from_login_url(url: str) -> dict[str, str]:
    """扫码成功返回的跳转链接里带 SESSDATA / bili_jct / DedeUserID。"""
    if not url:
        return {}
    query = urllib.parse.urlparse(url).query
    parsed = urllib.parse.parse_qs(query)
    wanted = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")
    return {key: values[0] for key, values in parsed.items() if key in wanted and values}


class BiliClient:
    """带缓存、限速与风控退避的异步客户端。"""

    def __init__(
        self,
        *,
        cookie: str = "",
        user_agent: str = "",
        proxy: str = "",
        timeout: float = 15.0,
        logger: Any = None,
        cookie_provider: Any = None,
        cookie_saver: Any = None,
        min_request_interval: float | None = None,
    ) -> None:
        self._logger = logger
        self._timeout = max(3.0, float(timeout))
        self._ua = user_agent or DEFAULT_UA
        self._proxy = proxy or ""
        self._cookie_provider = cookie_provider
        self._cookie_saver = cookie_saver
        self._base_cookies = parse_cookie_string(cookie)
        self._cookies: dict[str, str] = dict(self._base_cookies)
        self._client: httpx.AsyncClient | None = None
        self._wbi: tuple[str, str, float] | None = None
        self._buvid_ready = False
        #: 新会话第一次动态请求可能被降级成空列表，只重试一次
        self._empty_retry_used = False
        self._lock = asyncio.Lock()
        self._cooldown_until = 0.0
        self._risk_strikes = 0
        self._log_cache: dict[str, float] = {}
        self._last_request = 0.0
        # 匿名访问时请求太密很容易触发风控：默认请求间隔 0.6 秒；
        # 直播开播快检（≤5 秒档）会调到 0.25 秒，同样受风控冷却保护
        self._min_interval = max(0.2, float(min_request_interval)) if min_request_interval else 0.6
        self.stats = {"requests": 0, "errors": 0, "risk": 0}

    # ---------------------------------------------------------------- 生命周期
    async def __aenter__(self) -> "BiliClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._cookie_provider is not None:
            try:
                stored = await self._cookie_provider()
            except Exception:  # pragma: no cover - 读取失败不应阻断启动
                stored = ""
            if stored:
                self._base_cookies.update(parse_cookie_string(stored))
                self._cookies.update(parse_cookie_string(stored))
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self._timeout),
            "follow_redirects": True,
            "headers": {
                "User-Agent": self._ua,
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            },
        }
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------------------------------------------------------------- 内部工具
    @property
    def has_login(self) -> bool:
        return "SESSDATA" in self._cookies and bool(self._cookies["SESSDATA"])

    def cookie_header(self) -> str:
        return "; ".join(f"{key}={value}" for key, value in self._cookies.items())

    def _log(self, level: str, message: str) -> None:
        logger = self._logger
        if logger is None:
            return
        try:
            getattr(logger, level)(message)
        except Exception:  # pragma: no cover - 日志失败不影响业务
            pass

    def _log_once(self, key: str, level: str, message: str, *, window: float = 600.0) -> None:
        """同一条风控提示最多每 window 秒记一次，避免轮询把日志刷满。"""
        now = time.monotonic()
        if now - self._log_cache.get(key, 0.0) < window:
            return
        self._log_cache[key] = now
        self._log(level, message)

    async def _throttle(self) -> None:
        now = time.monotonic()
        wait = self._min_interval - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _ensure_buvid(self) -> None:
        if self._buvid_ready or ("buvid3" in self._cookies and self._cookies["buvid3"]):
            self._buvid_ready = True
            return
        async with self._lock:
            if self._buvid_ready:
                return
            try:
                payload = await self._request(
                    "GET", f"{API}/x/frontend/finger/spi", referer="https://www.bilibili.com/"
                )
            except BiliApiError as exc:
                self._log("warning", f"获取 buvid 失败，动态接口可能被风控：{exc}")
                return
            data = payload or {}
            if data.get("b_3"):
                self._cookies["buvid3"] = str(data["b_3"])
            if data.get("b_4"):
                self._cookies["buvid4"] = str(data["b_4"])
            self._cookies.setdefault("b_nut", str(int(time.time())))
            self._buvid_ready = bool(self._cookies.get("buvid3"))
            if self._buvid_ready:
                await self._persist_cookies()

    async def _persist_cookies(self) -> None:
        if self._cookie_saver is None:
            return
        try:
            await self._cookie_saver(self.cookie_header())
        except Exception:  # pragma: no cover - 落盘失败只影响下次启动
            pass

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        referer: str = "https://www.bilibili.com/",
        origin: str = "https://www.bilibili.com",
        expect_json: bool = True,
        allow_codes: tuple[int, ...] = (),
        return_body: bool = False,
    ) -> Any:
        if self._client is None:
            raise BiliApiError("客户端尚未启动", endpoint=url)
        remaining = self.cooldown_remaining
        if remaining > 0:
            raise BiliCooldownError(
                f"风控冷却中，还需等待 {remaining:.0f} 秒", retry_after=remaining, endpoint=url
            )
        await self._throttle()
        headers = {"Referer": referer, "Origin": origin, "Cookie": self.cookie_header()}
        self.stats["requests"] += 1
        try:
            response = await self._client.request(method, url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            self.stats["errors"] += 1
            raise BiliApiError(f"网络请求失败：{exc}", endpoint=url) from exc
        if response.status_code in (412, 429):
            self.stats["risk"] += 1
            self._enter_cooldown(600)
            raise BiliApiError(f"触发风控 HTTP {response.status_code}", code=-412, endpoint=url)
        if response.status_code >= 400:
            self.stats["errors"] += 1
            raise BiliApiError(f"HTTP {response.status_code}", endpoint=url)
        try:
            body = response.json()
        except ValueError as exc:
            self.stats["errors"] += 1
            raise BiliApiError("响应不是 JSON", endpoint=url) from exc
        code = body.get("code")
        if code not in (0, None):
            if code in allow_codes:
                # 例如 nav 在未登录时返回 -101，但 data 里仍然带着 wbi_img
                return body if return_body else body.get("data")
            if code in RISK_CODES:
                self.stats["risk"] += 1
                # -352 多是「请求太快」的软风控，冷却短一点；-412 等按硬风控处理
                self._enter_cooldown(90 if int(code) == -352 else 600)
            raise BiliApiError(str(body.get("message") or f"接口返回 {code}"), code=int(code), endpoint=url)
        # 成功一次就把连续风控计数清零，避免偶发抖动把冷却越滚越长
        self._risk_strikes = 0
        if not expect_json or return_body:
            return body
        return body.get("data")

    def _enter_cooldown(self, base_seconds: float) -> None:
        """连续命中风控时指数退避，避免「一撞再撞」把冷却越拖越长。"""
        self._risk_strikes = min(self._risk_strikes + 1, 5)
        seconds = min(float(base_seconds) * (2 ** (self._risk_strikes - 1)), 1800.0)
        until = time.monotonic() + seconds
        if until > self._cooldown_until:
            self._cooldown_until = until
            self._log_once("cooldown", "warning", f"B 站风控触发，暂停请求 {seconds:.0f} 秒")

    @property
    def cooldown_remaining(self) -> float:
        """距冷却结束还剩多少秒（0 表示可以正常请求）。"""
        return max(0.0, self._cooldown_until - time.monotonic())

    # ---------------------------------------------------------------- WBI
    async def _wbi_keys(self, *, force: bool = False) -> tuple[str, str]:
        cached = self._wbi
        if cached is not None and not force and time.time() - cached[2] < 6 * 3600:
            return cached[0], cached[1]
        # 未登录时 nav 返回 -101，但 data 里仍然带 wbi_img
        body = await self._request(
            "GET", f"{API}/x/web-interface/nav",
            allow_codes=(-101,), return_body=True,
        )
        data = (body or {}).get("data") or {}
        try:
            img_url = data["wbi_img"]["img_url"]
            sub_url = data["wbi_img"]["sub_url"]
        except (KeyError, TypeError) as exc:
            raise BiliApiError("拿不到 WBI 密钥", endpoint="nav") from exc
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]
        self._wbi = (img_key, sub_key, time.time())
        return img_key, sub_key

    async def _signed(self, path: str, params: dict[str, Any], *, referer: str) -> dict[str, Any]:
        img_key, sub_key = await self._wbi_keys()
        signed = sign_wbi(params, img_key, sub_key)
        try:
            return await self._request("GET", f"{API}{path}", params=signed, referer=referer)
        except BiliApiError as exc:
            if exc.code in (-403, -352):
                # 密钥可能轮换，换个新的再试一次
                img_key, sub_key = await self._wbi_keys(force=True)
                signed = sign_wbi(params, img_key, sub_key)
                return await self._request("GET", f"{API}{path}", params=signed, referer=referer)
            raise

    # ---------------------------------------------------------------- 业务接口
    async def fetch_dynamic_feed(self, mid: int) -> list[dict[str, Any]]:
        """取空间首页一页动态（最新在前）。

        实测新会话的**第一次**动态请求会被软性降级成空列表（buvid 还没被站点认过），
        同一进程里的后续请求正常。这里对「进程内第一次拿到空列表」重试一次，
        避免新订阅建立基线时把最新动态漏掉。
        """
        await self._ensure_buvid()
        items = await self._fetch_dynamic_once(mid)
        if not items and not self._empty_retry_used:
            self._empty_retry_used = True
            await asyncio.sleep(0.8)
            items = await self._fetch_dynamic_once(mid)
        return items

    async def _fetch_dynamic_once(self, mid: int) -> list[dict[str, Any]]:
        data = await self._signed(
            "/x/polymer/web-dynamic/v1/feed/space",
            {
                "host_mid": mid,
                "offset": "",
                "timezone_offset": -480,
                "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote",
                "web_location": "333.1387",
                "dm_img_list": "[]",
                "dm_img_str": _DM_IMG_STR,
                "dm_cover_img_str": _DM_COVER_IMG_STR,
                "dm_img_inter": _DM_IMG_INTER,
            },
            referer=f"https://space.bilibili.com/{mid}/dynamic",
        )
        return list((data or {}).get("items") or [])

    async def fetch_relation_stat(self, mid: int) -> dict[str, Any]:
        data = await self._request(
            "GET", f"{API}/x/relation/stat", params={"vmid": mid}, referer=f"https://space.bilibili.com/{mid}/"
        )
        return dict(data or {})

    async def fetch_account_info(self, mid: int) -> dict[str, Any]:
        """昵称/头像优先走无需签名的 card 接口，拿不到再退回 WBI 空间接口。

        ``card`` 接口稳定且不吃风控；``space/wbi/acc/info`` 在缺少浏览器指纹参数时
        容易返回 -352，只作为兜底。
        """
        try:
            data = await self._request(
                "GET", f"{API}/x/web-interface/card",
                params={"mid": mid, "photo": "false"},
                referer=f"https://space.bilibili.com/{mid}/",
            )
            payload = data or {}
            card = payload.get("card") or {}
            if card.get("name"):
                return {
                    "mid": mid,
                    "name": card.get("name"),
                    "face": card.get("face"),
                    "sign": card.get("sign"),
                    "follower": payload.get("follower"),
                    "level": ((card.get("level_info") or {}).get("current_level")),
                }
        except BiliCooldownError:
            # 冷却中换接口重试没有意义，只会让冷却更久
            raise
        except BiliApiError as exc:
            self._log_once(
                f"card:{mid}", "warning", f"card 接口失败，改用空间接口 uid={mid}: {exc}"
            )
        data = await self._signed(
            "/x/space/wbi/acc/info",
            {
                "mid": mid,
                "platform": "web",
                "web_location": "1550101",
                "dm_img_list": "[]",
                "dm_img_str": _DM_IMG_STR,
                "dm_cover_img_str": _DM_COVER_IMG_STR,
                "dm_img_inter": _DM_IMG_INTER,
            },
            referer=f"https://space.bilibili.com/{mid}/",
        )
        return dict(data or {})

    async def fetch_live_room(self, room_id: int) -> LiveRoomSnapshot:
        data = await self._request(
            "GET",
            f"{LIVE_API}/room/v1/Room/get_info",
            params={"room_id": room_id},
            referer=f"https://live.bilibili.com/{room_id}",
            origin="https://live.bilibili.com",
        )
        data = data or {}
        cover = str(data.get("user_cover") or data.get("cover") or "")
        return LiveRoomSnapshot(
            room_id=_to_int(data.get("room_id"), room_id),
            live_status=_to_int(data.get("live_status"), 0),
            title=str(data.get("title") or ""),
            cover=_https(cover),
            keyframe=_https(str(data.get("keyframe") or "")),
            online=_to_int(data.get("online"), 0),
            # 开播中的 live_time 是 "2026-11-12 12:56:27" 这样的字符串，未开播是 "0000-00-00 00:00:00"
            live_time=_to_epoch(data.get("live_time")),
            area_name=str(data.get("area_name") or ""),
            parent_area_name=str(data.get("parent_area_name") or ""),
            uid=_to_int(data.get("uid"), 0),
        )

    async def fetch_live_master(self, uid: int) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"{LIVE_API}/live_user/v1/Master/info",
            params={"uid": uid},
            referer=f"https://live.bilibili.com/",
            origin="https://live.bilibili.com",
        )
        return dict(data or {})

    async def resolve_profile(self, uid: int) -> UpProfile:
        """聚合昵称/头像/粉丝数/直播间号。

        单个接口失败不影响其余字段；但**一旦进入风控冷却就立刻停止**——
        继续换接口重试只会把冷却时间拖得更长（这正是「card 失败换空间接口再失败」
        的根源）。
        """
        profile = UpProfile(uid=uid)

        # 1) card 接口一次性给昵称 / 头像 / 粉丝数
        try:
            info = await self.fetch_account_info(uid)
        except BiliCooldownError:
            raise
        except BiliApiError as exc:
            self._log_once(f"profile:{uid}", "warning", f"获取 UP 主资料失败 uid={uid}: {exc}")
            info = {}
        profile.name = str(info.get("name") or "")
        profile.face = _https(str(info.get("face") or ""))
        profile.sign = str(info.get("sign") or "")
        if info.get("follower") is not None:
            profile.follower = _to_int(info.get("follower"), 0)

        # 2) 直播间号 + 备用昵称/粉丝数
        try:
            master = await self.fetch_live_master(uid)
        except BiliCooldownError:
            raise
        except BiliApiError:
            master = {}
        if master.get("room_id"):
            profile.room_id = _to_int(master["room_id"], 0) or None
        if not profile.name:
            profile.name = str((master.get("info") or {}).get("uname") or "")
        if master.get("follower_num") is not None and profile.follower is None:
            profile.follower = _to_int(master["follower_num"], 0)

        # 3) 还缺粉丝数才去查关系接口（能少一次请求就少一次）
        if profile.follower is None:
            try:
                stat = await self.fetch_relation_stat(uid)
                if stat.get("follower") is not None:
                    profile.follower = _to_int(stat["follower"], 0)
            except BiliCooldownError:
                raise
            except BiliApiError:
                pass
        return profile

    # ---------------------------------------------------------------- 扫码登录
    async def qr_generate(self, qr_image_service: str = "") -> tuple[QrLoginSession, str]:
        """返回 (会话, 二维码图片地址)。图片服务为空时由调用方自行展示链接。"""
        data = await self._request(
            "GET",
            f"{PASSPORT}/x/passport-login/web/qrcode/generate",
            params={"source": "main-fe-header", "go_url": "https://www.bilibili.com/"},
            referer="https://www.bilibili.com/",
        )
        data = data or {}
        key = str(data.get("qrcode_key") or "")
        url = str(data.get("url") or "")
        if not key or not url:
            raise BiliApiError("二维码生成失败", endpoint="qr_generate")
        image = ""
        if qr_image_service:
            image = qr_image_service.replace("{data}", urllib.parse.quote(url, safe=""))
        return QrLoginSession(qrcode_key=key, url=url), image

    async def qr_poll(self, session: QrLoginSession) -> tuple[str, dict[str, str]]:
        """返回 (状态, cookies)；状态取值：waiting / scanned / expired / ok / error。"""
        data = await self._request(
            "GET",
            f"{PASSPORT}/x/passport-login/web/qrcode/poll",
            params={"qrcode_key": session.qrcode_key, "source": "main-fe-header"},
            referer="https://www.bilibili.com/",
        )
        data = data or {}
        code = int(data.get("code") or 0)
        if code == 0:
            cookies = extract_cookie_from_login_url(str(data.get("url") or ""))
            if cookies.get("SESSDATA"):
                self._cookies.update(cookies)
                self._buvid_ready = False
                await self._persist_cookies()
                return "ok", cookies
            return "error", {}
        if code == 86101:
            return "waiting", {}
        if code == 86090:
            return "scanned", {}
        if code == 86038:
            return "expired", {}
        return "error", {}

    async def download_image(self, url: str, *, max_bytes: int = 8 * 1024 * 1024) -> bytes | None:
        """把图片下载到内存里，供「按字节发送」使用。

        官方机器人通道在上传富媒体时会让 QQ 去拉图片地址，B 站 CDN 有防盗链，
        经常报「富媒体文件下载失败」。插件自己带 Referer 下载再交给通道上传，
        就绕开了这一步。
        """
        if self._client is None or not url:
            return None
        headers = {
            "Referer": "https://www.bilibili.com/",
            "User-Agent": self._ua,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
        }
        try:
            response = await self._client.get(url, headers=headers, follow_redirects=True)
        except httpx.HTTPError as exc:
            self._log_once(f"img:{url[:40]}", "warning", f"图片下载失败：{exc}")
            return None
        if response.status_code >= 400:
            self._log_once(f"img:{url[:40]}", "warning", f"图片下载失败：HTTP {response.status_code}")
            return None
        data = response.content
        if not data or len(data) > max_bytes:
            return None
        return data

    async def check_login(self) -> str:
        """返回当前登录账号昵称，未登录返回空字符串。"""
        try:
            body = await self._request(
                "GET", f"{API}/x/web-interface/nav",
                allow_codes=(-101,), return_body=True,
            )
        except BiliApiError:
            return ""
        data = (body or {}).get("data") or {}
        if data.get("isLogin"):
            return str(data.get("uname") or "已登录")
        return ""


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_epoch(value: Any) -> int:
    """把 ``1700000000`` / ``"1700000000"`` / ``"2026-11-12 12:56:27"`` 统一成秒级时间戳。"""
    if value in (None, "", "0000-00-00 00:00:00"):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(time.mktime(time.strptime(text, fmt)))
        except ValueError:
            continue
    return 0


def _https(url: str) -> str:
    """B 站 CDN 常给 http://，QQ 图片下载对 https 更宽容。"""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url
