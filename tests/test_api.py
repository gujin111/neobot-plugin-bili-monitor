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
