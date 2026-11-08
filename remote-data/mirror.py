#!/usr/bin/env python3
"""远程数据本地化工具：把平台可获取的资源目录、影像索引、影像文件拉取并写入本地 SQLite。

用法：
  python3 mirror.py survey            # 资源清单 + 各类根索引 + 本地数据库（不含大文件）
  python3 mirror.py images [--limit N] [--workers 8] [--resource <id>]   # 下载倾斜影像
  python3 mirror.py assets            # 下载 XML/快照/包围盒索引等小文件
  python3 mirror.py report            # 打印本地库统计
"""
import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from contextlib import closing

import apilib

T_INCLINE = 4000001
T_WMS = 12000004
T_WMTS = 12000005
T_TMS = 12000008
T_TERRAIN = 1000012
T_PC_3DTILES = 13000006
T_VIDEO = 10000004
T_WFS = 2000004

FILE_RESOURCE_TYPES = {T_INCLINE, T_TERRAIN, T_PC_3DTILES, T_VIDEO, T_TMS}

SCHEMA = """
CREATE TABLE IF NOT EXISTS dataset (
    id TEXT PRIMARY KEY, name TEXT, resource_count INTEGER
);
CREATE TABLE IF NOT EXISTS resource (
    id TEXT PRIMARY KEY, label TEXT, name TEXT, type INTEGER, type_name TEXT,
    big_resource_type INTEGER, dataset_id TEXT, file_count INTEGER, file_size INTEGER,
    original_root_file_id TEXT, upload_time TEXT, data_production_time TEXT,
    epsg_id INTEGER, srs_id TEXT, range_wkt TEXT, data_url TEXT,
    is_upload_completed INTEGER, is_deleted INTEGER, metadata_json TEXT,
    retrievable TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS resource_asset (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL, kind TEXT NOT NULL, fastdfs_id TEXT,
    relative_path TEXT, local_path TEXT, bytes INTEGER, sha256 TEXT,
    status TEXT, note TEXT,
    UNIQUE(resource_id, kind, fastdfs_id, relative_path)
);
CREATE TABLE IF NOT EXISTS photo_group (
    resource_id TEXT NOT NULL, group_id INTEGER NOT NULL, group_name TEXT,
    photo_type TEXT, description TEXT,
    PRIMARY KEY (resource_id, group_id)
);
CREATE TABLE IF NOT EXISTS photo (
    resource_id TEXT NOT NULL, photo_id INTEGER NOT NULL, fastdfs_id TEXT,
    group_id INTEGER, row_no INTEGER, col_no INTEGER, width REAL, height REAL,
    focal_length REAL, pixel_size REAL, principal_x REAL, principal_y REAL,
    dist_k1 REAL, dist_k2 REAL, dist_k3 REAL, dist_k4 REAL, dist_p1 REAL, dist_p2 REAL,
    x REAL, y REAL, z REAL, phi REAL, omega REAL, kappa REAL, rotation_sys INTEGER,
    rpc TEXT, model_matrix TEXT, project_matrix TEXT, ori_flag INTEGER,
    is_cc_xml_data INTEGER, local_path TEXT, bytes INTEGER, sha256 TEXT, status TEXT,
    PRIMARY KEY (resource_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_photo_status ON photo(status);
CREATE TABLE IF NOT EXISTS download_log (
    ts TEXT, resource_id TEXT, kind TEXT, target TEXT, status TEXT, bytes INTEGER, detail TEXT
);
"""


def db_path(out):
    return os.path.join(out, "catalog.db")


class Store:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert(self, table, row, keys):
        cols = ",".join(row)
        ph = ",".join("?" * len(row))
        upd = ",".join(f"{c}=excluded.{c}" for c in row if c not in keys)
        sql = f"INSERT INTO {table}({cols}) VALUES({ph}) ON CONFLICT({','.join(keys)}) DO UPDATE SET {upd}"
        with self.lock:
            self.conn.execute(sql, tuple(row.values()))
            self.conn.commit()

    def exec(self, sql, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def rows(self, sql, params=()):
        with self.lock:
            return self.conn.execute(sql, params).fetchall()


RETRY_STATUS = {204, 429, 500, 502, 503, 504}


def get_stream(key, timeout=600, retries=4):
    """GET /api/DownLoad/GetFileStream?<fastdfs key>；204/5xx 与网络异常按指数退避重试。"""
    last = (-1, b"")
    for i in range(retries):
        try:
            code, payload = apilib.call("/api/DownLoad/GetFileStream?" + urllib.parse.quote(key, safe="/"),
                                        timeout=timeout)
            if code == 200:
                return 200, payload
            last = (code, payload)
            if code in RETRY_STATUS:
                time.sleep(1.5 * (i + 1))
                continue
            return code, payload
        except Exception as exc:  # noqa: BLE001
            last = (-1, str(exc).encode())
            time.sleep(1.5 * (i + 1))
    return last


def unc_to_key(value):
    """把 DB3 里的 UNC 路径（\\\\host\\share\\group1\\M00\\..）转成 FastDFS 键。"""
    if not isinstance(value, str):
        return None
    v = value.replace("\\", "/")
    if v.startswith("//"):
        parts = v[2:].split("/")
        if len(parts) < 5:
            return None
        v = "/".join(parts[2:])
    if not v.startswith("group"):
        return None
    return v


def fetch_resources():
    rows, page, total = [], 1, None
    while True:
        code, payload = apilib.jcall("/api/cim/CIMScene/GetCIMSceneResourceByPage", {
            "BigResourceType": [], "DataSetIds": [], "Type": 0, "Label": "",
            "StartTime": None, "EndTime": None, "PageIndex": page, "PageSize": 100})
        if code != 200:
            raise SystemExit(f"资源列表请求失败 HTTP {code}")
        rows.extend(payload["Rows"])
        total = payload["Total"]
        if len(rows) >= total or not payload["Rows"]:
            break
        page += 1
    return rows, total


def kind_of(res):
    t = res.get("Type")
    return {T_INCLINE: "incline_index", T_TERRAIN: "terrain_3dtiles_root",
            T_PC_3DTILES: "pointcloud_3dtiles_root"}.get(t)


def cmd_survey(args):
    out = args.out
    os.makedirs(out, exist_ok=True)
    store = Store(db_path(out))
    rows, total = fetch_resources()
    print(f"资源总数 {total}，已获取 {len(rows)}")

    datasets = {}
    for r in rows:
        md = r.get("MetaData")
        meta = None
        if isinstance(md, str) and md.strip():
            try:
                meta = json.loads(md)
            except json.JSONDecodeError:
                meta = None
        r["_meta"] = meta
        datasets.setdefault(r.get("DataSetId"), {"name": r.get("Name"), "n": 0})
        datasets[r.get("DataSetId")]["n"] += 1
        root = r.get("OriginalRootFileId")
        retrievable, note = "metadata", ""
        if r.get("Type") == T_INCLINE:
            retrievable, note = "full", "db3 索引 + 全部照片 + 相机 XML 均可下载"
        elif r.get("Type") in (T_TERRAIN, T_PC_3DTILES):
            retrievable = "partial"
            note = ("仅 tileset.json 根文件可下载；分块 *json/b3dm/pnts 无文件清单接口，"
                    "接口未暴露相对路径→FastDFS 映射")
        elif r.get("Type") == T_VIDEO:
            retrievable = "partial"
            note = "仅首帧快照 PNG 可下载；MP4 原片无文件 id 暴露"
        elif r.get("Type") == T_TMS:
            retrievable = "none"
            note = "平台内切片，无根文件 id、无切片服务地址暴露"
        elif r.get("Type") in (T_WMS, T_WMTS, T_WFS):
            retrievable = "service"
            note = "仅服务元数据；DataUrl 指向内网 GeoServer(192.168.2.2:8080) 外部不可达"
        store.upsert("resource", {
            "id": r["Id"], "label": r.get("Label"), "name": r.get("Name"), "type": r.get("Type"),
            "type_name": r.get("TypeName"), "big_resource_type": r.get("BigResourceType"),
            "dataset_id": r.get("DataSetId"), "file_count": r.get("FileCount"),
            "file_size": r.get("FileSize"), "original_root_file_id": root,
            "upload_time": r.get("UploadTime"), "data_production_time": r.get("DataProductionTime"),
            "epsg_id": r.get("EpsgId"), "srs_id": r.get("SrsId"), "range_wkt": r.get("Range"),
            "data_url": r.get("DataUrl"),
            "is_upload_completed": int(bool(r.get("IsUploadCompleted"))),
            "is_deleted": int(bool(r.get("IsDeleted"))),
            "metadata_json": json.dumps(meta, ensure_ascii=False) if meta is not None else None,
            "retrievable": retrievable, "note": note,
        }, ["id"])

    for d, v in datasets.items():
        store.upsert("dataset", {"id": d, "name": v["name"], "resource_count": v["n"]}, ["id"])

    os.makedirs(os.path.join(out, "resources"), exist_ok=True)
    with open(os.path.join(out, "resources", "resources_all.json"), "w", encoding="utf-8") as f:
        json.dump({"Total": total, "Rows": rows}, f, ensure_ascii=False, indent=1)

    # 根索引文件：倾斜 db3（含全部照片清单）与 3dtiles tileset.json
    for r in rows:
        kind = kind_of(r)
        if not kind or not r.get("OriginalRootFileId"):
            continue
        key = r["OriginalRootFileId"]
        ext = os.path.splitext(key)[1].lstrip(".").lower()
        code, raw = get_stream(key)
        rel = f"indexes/{r['Id']}/{'index.db3' if ext == 'db3' else 'root.json'}"
        local = os.path.join(out, rel)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        status = "ok" if code == 200 else f"http_{code}"
        if code == 200:
            with open(local, "wb") as f:
                f.write(raw)
            sha = hashlib.sha256(raw).hexdigest()
            if ext == "db3":
                ingest_db3(store, out, r, raw, local, sha)
        else:
            sha = None
        store.upsert("resource_asset", {
            "resource_id": r["Id"], "kind": kind, "fastdfs_id": key, "relative_path": None,
            "local_path": rel if code == 200 else None, "bytes": len(raw) if code == 200 else None,
            "sha256": sha, "status": status, "note": None,
        }, ["resource_id", "kind", "fastdfs_id", "relative_path"])
        print(f"  [{kind}] {r['Id']} {r.get('Label')} -> {status} {len(raw)} bytes")

    print("survey 完成，本地库:", db_path(out))


def ingest_db3(store, out, res, raw, local, sha):
    rid = res["Id"]
    os.makedirs(os.path.join(out, "indexes", rid), exist_ok=True)
    with closing(sqlite3.connect(":memory:")) as db:
        db.deserialize(raw)
        try:
            groups = db.execute("SELECT Id, GroupName, PhotoType, Description FROM PhotoGroup").fetchall()
        except sqlite3.Error:
            groups = []
        for g in groups:
            store.upsert("photo_group", {"resource_id": rid, "group_id": g[0], "group_name": g[1],
                                         "photo_type": g[2], "description": g[3]},
                         ["resource_id", "group_id"])
        try:
            photos = db.execute("""SELECT Id, PhotoFile, GroupId, RowNo, ColNo, Width, Height,
                FocalLength, PixelSize, Principal_X, Principal_Y, Distotion_K1, Distotion_K2,
                Distotion_K3, Distotion_K4, Distotion_P1, Distotion_P2, X, Y, Z, Phi, Omega, Kappa,
                RotationSys, RPC, ModelMatrix, ProjectMatrix, OriFlag, IsCCXmlData
                FROM Photo ORDER BY Id""").fetchall()
        except sqlite3.Error as exc:
            print(f"  ! {rid} Photo 表读取失败: {exc}")
            return
    batch = []
    for p in photos:
        key = unc_to_key(p[1])
        batch.append({
            "resource_id": rid, "photo_id": p[0], "fastdfs_id": key, "group_id": p[2],
            "row_no": p[3], "col_no": p[4], "width": p[5], "height": p[6], "focal_length": p[7],
            "pixel_size": p[8], "principal_x": p[9], "principal_y": p[10], "dist_k1": p[11],
            "dist_k2": p[12], "dist_k3": p[13], "dist_k4": p[14], "dist_p1": p[15], "dist_p2": p[16],
            "x": p[17], "y": p[18], "z": p[19], "phi": p[20], "omega": p[21], "kappa": p[22],
            "rotation_sys": p[23], "rpc": p[24], "model_matrix": p[25], "project_matrix": p[26],
            "ori_flag": p[27], "is_cc_xml_data": p[28], "local_path": None, "bytes": None,
            "sha256": None, "status": "pending",
        })
    with store.lock:
        store.conn.executemany(
            """INSERT INTO photo(resource_id, photo_id, fastdfs_id, group_id, row_no, col_no, width,
            height, focal_length, pixel_size, principal_x, principal_y, dist_k1, dist_k2, dist_k3,
            dist_k4, dist_p1, dist_p2, x, y, z, phi, omega, kappa, rotation_sys, rpc, model_matrix,
            project_matrix, ori_flag, is_cc_xml_data, local_path, bytes, sha256, status)
            VALUES(:resource_id,:photo_id,:fastdfs_id,:group_id,:row_no,:col_no,:width,:height,
            :focal_length,:pixel_size,:principal_x,:principal_y,:dist_k1,:dist_k2,:dist_k3,:dist_k4,
            :dist_p1,:dist_p2,:x,:y,:z,:phi,:omega,:kappa,:rotation_sys,:rpc,:model_matrix,
            :project_matrix,:ori_flag,:is_cc_xml_data,:local_path,:bytes,:sha256,:status)
            ON CONFLICT(resource_id, photo_id) DO UPDATE SET fastdfs_id=excluded.fastdfs_id""", batch)
        store.conn.commit()
    print(f"    照片清单 {len(batch)} 条写入本地库")


def cmd_images(args):
    out = args.out
    store = Store(db_path(out))
    sql = "SELECT resource_id, photo_id, fastdfs_id, status FROM photo WHERE fastdfs_id IS NOT NULL"
    params = []
    if args.resource:
        sql += " AND resource_id=?"
        params.append(args.resource)
    if getattr(args, "aoi", None):
        sql += (" AND EXISTS (SELECT 1 FROM photo_selection ps WHERE ps.resource_id=photo.resource_id"
                " AND ps.photo_id=photo.photo_id AND ps.aoi=?)")
        params.append(args.aoi)
    if not args.include_duplicates:
        sql += (" AND NOT EXISTS (SELECT 1 FROM resource_duplicate rd WHERE rd.dup_id=photo.resource_id)")
    if not args.redownload:
        sql += " AND (status IS NULL OR status!='ok')"
    photo_rows = store.rows(sql, tuple(params))
    if args.limit:
        photo_rows = photo_rows[:args.limit]
    print(f"待下载照片 {len(photo_rows)}")
    counter = {"ok": 0, "fail": 0, "bytes": 0}
    lock = threading.Lock()

    def work(item):
        rid, pid, key, _status = item
        ext = os.path.splitext(key)[1] or ".jpg"
        rel = f"images/{rid}/{pid:06d}_{os.path.basename(key)[:20]}{ext}"
        local = os.path.join(out, rel)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            with lock:
                counter["ok"] += 1
            store.exec("UPDATE photo SET status='ok', local_path=?, bytes=? WHERE resource_id=? AND photo_id=?",
                       (rel, os.path.getsize(local), rid, pid))
            return
        code, raw = get_stream(key)
        if code == 200 and raw[:3] == b"\xff\xd8\xff" or (code == 200 and raw[:8] == b"\x89PNG\r\n\x1a\n"):
            os.makedirs(os.path.dirname(local), exist_ok=True)
            tmp = local + ".part"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, local)
            sha = hashlib.sha256(raw).hexdigest()
            store.exec("""UPDATE photo SET status='ok', local_path=?, bytes=?, sha256=?
                          WHERE resource_id=? AND photo_id=?""", (rel, len(raw), sha, rid, pid))
            with lock:
                counter["ok"] += 1
                counter["bytes"] += len(raw)
        else:
            store.exec("""UPDATE photo SET status=? WHERE resource_id=? AND photo_id=?""",
                       (f"http_{code}" if code != 200 else "bad_content", rid, pid))
            with lock:
                counter["fail"] += 1

    t0 = time.time()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        futs = [ex.submit(work, it) for it in photo_rows]
        for i, f in enumerate(cf.as_completed(futs), 1):
            f.result()
            if i % 100 == 0 or i == len(futs):
                dt = time.time() - t0
                print(f"  {i}/{len(futs)} ok={counter['ok']} fail={counter['fail']} "
                      f"{counter['bytes']/2**20:.0f}MiB {counter['bytes']/max(dt,1e-6)/2**20:.1f}MiB/s",
                      flush=True)


def cmd_assets(args):
    out = args.out
    store = Store(db_path(out))
    # 相机/空三 XML、视频首帧快照、3dtiles 包围盒索引
    items = []
    for rid, meta_json, typ in store.rows("SELECT id, metadata_json, type FROM resource"):
        if not meta_json:
            continue
        meta = json.loads(meta_json)
        xml = meta.get("XmlFileId")
        if xml:
            items.append((rid, "camera_xml", xml))
        bb = meta.get("BoundingBoxDb3FileId")
        if bb:
            items.append((rid, "boundingbox_db3", bb))
        for v in meta.get("videoInfos") or []:
            if v.get("FirstSnapshotFileId"):
                items.append((rid, "video_snapshot", v["FirstSnapshotFileId"]))
    print(f"待下载小文件 {len(items)}")
    for rid, kind, key in items:
        ext = os.path.splitext(key)[1] or ".bin"
        rel = f"assets/{kind}/{rid}{ext}"
        local = os.path.join(out, rel)
        if os.path.exists(local) and os.path.getsize(local) > 0:
            code, raw = 200, open(local, "rb").read()
        else:
            code, raw = get_stream(key)
            if code == 200:
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, "wb") as f:
                    f.write(raw)
        store.upsert("resource_asset", {
            "resource_id": rid, "kind": kind, "fastdfs_id": key, "relative_path": None,
            "local_path": rel if code == 200 else None, "bytes": len(raw) if code == 200 else None,
            "sha256": hashlib.sha256(raw).hexdigest() if code == 200 else None,
            "status": "ok" if code == 200 else f"http_{code}", "note": None,
        }, ["resource_id", "kind", "fastdfs_id", "relative_path"])
        print(f"  {kind:<18} {rid} {code} {len(raw)}")


def cmd_dedupe(args):
    """按空三位姿比对，找出同一航飞重复上传到多个数据集的资源。"""
    import geo
    store = Store(db_path(args.out))
    store.exec("""CREATE TABLE IF NOT EXISTS resource_duplicate (
        dup_id TEXT PRIMARY KEY, canonical_id TEXT, matched_photos INTEGER, note TEXT)""")
    res = [r[0] for r in store.rows("SELECT id FROM resource WHERE type=4000001")]
    poses = {}
    for rid in res:
        xml = os.path.join(args.out, "assets", "camera_xml", rid + ".xml")
        if not os.path.exists(xml):
            continue
        cam = geo.parse_camera_xml(xml)
        s = set()
        for g in cam["groups"]:
            for p in g["photos"]:
                lon, lat = cam["crs"].to_lonlat(p["C"][0], p["C"][1])
                s.add((round(lat, 7), round(lon, 7), round(p["C"][2], 1)))
        poses[rid] = s
    store.exec("DELETE FROM resource_duplicate")
    seen = set()
    for i, a in enumerate(res):
        if a in seen or a not in poses:
            continue
        group = [a]
        for b in res[i + 1:]:
            if b not in poses or b in seen:
                continue
            ov = len(poses[a] & poses[b])
            if ov and ov >= 0.95 * min(len(poses[a]), len(poses[b])):
                group.append(b)
                seen.add(b)
        for b in group[1:]:
            store.exec("INSERT OR REPLACE INTO resource_duplicate VALUES (?,?,?,?)",
                       (b, a, len(poses[a] & poses[b]), "位姿重合，判为同一航飞重复上传"))
            print(f"  重复: {b} == {a}（{len(poses[a] & poses[b])} 个相机站位重合）")
    print("去重表写入完成")


def cmd_report(args):
    store = Store(db_path(args.out))
    print("== 资源类型统计 ==")
    for tn, n, files, size in store.rows(
            "SELECT type_name, COUNT(*), SUM(file_count), SUM(file_size) FROM resource "
            "GROUP BY type_name ORDER BY COUNT(*) DESC"):
        size = size or 0
        print(f"  {tn:<16} 资源 {n:<4} 标称文件 {files or 0:<7} 标称大小 {size/2**30:8.2f} GiB")
    print("== 照片清单（DB3 索引） ==")
    for rid, label, n, have, b in store.rows("""
            SELECT r.id, r.label, COUNT(p.photo_id),
                   SUM(CASE WHEN p.status='ok' THEN 1 ELSE 0 END), SUM(p.bytes)
            FROM resource r JOIN photo p ON p.resource_id=r.id GROUP BY r.id ORDER BY 3 DESC"""):
        print(f"  {label:<14} {rid} 照片 {n:<6} 已下载 {have or 0:<6} {(b or 0)/2**30:7.2f} GiB")
    tot = store.rows("SELECT COUNT(*), SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END), SUM(bytes) FROM photo")[0]
    print(f"  合计: {tot[0]} 张清单 / 已下载 {tot[1] or 0} 张 / {(tot[2] or 0)/2**30:.2f} GiB")
    print("== 资源文件 ==")
    for kind, n, b in store.rows("SELECT kind, COUNT(*), SUM(bytes) FROM resource_asset GROUP BY kind"):
        print(f"  {kind:<22} {n:<4} {(b or 0)/2**20:8.2f} MiB")
    print("== 可拉取性 ==")
    for ret, n in store.rows("SELECT retrievable, COUNT(*) FROM resource GROUP BY retrievable"):
        print(f"  {ret:<10} {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="out")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("survey")
    p_img = sub.add_parser("images")
    p_img.add_argument("--limit", type=int, default=0)
    p_img.add_argument("--workers", type=int, default=8)
    p_img.add_argument("--resource")
    p_img.add_argument("--redownload", action="store_true")
    p_img.add_argument("--aoi", help="只下载 aoi.py 选中的影像（AoI 名称）")
    p_img.add_argument("--include-duplicates", action="store_true", help="包含重复航飞的资源")
    sub.add_parser("assets")
    sub.add_parser("dedupe")
    sub.add_parser("report")
    args = ap.parse_args()
    {"survey": cmd_survey, "images": cmd_images, "assets": cmd_assets, "report": cmd_report,
     "dedupe": cmd_dedupe}[args.cmd](args)


if __name__ == "__main__":
    main()
