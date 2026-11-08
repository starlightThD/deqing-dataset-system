#!/usr/bin/env python3
"""直接用账号密码读取数据；Python 3.11+，仅标准库，数据只在内存中使用。"""

import argparse
from contextlib import closing
import getpass
import hashlib
import http.client
import json
import math
import re
import sqlite3
import sys
from urllib.parse import quote, urlsplit
import urllib.error
import urllib.request


DEFAULT_BASE = "http://1jk412ww3207.vicp.fun:16761"
READ_LIMIT = 64 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def storage_key(value):
    if not isinstance(value, str):
        raise ValueError("缺少有效文件存储键")
    value = value.replace("\\", "/")
    if value.startswith("//"):
        parts = value[2:].split("/")
        if len(parts) < 5 or not parts[0] or not parts[1]:
            raise ValueError("无效 UNC 文件引用")
        value = "/".join(parts[2:])
    if not re.match(r"^group[0-9]+/M[0-9]{2}/", value):
        raise ValueError("未支持的文件存储键")
    if any(part in ("", ".", "..") for part in value.split("/")) or any(ord(c) < 32 for c in value):
        raise ValueError("文件存储键含无效路径")
    return value


class DataClient:
    def __init__(self, base_url=DEFAULT_BASE, timeout=50):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError("base_url 需为完整 HTTP/HTTPS 服务地址，不含凭据或查询参数")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout 必须为大于 0 的有限秒数")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = ""
        self._resources = {}
        self._catalogs = {}
        self._opener = urllib.request.build_opener(NoRedirect())

    def _read(self, path, body=None):
        if path != "/api/Login/Login" and not self._token:
            raise ValueError("请先调用 login(username, password)")
        headers = {"Accept-Encoding": "identity"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.status != 200:
                    raise ValueError(f"接口返回 HTTP {response.status}，未取得完整内容")
                expected = response.headers.get("Content-Length")
                if expected is not None and (int(expected) < 0 or int(expected) > READ_LIMIT):
                    raise ValueError("响应长度无效或超过 64 MiB 内存读取上限")
                raw = response.read(READ_LIMIT + 1)
                if len(raw) > READ_LIMIT:
                    raise ValueError("响应超过 64 MiB 内存读取上限")
                if expected is not None and len(raw) != int(expected):
                    raise ValueError("响应传输不完整：Content-Length 与实际字节数不符")
                prefix = raw.lstrip(b"\xef\xbb\xbf \t\r\n")
                if "text/html" in response.headers.get("Content-Type", "").lower() or prefix.lower().startswith((b"<!doctype html", b"<html")):
                    raise ValueError("服务返回了 HTML 页面，未取得数据")
                return raw
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            hints = {401: "请重新登录", 403: "当前账号无权访问", 404: "接口或文件不存在",
                     502: "上游服务暂不可用，请稍后重试", 503: "服务暂不可用，请稍后重试",
                     504: "上游服务响应超时，请稍后重试"}
            raise ValueError(f"请求失败：HTTP {code}，{hints.get(code, '请检查服务响应和请求参数')}") from None
        except urllib.error.URLError:
            raise ValueError("网络请求失败，请检查服务地址和连接") from None

    def _json(self, path, body=None):
        try:
            payload = json.loads(self._read(path, body).decode("utf-8-sig"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("接口未返回有效 JSON") from None
        if isinstance(payload, dict) and "State" in payload:
            state = payload["State"]
            if type(state) not in (int, float) or state != 0:
                raise ValueError("业务请求失败，请检查账号、权限和参数")
        return payload

    def login(self, username, password):
        # 切换账号时丢弃上一个账号的令牌和目录缓存。
        self._token = ""
        self._resources.clear()
        self._catalogs.clear()
        payload = self._json("/api/Login/Login", {
            "UserName": username, "Password": hashlib.md5(password.encode("utf-8")).hexdigest(),
        })
        data = payload.get("Data") if isinstance(payload, dict) else None
        token = data.get("Token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise ValueError("登录响应缺少 Data.Token")
        self._token = token.strip()

    @staticmethod
    def _page(page, page_size):
        if type(page) is not int or type(page_size) is not int or page < 1 or page_size < 1:
            raise ValueError("page 和 page_size 必须为正整数")

    def list_resources(self, page=1, page_size=20):
        self._page(page, page_size)
        payload = self._json("/api/cim/CIMScene/GetCIMSceneResourceByPage", {
            "BigResourceType": [], "DataSetIds": [], "Type": 0, "Label": "",
            "StartTime": None, "EndTime": None, "PageIndex": page, "PageSize": page_size,
        })
        if not isinstance(payload, dict) or not isinstance(payload.get("Rows"), list) or type(payload.get("Total")) is not int or payload["Total"] < 0:
            raise ValueError("资源列表响应缺少有效 Rows/Total")
        for row in payload["Rows"]:
            if not isinstance(row, dict) or row.get("Id") is None:
                raise ValueError("资源记录缺少 Id")
            self._resources[str(row["Id"])] = row
        return payload

    def _resource(self, resource_id):
        resource_id = str(resource_id)
        if resource_id in self._resources:
            return self._resources[resource_id]
        page, visited = 1, set()
        while True:
            result = self.list_resources(page=page, page_size=100)
            if resource_id in self._resources:
                return self._resources[resource_id]
            ids = {str(row["Id"]) for row in result["Rows"]}
            if not ids or len(visited | ids) >= result["Total"]:
                raise ValueError("当前账号可见的资源列表中未找到该 resource_id")
            if not ids - visited:
                raise ValueError("资源分页重复，无法继续定位资源")
            visited.update(ids)
            page += 1

    def _catalog(self, resource_id):
        resource_id = str(resource_id)
        if resource_id in self._catalogs:
            return self._catalogs[resource_id]
        if not hasattr(sqlite3.Connection, "deserialize"):
            raise ValueError("全内存解析 DB3 需要支持 sqlite3.deserialize 的 Python 3.11 或以上版本")
        row = self._resource(resource_id)
        if row.get("Type") != 4000001:
            raise ValueError("照片目录目前仅支持航空倾斜影像 Type=4000001")
        key = storage_key(row.get("OriginalRootFileId"))
        if not key.lower().endswith(".db3"):
            raise ValueError("资源未提供可解析的 DB3 索引")
        raw = self._read("/api/DownLoad/GetFileStream?" + quote(key, safe="/"))
        if not raw.startswith(b"SQLite format 3\x00"):
            raise ValueError("服务未返回有效的 SQLite 影像索引")
        try:
            with closing(sqlite3.connect(":memory:")) as db:
                db.execute("PRAGMA temp_store=MEMORY")
                db.execute("PRAGMA trusted_schema=OFF")
                db.deserialize(raw)
                db.execute("PRAGMA query_only=ON")
                columns = {entry[1] for entry in db.execute("PRAGMA table_info(Photo)")}
                if not {"Id", "PhotoFile", "Width", "Height"}.issubset(columns):
                    raise ValueError("索引 Photo 表缺少 Id/PhotoFile/Width/Height")
                photos = db.execute("SELECT Id, PhotoFile, Width, Height FROM Photo ORDER BY Id").fetchall()
        except sqlite3.Error:
            raise ValueError("SQLite 影像索引无法在内存中解析") from None
        items, identifiers = [], set()
        for identifier, photo_file, width, height in photos:
            if type(identifier) is not int or identifier < 0 or identifier in identifiers:
                raise ValueError("索引照片 Id 无效或重复")
            identifiers.add(identifier)
            file_id = storage_key(photo_file)
            dimensions = [int(v) if isinstance(v, (int, float)) and math.isfinite(v) and v > 0 else None
                          for v in (width, height)]
            items.append({"id": str(identifier), "width": dimensions[0], "height": dimensions[1], "fileId": file_id})
        catalog = {"resourceId": resource_id, "name": row.get("Label", ""), "items": items}
        self._catalogs[resource_id] = catalog
        return catalog

    def list_images(self, resource_id, page=1, page_size=20):
        self._page(page, page_size)
        catalog = self._catalog(resource_id)
        start = (page - 1) * page_size
        return {"resourceId": catalog["resourceId"], "name": catalog["name"], "total": len(catalog["items"]),
                "page": page, "pageSize": page_size,
                "items": [dict(item) for item in catalog["items"][start:start + page_size]]}

    def read_image(self, resource_id, image_id):
        catalog = self._catalog(resource_id)
        item = next((p for p in catalog["items"] if p["id"] == str(image_id)), None)
        if item is None:
            raise ValueError("资源索引中未找到指定照片 Id")
        raw = self._read("/api/DownLoad/GetFileStream?" + quote(item["fileId"], safe="/"))
        if not raw.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n")):
            raise ValueError("服务未返回有效 JPEG/PNG 图片")
        return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="登录密码；省略时隐藏输入")
    parser.add_argument("--resource-id", help="航空影像资源 Id；省略则查询资源列表")
    parser.add_argument("--image-id", help="将指定照片读入内存，仅打印摘要")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--page-size", type=int, default=20)
    args = parser.parse_args()
    if args.image_id is not None and args.resource_id is None:
        parser.error("--image-id 需与 --resource-id 一起使用")
    DataClient._page(args.page, args.page_size)
    password = args.password if args.password is not None else getpass.getpass("登录密码（输入不显示）：")
    client = DataClient(args.base_url)
    client.login(args.username, password)
    if args.image_id is not None:
        content = client.read_image(args.resource_id, args.image_id)
        result = {"resourceId": args.resource_id, "imageId": args.image_id, "byteLength": len(content),
                  "mime": "image/jpeg" if content.startswith(b"\xff\xd8\xff") else "image/png",
                  "savedToDisk": False}
    elif args.resource_id is not None:
        result = client.list_images(args.resource_id, args.page, args.page_size)
    else:
        result = client.list_resources(args.page, args.page_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消", file=sys.stderr)
        sys.exit(2)
    except (ValueError, OSError, http.client.HTTPException) as exc:
        print("[失败] " + str(exc), file=sys.stderr)
        sys.exit(1)
