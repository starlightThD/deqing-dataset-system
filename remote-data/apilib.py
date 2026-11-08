#!/usr/bin/env python3
"""轻量 API 客户端：登录一次并复用 token（token 缓存在 .token）。"""
import functools
import hashlib
import http.client
import json
import os
import urllib.error
import urllib.request

BASE = "http://1jk412ww3207.vicp.fun:16761"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


class _BoundHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *a, **k):
        k.setdefault("source_address", (BIND_IP, 0))
        super().__init__(*a, **k)


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *a, **k):
        k.setdefault("source_address", (BIND_IP, 0))
        super().__init__(*a, **k)


class _BoundHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_BoundHTTPConnection, req)


class _BoundHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_BoundHTTPSConnection, req, context=self._context)


# 设置 APILIB_BIND_IP=192.168.150.58 时，连接绑定该源地址直连（绕过本机 Mihomo 代理）。
# 注意：代理环境下系统 DNS 会给假地址（198.18.x.x），所以此时用 DoH 取真实 IPv4。
BIND_IP = os.environ.get("APILIB_BIND_IP", "").strip()
_RESOLVED = {}


def _doh_ipv4(host):
    if host in _RESOLVED:
        return _RESOLVED[host]
    import ssl
    url = f"https://223.5.5.5/resolve?name={host}&type=A"
    with urllib.request.urlopen(url, timeout=15, context=ssl.create_default_context()) as r:
        data = json.loads(r.read().decode())
    ip = next((a["data"] for a in data.get("Answer", []) if a.get("type") == 1), None)
    if not ip:
        raise OSError(f"DoH 解析失败: {host}")
    _RESOLVED[host] = ip
    return ip


if BIND_IP:
    import socket as _socket
    _orig_gai = _socket.getaddrinfo

    def _gai(host, port, family=0, *a, **k):
        if isinstance(host, str) and not host.replace(".", "").isdigit():
            host = _doh_ipv4(host)
        return _orig_gai(host, port, _socket.AF_INET, *a, **k)

    _socket.getaddrinfo = _gai
    OPENER = urllib.request.build_opener(NoRedirect(), _BoundHTTPHandler(), _BoundHTTPSHandler())
else:
    OPENER = urllib.request.build_opener(NoRedirect())


def login(user=None, pwd=None):
    user = user or os.environ.get("REMOTE_DATA_USERNAME", "admin")
    pwd = pwd or os.environ.get("REMOTE_DATA_PASSWORD")
    if not pwd:
        raise RuntimeError("set REMOTE_DATA_PASSWORD before logging in to the legacy service")
    body = json.dumps({"UserName": user, "Password": hashlib.md5(pwd.encode()).hexdigest()}).encode()
    req = urllib.request.Request(BASE + "/api/Login/Login", data=body,
                                 headers={"Content-Type": "application/json;charset=UTF-8"})
    with OPENER.open(req, timeout=50) as r:
        token = json.loads(r.read().decode("utf-8-sig"))["Data"]["Token"]
    descriptor = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w") as f:
        f.write(token)
    return token


def token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            t = f.read().strip()
        if t:
            return t
    return login()


def call(path, body=None, method=None, timeout=60, token_override=None, raw=False):
    t = token_override or token()
    headers = {"Authorization": "Bearer " + t, "Accept": "*/*", "Accept-Encoding": "identity"}
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json;charset=UTF-8"
    if method is None:
        method = "POST" if data is not None else "GET"
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=timeout) as r:
            payload = r.read()
            return r.status, payload
    except urllib.error.HTTPError as e:
        code = e.code
        payload = e.read()
        if code == 401 and not raw:
            login()
            return call(path, body, method, timeout, token_override=None, raw=raw)
        return code, payload


def jcall(path, body=None, method=None, **kw):
    code, payload = call(path, body, method, **kw)
    try:
        return code, json.loads(payload.decode("utf-8-sig"))
    except Exception:  # noqa: BLE001
        return code, payload
