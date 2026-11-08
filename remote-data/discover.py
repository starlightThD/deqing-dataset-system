#!/usr/bin/env python3
"""数据摸底：抓取资源清单 + 各类资源根索引文件，输出可拉取性报告。

用法：python3 discover.py [--out out]
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
from contextlib import closing

import apilib

INCLINE = 4000001      # 航空倾斜
TERRAIN_3DTILES = 1000012
POINTCLOUD_3DTILES = 13000006
TMS = 12000008
WMS = 12000004
WMTS = 12000005
WFS = 2000004
VIDEO = 10000004


def fetch_all_resources(outdir):
    rows, page = [], 1
    while True:
        code, payload = apilib.jcall("/api/cim/CIMScene/GetCIMSceneResourceByPage", {
            "BigResourceType": [], "DataSetIds": [], "Type": 0, "Label": "",
            "StartTime": None, "EndTime": None, "PageIndex": page, "PageSize": 100})
        if code != 200:
            raise SystemExit(f"资源列表请求失败 HTTP {code}")
        rows.extend(payload["Rows"])
        total = payload["Total"]
        if len(rows) >= total:
            break
        page += 1
    for r in rows:
        md = r.get("MetaData")
        if isinstance(md, str) and md.strip():
            try:
                r["_meta"] = json.loads(md)
            except json.JSONDecodeError:
                r["_meta"] = None
    with open(os.path.join(outdir, "resources_all.json"), "w", encoding="utf-8") as f:
        json.dump({"Total": total, "Rows": rows}, f, ensure_ascii=False, indent=1)
    return rows


def get_stream(key, timeout=300):
    return apilib.call("/api/DownLoad/GetFileStream?" + urllib.parse.quote(key, safe="/"), timeout=timeout)


def parse_db3(raw):
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(raw)
        tables = {}
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            try:
                cols = [c[1] for c in db.execute(f'PRAGMA table_info("{name}")')]
                n = db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                tables[name] = {"rows": n, "columns": cols}
            except sqlite3.Error as exc:
                tables[name] = {"error": str(exc)}
        return tables


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    rows = fetch_all_resources(args.out)
    print(f"资源总数: {len(rows)}")

    report = []
    for r in rows:
        rid, t = r["Id"], r.get("Type")
        key = r.get("OriginalRootFileId")
        item = {"id": rid, "label": r.get("Label"), "type": t,
                "typeName": r.get("TypeName"), "rootFileId": key,
                "fileCount": r.get("FileCount"), "fileSize": r.get("FileSize")}
        if t in (INCLINE, TERRAIN_3DTILES, POINTCLOUD_3DTILES) and key:
            code, raw = get_stream(key)
            item["rootHttp"], item["rootBytes"] = code, len(raw)
            ext = os.path.splitext(key)[1].lstrip(".").lower()
            d = os.path.join(args.out, "root", rid)
            os.makedirs(d, exist_ok=True)
            if code == 200:
                with open(os.path.join(d, "root." + ext), "wb") as f:
                    f.write(raw)
                if ext == "db3":
                    item["db3Tables"] = parse_db3(raw)
                elif ext == "json":
                    try:
                        j = json.loads(raw.decode("utf-8-sig"))
                        s = json.dumps(j, ensure_ascii=False)
                        item["jsonKeys"] = sorted(j.keys())
                        item["absoluteUrls"] = sorted(set(re.findall(r'https?://[^"\\ ]+', s)))[:5]
                        item["sampleUris"] = sorted(set(re.findall(r'"uri"\s*:\s*"([^"]+)"', s)))[:5]
                        item["uriCount"] = len(set(re.findall(r'"uri"\s*:\s*"([^"]+)"', s)))
                    except Exception as exc:  # noqa: BLE001
                        item["jsonError"] = str(exc)
        report.append(item)
        print(f"  {item['typeName']:<14} {rid} root={item.get('rootHttp')} bytes={item.get('rootBytes')}")

    with open(os.path.join(args.out, "probe_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print("报告: out/probe_report.json")


if __name__ == "__main__":
    main()
