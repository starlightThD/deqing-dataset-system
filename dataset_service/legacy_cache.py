"""Read previously downloaded legacy API responses without network access."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi.responses import FileResponse, Response


MAX_BODY = 1024 * 1024


@dataclass
class CacheResult:
    status: int
    content_type: str = "application/json; charset=utf-8"
    body: bytes = b""
    file: Path | None = None
    source: str = "miss"

    def response(self):
        headers = {"X-Legacy-Cache": self.source, "Cache-Control": "private, no-store"}
        if self.file is not None:
            return FileResponse(self.file, status_code=self.status, media_type=self.content_type, headers=headers)
        return Response(self.body, status_code=self.status, media_type=self.content_type, headers=headers)


class LegacyCache:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "objects").mkdir(exist_ok=True, mode=0o700)
        self.db_path = self.root / "mirror.sqlite3"
        with self._db() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS http_cache (
                    cache_key TEXT PRIMARY KEY,
                    method TEXT NOT NULL,
                    target TEXT NOT NULL,
                    request_json TEXT,
                    status INTEGER NOT NULL,
                    content_type TEXT NOT NULL,
                    object_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS http_cache_target ON http_cache(target)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS scene (
                    id TEXT PRIMARY KEY,
                    ordinal INTEGER NOT NULL,
                    name TEXT,
                    created_at TEXT,
                    row_json TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS mirror_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        self.db_path.chmod(0o600)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _error(status: int, message: str) -> CacheResult:
        return CacheResult(status, body=json.dumps(
            {"State": status, "Message": message}, ensure_ascii=False
        ).encode())

    @staticmethod
    def _cache_key(method: str, target: str, body: bytes, content_type: str) -> str:
        key_body = body
        if body and "json" in content_type.lower():
            parsed = json.loads(body.decode("utf-8-sig"))
            key_body = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(method.encode() + b"\0" + target.encode() + b"\0" + key_body).hexdigest()

    def _cached(self, cache_key: str) -> CacheResult | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM http_cache WHERE cache_key=?", (cache_key,)).fetchone()
        if row is None:
            return None
        path = (self.root / "objects" / row["object_name"]).resolve()
        if not path.is_relative_to(self.root / "objects") or not path.is_file() or path.stat().st_size != row["size_bytes"]:
            return None
        return CacheResult(row["status"], row["content_type"], file=path, source="hit")

    def _scene_page(self, body: bytes) -> CacheResult | None:
        try:
            filters = json.loads(body) if body else {}
            if not isinstance(filters, dict) or filters.get("Tag"):
                return None
            page_index = max(1, int(filters.get("PageIndex") or 1))
            page_size = max(0, int(filters.get("PageSize", 20)))
        except (UnicodeError, ValueError, TypeError):
            return None
        with self._db() as db:
            complete = db.execute("SELECT value FROM mirror_meta WHERE key='scene_complete_total'").fetchone()
            total_meta = db.execute("SELECT value FROM mirror_meta WHERE key='scene_total'").fetchone()
            if complete is None and (total_meta is None or page_index != 1 or
                                     filters.get("Name") or filters.get("StartTime") or filters.get("EndTime")):
                return None
            rows = [json.loads(row["row_json"]) for row in db.execute(
                "SELECT row_json FROM scene ORDER BY ordinal"
            )]
        name = str(filters.get("Name") or "").strip()
        if name:
            rows = [row for row in rows if name in (row.get("Name") or "")]
        start_time = filters.get("StartTime")
        end_time = filters.get("EndTime")
        if (start_time is not None and not isinstance(start_time, str)) or (end_time is not None and not isinstance(end_time, str)):
            return None
        if start_time:
            rows = [row for row in rows if (row.get("CreateByTime") or "") >= start_time]
        if end_time:
            end_bound = end_time + "T23:59:59" if len(end_time) == 10 else end_time
            rows = [row for row in rows if (row.get("CreateByTime") or "") <= end_bound]
        start = (page_index - 1) * page_size
        total = len(rows) if complete is not None else int(total_meta["value"])
        payload = json.dumps(
            {"Total": total, "Rows": rows[start:start + page_size]},
            ensure_ascii=False, separators=(",", ":"),
        ).encode()
        return CacheResult(200, body=payload, source="catalog" if complete is not None else "partial")

    def get_cached(self, method: str, target: str, body: bytes = b"", content_type: str = "") -> CacheResult:
        if len(body) > MAX_BODY:
            return self._error(413, "请求体过大")
        path = target.partition("?")[0]
        if not (path.startswith("/api/") or path == "/geoserver" or path.startswith("/geoserver/")) \
                or target.startswith("//") or "\r" in target or "\n" in target:
            return self._error(400, "无效的旧接口路径")
        try:
            cache_key = self._cache_key(method, target, body, content_type)
            if method == "POST" and target == "/api/ArScene/GetByPage":
                scene_page = self._scene_page(body)
                if scene_page is not None:
                    return scene_page
            cached = self._cached(cache_key)
            if cached is not None:
                return cached
        except (UnicodeError, ValueError):
            return self._error(400, "JSON 请求体不合法")
        except (OSError, sqlite3.Error):
            return self._error(503, "本地镜像暂不可用")
        if target.startswith("/api/DownLoad/GetFileStream?"):
            return CacheResult(204)
        return CacheResult(404)

    def cached_targets(self, method: str, targets: list[str]) -> set[str]:
        """Return exact cached targets whose mirrored object still exists intact."""
        unique = list(dict.fromkeys(targets))
        if not unique:
            return set()
        object_root = (self.root / "objects").resolve()
        found: set[str] = set()
        for start in range(0, len(unique), 400):
            batch = unique[start:start + 400]
            placeholders = ",".join("?" for _ in batch)
            with self._db() as db:
                rows = db.execute(
                    f"SELECT target,object_name,size_bytes FROM http_cache "
                    f"WHERE method=? AND target IN ({placeholders})",
                    [method, *batch],
                ).fetchall()
            for row in rows:
                path = (object_root / row["object_name"]).resolve()
                if (path.is_relative_to(object_root) and path.is_file()
                        and path.stat().st_size == row["size_bytes"]):
                    found.add(row["target"])
        return found

    def list_entries(self, limit: int = 100) -> list[dict]:
        with self._db() as db:
            return [dict(row) for row in db.execute(
                "SELECT method,target,status,content_type,size_bytes,sha256,fetched_at "
                "FROM http_cache ORDER BY fetched_at DESC LIMIT ?", (limit,)
            )]
