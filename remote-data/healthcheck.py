#!/usr/bin/env python3
"""远程服务体检：API 登录 / 资源列表 / 文件流 / GeoServer / 前端，并识别花生壳拦截页。

用法: python3 healthcheck.py
退出码: 0=数据接口正常, 1=数据接口不可用, 2=未提供登录密码
"""
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

API = "http://1jk412ww3207.vicp.fun:16761"
GEOSERVER = "http://1jk412ww3207.vicp.fun:45119"
WEB = "http://1jk412ww3207.vicp.fun"
PROBE_KEY = "group1/M00/47/D3/wKgCAmpUQFaAdY2KAD0BIOQ3DkU131.jpg"
ORAY_MARKS = ("贝锐花生壳", "网站访问受限", "设备环境受限", "映射不支持网页访问")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):
        return None


OPENER = urllib.request.build_opener(NoRedirect())


def req(url, body=None, headers=None, timeout=25):
    h = {"Accept": "*/*", "Accept-Encoding": "identity"}
    h.update(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json;charset=UTF-8"
    r = urllib.request.Request(url, data=data, headers=h)
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(), ""
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read(), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return -1, {}, b"", f"{type(exc).__name__}: {exc}"


def is_oray_page(raw):
    txt = raw[:4000].decode("utf-8", "replace")
    return any(m in txt for m in ORAY_MARKS)


def main():
    ok = True
    print(f"检查时间: {__import__('datetime').datetime.now():%Y-%m-%d %H:%M:%S}\n")
    password = os.environ.get("REMOTE_DATA_PASSWORD")
    if not password:
        print("缺少 REMOTE_DATA_PASSWORD，无法执行登录体检。")
        return 2

    # 1. 登录
    status, hdrs, raw, err = req(API + "/api/Login/Login", {
        "UserName": os.environ.get("REMOTE_DATA_USERNAME", "admin"),
        "Password": hashlib.md5(password.encode()).hexdigest()})
    token = None
    if status == 200 and raw[:1] == b"{":
        try:
            token = json.loads(raw.decode("utf-8-sig"))["Data"]["Token"]
        except Exception:  # noqa: BLE001
            token = None
    if token:
        print(f"[ OK ] 登录           HTTP {status}，已取得令牌")
    else:
        ok = False
        print(f"[FAIL] 登录           HTTP {status} {err}  {len(raw)}B"
              + ("  ← 命中花生壳拦截页" if is_oray_page(raw) else ""))

    auth = {"Authorization": "Bearer " + token} if token else {}

    # 2. 资源列表
    status, hdrs, raw, err = req(API + "/api/cim/CIMScene/GetCIMSceneResourceByPage", {
        "BigResourceType": [], "DataSetIds": [], "Type": 0, "Label": "", "StartTime": None,
        "EndTime": None, "PageIndex": 1, "PageSize": 3}, headers=auth)
    total = None
    if status == 200 and raw[:1] == b"{":
        try:
            total = json.loads(raw.decode("utf-8-sig")).get("Total")
        except Exception:  # noqa: BLE001
            total = None
    if total is not None:
        print(f"[ OK ] 资源列表       HTTP {status}，Total={total}")
    else:
        ok = False
        print(f"[FAIL] 资源列表       HTTP {status} {err}  {len(raw)}B"
              + ("  ← 命中花生壳拦截页" if is_oray_page(raw) else ""))

    # 3. 文件流
    status, hdrs, raw, err = req(API + "/api/DownLoad/GetFileStream?" + PROBE_KEY, headers=auth)
    if status == 200 and raw[:3] == b"\xff\xd8\xff":
        print(f"[ OK ] 文件下载       HTTP {status}，{len(raw)/1e6:.1f} MB JPEG")
    else:
        ok = False
        print(f"[FAIL] 文件下载       HTTP {status} {err}  {len(raw)}B"
              + ("  ← 命中花生壳拦截页" if is_oray_page(raw) else ""))

    # 4. GeoServer
    status, hdrs, raw, err = req(GEOSERVER + "/geoserver/wms?service=WMS&version=1.3.0&request=GetCapabilities")
    if status == 200 and b"WMS_Capabilities" in raw[:400]:
        print(f"[ OK ] GeoServer      HTTP {status}，能力文档 {len(raw)}B")
    else:
        print(f"[WARN] GeoServer      HTTP {status} {err}  {len(raw)}B"
              + ("  ← 命中花生壳拦截页" if is_oray_page(raw) else ""))

    # 5. 前端(80)
    status, hdrs, raw, err = req(WEB + "/")
    if status == 200 and b"<html" in raw[:200].lower() and not is_oray_page(raw):
        print(f"[ OK ] 前端 80 端口    HTTP {status}，{len(raw)}B（未受影响）")
    else:
        print(f"[WARN] 前端 80 端口    HTTP {status} {err}  {len(raw)}B")

    print()
    if ok:
        print("结论：数据接口可用。")
        return 0
    print("结论：数据接口当前不可用（见上）。本地数据与脚本不受影响，恢复后可直接续跑 mirror.py。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
