#!/usr/bin/env python3
"""记录"出口公网 IP ↔ 平台可用性"的对照表，用来摸清拦截边界。

每换一个网络（公司/家里/手机热点）跑一次即可，结果追加到 out/ipcheck.csv：

    python3 ipcheck.py                       # 走当前默认路由（含代理）
    APILIB_BIND_IP=192.168.150.58 python3 ipcheck.py   # 绑定网卡源地址，绕过代理直连

判定：只发 1 次登录请求（约 7 KB），不下载数据。
"""
import csv
import datetime
import hashlib
import json
import os
import ssl
import subprocess
import urllib.request

import apilib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out", "ipcheck.csv")
ECHO = [("ip.3322.net", "http://ip.3322.net/"), ("www.taobao.com", None), ("api.ipify.org", "https://api.ipify.org")]


def doh_ipv4(host):
    url = f"https://223.5.5.5/resolve?name={host}&type=A"
    with urllib.request.urlopen(url, timeout=12, context=ssl.create_default_context()) as r:
        d = json.loads(r.read().decode())
    return next((a["data"] for a in d.get("Answer", []) if a.get("type") == 1), None)


def public_ip():
    """优先用国内回显服务直连取真实出口 IP；失败再退回默认路由。"""
    bind = apilib.BIND_IP
    for host, url in ECHO:
        if not url:
            continue
        ip = doh_ipv4(host)
        if not ip:
            continue
        cmd = ["curl", "-s", "--max-time", "12", "--resolve", f"{host}:80:{ip}", url]
        if bind:
            cmd[1:1] = ["--interface", bind]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            txt = (r.stdout or "").strip()
            if txt and len(txt) <= 64 and txt.count(".") == 3:
                return txt, host
        except Exception:  # noqa: BLE001
            continue
    try:
        r = subprocess.run(["curl", "-s", "--max-time", "12", "https://api.ipify.org"],
                           capture_output=True, text=True, timeout=20)
        t = (r.stdout or "").strip()
        if t and t.count(".") == 3:
            return t, "api.ipify.org(proxied)"
    except Exception:  # noqa: BLE001
        pass
    return "", ""


def isp_of(ip):
    try:
        url = f"http://ip-api.com/json/{ip}?fields=country,regionName,city,isp,as"
        with urllib.request.urlopen(url, timeout=12) as r:
            d = json.loads(r.read().decode())
        return f"{d.get('country','')}/{d.get('regionName','')}/{d.get('city','')} {d.get('isp','')} {d.get('as','')}".strip()
    except Exception:  # noqa: BLE001
        return ""


def probe_api():
    """只登录一次，返回 (可用?, 说明)。"""
    password = os.environ.get("REMOTE_DATA_PASSWORD")
    if not password:
        return False, "password-missing"
    code, payload = apilib.call("/api/Login/Login",
                                {"UserName": os.environ.get("REMOTE_DATA_USERNAME", "admin"),
                                 "Password": hashlib.md5(password.encode()).hexdigest()},
                                "POST", timeout=25, token_override="probe", raw=True)
    if code == 200 and payload[:1] == b"{":
        try:
            if json.loads(payload.decode("utf-8-sig")).get("Data", {}).get("Token"):
                return True, "ok"
        except Exception:  # noqa: BLE001
            pass
        return False, "json-no-token"
    txt = payload[:300].decode("utf-8", "replace")
    if "网站访问受限" in txt or "贝锐花生壳" in txt:
        return False, "oray-blocked"
    return False, f"http_{code}"


def load_rows():
    if not os.path.exists(OUT):
        return []
    with open(OUT, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    ip, src = public_ip()
    isp = isp_of(ip) if ip else ""
    ok, note = probe_api()
    row = {"time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
           "egress_ip": ip or "unknown", "resolve_via": src, "isp": isp,
           "bind": apilib.BIND_IP or "-", "api_ok": "YES" if ok else "NO", "note": note}
    rows = load_rows()
    fieldnames = ["time", "egress_ip", "resolve_via", "isp", "bind", "api_ok", "note"]
    with open(OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
        w.writerow(row)

    print(f"时间      : {row['time']}")
    print(f"出口 IP   : {row['egress_ip']}   ({row['isp'] or '未查到'})")
    print(f"探测方式  : {'绑定源地址 ' + apilib.BIND_IP if apilib.BIND_IP else '默认路由(可能走代理)'}")
    print(f"平台可用  : {'✅ 可用' if ok else '❌ 不可用'}  ({note})")
    print(f"\n历史记录（{len(rows) + 1} 条，{OUT}）:")
    print(f"  {'时间':<20}{'出口IP':<18}{'运营商':<46}{'结果'}")
    for r in rows + [row]:
        print(f"  {r['time']:<20}{r['egress_ip']:<18}{(r['isp'] or '-')[:44]:<46}{r['api_ok']}")
    ips = {}
    for r in rows + [row]:
        ips.setdefault(r["egress_ip"], set()).add(r["api_ok"])
    both = [i for i, v in ips.items() if len(v) > 1]
    print(f"\n小计：{len(ips)} 个不同出口 IP，其中 {sum(1 for v in ips.values() if 'YES' in v)} 个可用、"
          f"{sum(1 for v in ips.values() if 'YES' not in v)} 个被拦")
    if both:
        print(f"注意：{both} 在不同时间结果不同（可能是配额/风控而不是固定黑名单）")


if __name__ == "__main__":
    main()
