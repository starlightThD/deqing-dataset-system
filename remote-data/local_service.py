#!/usr/bin/env python3
"""本地复刻服务（仅标准库）：把 out/catalog.db + 已落盘文件，伪装成与远端同构的 API。

实现远端实测的 3 个接口，让 read_data.py / mirror.py 等客户端**零改造**指向本地：

    POST /api/Login/Login                                → {"State":0,"Data":{"Token":…}}
    POST /api/cim/CIMScene/GetCIMSceneResourceByPage      → {"Total":N,"Rows":[42 字段…]}
    GET  /api/DownLoad/GetFileStream?<fastdfs键>          → 文件字节 / 204

启动：
    python3 local_service.py --port 18080 --password <本地测试密码>

验证：
    python3 read_data.py --base-url http://127.0.0.1:18080 --username admin
    python3 read_data.py --base-url http://127.0.0.1:18080 --username admin \
        --resource-id 4bb288ad-125f-4067-9b57-a19ba347efbf --page-size 3
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SECRET = b"local-replica-secret"
ISSUER = "http://localhost:8031/"


# ---------------- 数据层 ----------------
class Store:
    def __init__(self, outdir):
        self.out = outdir
        self.conn = sqlite3.connect(os.path.join(outdir, "catalog.db"), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.keys = {}
        self._index_files()
        self._resources = self._load_resources()

    def _load_resources(self):
        """优先用 out/resources/resources_all.json（42 字段全量），否则退回 catalog.db。"""
        jf = os.path.join(self.out, "resources", "resources_all.json")
        if os.path.exists(jf):
            with open(jf, encoding="utf-8") as f:
                data = json.load(f)
            rows = []
            for r in data.get("Rows", []):
                r = dict(r)
                r.pop("_meta", None)
                rows.append(r)
            rows.sort(key=lambda r: r.get("num") or 0)
            return rows
        rows = []
        for r in self.conn.execute(
                "SELECT id,label,name,type,type_name,big_resource_type,dataset_id,file_count,file_size,"
                "original_root_file_id,upload_time,data_production_time,epsg_id,srs_id,range_wkt,data_url,"
                "is_upload_completed,is_deleted,metadata_json FROM resource ORDER BY id"):
            rows.append({
                "Id": r["id"], "Label": r["label"], "Name": r["name"], "Type": r["type"],
                "TypeName": r["type_name"], "BigResourceType": r["big_resource_type"],
                "DataSetId": r["dataset_id"], "FileCount": r["file_count"], "FileSize": r["file_size"],
                "OriginalRootFileId": r["original_root_file_id"], "UploadTime": r["upload_time"],
                "DataProductionTime": r["data_production_time"], "EpsgId": r["epsg_id"],
                "SrsId": r["srs_id"], "Range": r["range_wkt"], "DataUrl": r["data_url"],
                "MetaData": r["metadata_json"],
                "IsUploadCompleted": bool(r["is_upload_completed"]),
                "IsDeleted": bool(r["is_deleted"]), "num": None,
            })
        return rows

    def _index_files(self):
        """建立 fastdfs 键 → 本地文件 的映射。"""
        c = self.conn
        # 1) 相机会员/快照/包围盒等小文件
        try:
            for r in c.execute("SELECT fastdfs_id, local_path FROM resource_asset WHERE local_path IS NOT NULL"):
                self.keys[r["fastdfs_id"]] = r["local_path"]
        except sqlite3.Error:
            pass
        # 2) 根索引文件
        for r in c.execute("SELECT id, original_root_file_id FROM resource WHERE original_root_file_id IS NOT NULL"):
            ext = "index.db3" if r["original_root_file_id"].lower().endswith(".db3") else "root.json"
            rel = os.path.join("indexes", r["id"], ext)
            if os.path.exists(os.path.join(self.out, rel)):
                self.keys[r["original_root_file_id"]] = rel
        # 3) 已下载影像
        for r in c.execute("SELECT fastdfs_id, local_path FROM photo WHERE local_path IS NOT NULL"):
            if r["fastdfs_id"]:
                self.keys[r["fastdfs_id"]] = r["local_path"]

    def resources(self):
        return [dict(r) for r in self._resources]

    def photos_of(self, resource_id):
        return self.conn.execute(
            "SELECT photo_id, width, height, fastdfs_id FROM photo WHERE resource_id=? ORDER BY photo_id",
            (resource_id,)).fetchall()

    def path_of(self, key):
        rel = self.keys.get(key)
        if not rel:
            return None
        p = os.path.join(self.out, rel)
        return p if os.path.exists(p) else None


# ---------------- 鉴权 ----------------
def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(user, tenant, user_id, days=3):
    now = int(time.time())
    auth_info = json.dumps({"UserId": user_id, "LoginRecordId": b64u(os.urandom(16)),
                            "TenantId": tenant, "ExpireDays": days, "Type": 1,
                            "CurrentToken": None, "Param": None, "IsSuperAdmin": False},
                           separators=(",", ":"))
    header = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = b64u(json.dumps({"nbf": now, "exp": now + days * 86400, "AuthInfo": auth_info,
                               "iss": ISSUER, "aud": ISSUER}, separators=(",", ":")).encode())
    sig = b64u(hmac.new(SECRET, f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{sig}"


def token_valid(tok):
    try:
        h, p, s = tok.split(".")
        want = b64u(hmac.new(SECRET, f"{h}.{p}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(want, s):
            return False
        pad = "=" * (-len(p) % 4)
        payload = json.loads(base64.urlsafe_b64decode(p + pad))
        return payload.get("exp", 0) > time.time()
    except Exception:  # noqa: BLE001
        return False


# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    store = None
    password_md5 = ""
    server_version = "LocalReplica/1.0"

    def log_message(self, fmt, *args):  # 安静一点
        if os.environ.get("LOCAL_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ---- 工具 ----
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, data, ctype="application/octet-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _no_content(self):
        self.send_response(204)
        self.end_headers()

    def _authed(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Bearer ") or not token_valid(h[7:].strip()):
            self._send_json({"State": 401, "Message": "请重新登录"}, 401)
            return False
        return True

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        if raw and "json" not in ctype:
            return None, 415
        try:
            return (json.loads(raw.decode("utf-8-sig")) if raw else {}), 200
        except Exception:  # noqa: BLE001
            return None, 400

    # ---- 路由 ----
    def do_POST(self):
        path = urlsplit(self.path).path.rstrip("/")
        if path == "/api/Login/Login":
            body, code = self._body()
            if body is None:
                return self._send_json({"State": code, "Message": "请求体不合法"}, code)
            pwd = str(body.get("Password") or "").lower()
            if str(body.get("UserName") or "") != "admin" or pwd != self.password_md5:
                return self._send_json({"State": 1, "Message": "账号或密码错误"}, 200)
            tok = make_token("admin", "00000000-0000-0000-0000-000000000001",
                             "00000000-0000-0000-0000-0000000000aa")
            return self._send_json({"State": 0, "Message": "登录成功",
                                    "Data": {"Token": tok, "UserName": "admin", "RealName": "admin",
                                             "AuthCodes": ["PLATFORM_MANAGE_ALL"]}})

        if not self._authed():
            return
        if path == "/api/cim/CIMScene/GetCIMSceneResourceByPage":
            body, code = self._body()
            if body is None:
                return self._send_json({"State": code, "Message": "请求体不合法"}, code)
            rows = self.store.resources()
            t = body.get("Type") or 0
            if t:
                rows = [r for r in rows if r["Type"] == t]
            ds = body.get("DataSetIds") or []
            if ds:
                rows = [r for r in rows if r["DataSetId"] in ds]
            big = body.get("BigResourceType") or []
            if big:
                rows = [r for r in rows if r["BigResourceType"] in big]
            label = (body.get("Label") or "").strip()
            if label:
                rows = [r for r in rows if label in (r["Label"] or "")]
            total = len(rows)
            pi = max(1, int(body.get("PageIndex") or 1))
            ps = int(body.get("PageSize") or 20)
            start = (pi - 1) * ps
            return self._send_json({"Total": total, "Rows": rows[start:start + ps]})

        if path == "/api/Resource/GetByPage":
            body, code = self._body()
            if body is None:
                return self._send_json({"State": code, "Message": "请求体不合法"}, code)
            rows = [{"Id": r["Id"], "Label": r["Label"], "Type": r["Type"], "TypeName": r["TypeName"],
                     "EpsgId": r["EpsgId"], "UploadTime": r["UploadTime"], "MetaData": None}
                    for r in self.store.resources()]
            pi = max(1, int(body.get("PageIndex") or 1))
            ps = int(body.get("PageSize") or 20)
            return self._send_json({"Total": len(rows), "Rows": rows[(pi - 1) * ps: (pi - 1) * ps + ps]})

        return self._send_json({"State": 404, "Message": "接口不存在"}, 404)

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path.rstrip("/") == "/api/DownLoad/GetFileStream":
            if not self._authed():
                return
            key = unquote(u.query)          # ← 整段查询串就是 FastDFS 键（与远端一致）
            p = self.store.path_of(key)
            if not p:
                return self._no_content()
            with open(p, "rb") as f:
                data = f.read()
            ext = os.path.splitext(key)[1].lower()
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".json": "application/json", ".xml": "application/xml",
                     ".db3": "application/octet-stream", ".mp4": "video/mp4"}.get(ext, "application/octet-stream")
            return self._send_bytes(data, ctype)
        if u.path.rstrip("/") == "/health":
            return self._send_json({"State": 0, "files": len(self.store.keys),
                                    "resources": len(self.store.resources())})
        return self._send_json({"State": 404, "Message": "接口不存在"}, 404)

    def do_PUT(self):
        return self._send_json({"State": 405, "Message": "方法不允许"}, 405)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--password", required=True, help="本地复刻服务使用的测试密码")
    ap.add_argument("--user", default="admin")
    args = ap.parse_args()

    Handler.store = Store(args.out)
    Handler.password_md5 = hashlib.md5(args.password.encode()).hexdigest()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"本地复刻服务已启动: http://{args.host}:{args.port}")
    print(f"  资源 {len(Handler.store.resources())} 个 | 可访问文件键 {len(Handler.store.keys)} 个")
    print(f"  账号 {args.user}（md5 鉴权，与远端一致）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")


if __name__ == "__main__":
    main()
