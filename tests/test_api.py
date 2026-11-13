"""接口层纯函数：WBI 签名、Cookie 解析、时间与计数字段（不联网）。"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(scope="module")
def api(pkg):
    return importlib.import_module(f"{pkg.__name__}.bili_api")


# 官方前端的 mixin 表是固定置换，签名必须与之一致
IMG_KEY = "7cd084941338484aae1ad9425b84077c"
SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


def test_mixin_key_is_32_chars(api):
    key = api.mixin_key(IMG_KEY, SUB_KEY)
    assert len(key) == 32
    assert key == "ea1db124af3c7062474693fa704f4ff8"


def test_sign_wbi_adds_wts_and_w_rid(api, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000)
    signed = api.sign_wbi({"host_mid": 546195, "offset": ""}, IMG_KEY, SUB_KEY)
    assert signed["wts"] == 1700000000
    assert len(signed["w_rid"]) == 32
    assert signed["host_mid"] == 546195


def test_sign_wbi_is_deterministic(api, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000)
    first = api.sign_wbi({"a": 1, "b": 2}, IMG_KEY, SUB_KEY)
    second = api.sign_wbi({"b": 2, "a": 1}, IMG_KEY, SUB_KEY)
    assert first == second


def test_sign_wbi_strips_forbidden_chars(api, monkeypatch):
    monkeypatch.setattr(api.time, "time", lambda: 1700000000)
    plain = api.sign_wbi({"q": "abcdef"}, IMG_KEY, SUB_KEY)
    dirty = api.sign_wbi({"q": "a!b'c(d)e*f"}, IMG_KEY, SUB_KEY)
    # 过滤 !'()* 之后与干净字符串等价
    assert plain["w_rid"] == dirty["w_rid"]


def test_parse_cookie_string(api):
    cookies = api.parse_cookie_string("SESSDATA=abc; bili_jct=def ; DedeUserID=42")
    assert cookies == {"SESSDATA": "abc", "bili_jct": "def", "DedeUserID": "42"}
    assert api.parse_cookie_string("") == {}
    assert api.parse_cookie_string("garbage") == {}


def test_extract_cookie_from_login_url(api):
    url = "https://www.bilibili.com/?SESSDATA=s1&bili_jct=j1&DedeUserID=42&gourl=x"
    cookies = api.extract_cookie_from_login_url(url)
    assert cookies == {"SESSDATA": "s1", "bili_jct": "j1", "DedeUserID": "42"}
    assert api.extract_cookie_from_login_url("") == {}


def test_to_epoch(api):
    assert api._to_epoch("0000-00-00 00:00:00") == 0
    assert api._to_epoch("") == 0
    assert api._to_epoch(None) == 0
    assert api._to_epoch("1700000000") == 1700000000
    assert api._to_epoch(1700000000) == 1700000000
    assert api._to_epoch("2026-11-12 12:56:27") > 0


def test_to_int(api):
    assert api._to_int("12", 0) == 12
    assert api._to_int("12.7", 0) == 12
    assert api._to_int(None, 5) == 5
    assert api._to_int("abc", 7) == 7


def test_https_upgrade(api):
    assert api._https("http://i0.hdslb.com/x.jpg") == "https://i0.hdslb.com/x.jpg"
    assert api._https("//i0.hdslb.com/x.jpg") == "https://i0.hdslb.com/x.jpg"
    assert api._https("https://x/y.jpg") == "https://x/y.jpg"


def test_risk_codes_are_classified(api):
    error = api.BiliApiError("risk", code=-352)
    assert error.is_risk is True
    assert api.BiliApiError("other", code=-101).is_risk is False


# ---------------------------------------------------------------- 风控冷却
class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttp:
    """假的 httpx.AsyncClient：按顺序返回预设响应。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    async def request(self, *_args, **_kwargs):
        self.calls += 1
        item = self.responses.pop(0) if self.responses else {"code": 0, "data": {}}
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse(item)

    async def aclose(self):
        return None


def _client_with(api, *responses):
    client = api.BiliClient(timeout=5)
    client._client = FakeHttp(*responses)
    client._buvid_ready = True
    client._min_interval = 0
    return client


async def test_soft_risk_352_enters_short_cooldown(api):
    client = _client_with(api, {"code": -352, "message": "风控校验失败"})

    with pytest.raises(api.BiliApiError) as caught:
        await client._request("GET", "https://api.bilibili.com/x/web-interface/card")

    assert caught.value.code == -352
    # 第一次 -352 冷却 90 秒
    assert 0 < client.cooldown_remaining <= 90


async def test_hard_risk_412_enters_long_cooldown(api):
    client = _client_with(api, {"code": -412, "message": "请求被拦截"})

    with pytest.raises(api.BiliApiError):
        await client._request("GET", "https://api.bilibili.com/x/web-interface/card")

    assert client.cooldown_remaining > 500


async def test_request_during_cooldown_raises_cooldown_error(api):
    client = _client_with(api, {"code": -352, "message": "风控"})
    with pytest.raises(api.BiliApiError):
        await client._request("GET", "https://api.bilibili.com/x/web-interface/card")

    with pytest.raises(api.BiliCooldownError) as caught:
        await client._request("GET", "https://api.bilibili.com/x/web-interface/card")

    assert caught.value.retry_after > 0
    assert caught.value.is_risk is True


async def test_cooldown_escalates_on_repeated_risk(api):
    client = _client_with(api)
    client._enter_cooldown(90)
    first = client.cooldown_remaining
    client._enter_cooldown(90)
    assert client.cooldown_remaining > first


async def test_success_resets_risk_strikes(api):
    client = _client_with(api, {"code": -352, "message": "风控"}, {"code": 0, "data": {"ok": 1}})
    with pytest.raises(api.BiliApiError):
        await client._request("GET", "https://api.bilibili.com/x/a")
    assert client._risk_strikes == 1

    client._cooldown_until = 0  # 手动解除冷却，模拟冷却已过
    assert await client._request("GET", "https://api.bilibili.com/x/b") == {"ok": 1}
    assert client._risk_strikes == 0


async def test_card_failure_propagates_cooldown(api, monkeypatch):
    client = _client_with(api)
    client._cooldown_until = 0

    async def fake_card(*_args, **_kwargs):
        raise api.BiliCooldownError("风控冷却中", retry_after=88)

    monkeypatch.setattr(client, "_request", fake_card)
    with pytest.raises(api.BiliCooldownError):
        await client.fetch_account_info(1)


async def test_resolve_profile_stops_at_cooldown(api, monkeypatch):
    client = _client_with(api)
    called: list[str] = []

    async def fail_account(mid):
        called.append("account")
        raise api.BiliCooldownError("风控冷却中", retry_after=60)

    async def record_master(mid):
        called.append("master")
        return {}

    monkeypatch.setattr(client, "fetch_account_info", fail_account)
    monkeypatch.setattr(client, "fetch_live_master", record_master)

    with pytest.raises(api.BiliCooldownError):
        await client.resolve_profile(546195)
    assert called == ["account"]        # 冷却后不再换接口重试


def test_client_min_request_interval(api):
    assert api.BiliClient()._min_interval == 0.6
    assert api.BiliClient(min_request_interval=0.25)._min_interval == 0.25
    assert api.BiliClient(min_request_interval=0.01)._min_interval >= 0.2
