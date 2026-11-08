"""Persistent camera-position subsets of mirrored ImageAirImage layers."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .legacy_subsets import SubsetError, canonical_geometry


MAX_IMAGES = 200


class LegacyObliqueSubsetStore:
    def __init__(self, data_dir: Path, legacy_store, spatial_store):
        self.path = data_dir / "legacy_oblique_subsets.sqlite3"
        self.legacy_store = legacy_store
        self.spatial_store = spatial_store
        data_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS oblique_subset (
                id TEXT PRIMARY KEY, source_resource_id TEXT NOT NULL,
                source_version TEXT NOT NULL, geometry_hash TEXT NOT NULL,
                geometry_json TEXT NOT NULL, resource_json TEXT NOT NULL,
                image_count INTEGER NOT NULL, created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_resource_id, source_version, geometry_hash)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS oblique_subset_image (
                subset_id TEXT NOT NULL REFERENCES oblique_subset(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, source_image_id TEXT NOT NULL,
                PRIMARY KEY(subset_id, ordinal)
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self) -> list[dict]:
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT resource_json FROM oblique_subset ORDER BY created_at,id")]

    def get(self, subset_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM oblique_subset WHERE id=?", (subset_id,)).fetchone()
        if row is None:
            return None
        return {"subset_resource_id": row["id"], "source_resource_id": row["source_resource_id"],
                "source_version": row["source_version"], "geometry": json.loads(row["geometry_json"]),
                "image_count": row["image_count"], "mode": "camera_centers_inside",
                "resource": json.loads(row["resource_json"]), "created_at": row["created_at"]}

    def count(self, subset_id: str) -> int | None:
        with self._db() as db:
            row = db.execute("SELECT image_count FROM oblique_subset WHERE id=?", (subset_id,)).fetchone()
        return int(row[0]) if row else None

    def page(self, subset_id: str, start: int, amount: int) -> list[dict] | None:
        if self.count(subset_id) is None:
            return None
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT source_image_id FROM oblique_subset_image WHERE subset_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                (subset_id, amount, start))]
        if not ids:
            return []
        with sqlite3.connect(self.legacy_store.arlayer_db) as db:
            records = {row[0]: json.loads(row[1]) for row in db.execute(
                f"SELECT image_id,record_json FROM arlayer_photo WHERE image_id IN ({','.join('?' for _ in ids)})", ids)}
        return [records[image_id] for image_id in ids if image_id in records]

    def create(self, source_id: str, geometry_value: dict, crs: str, name: str, creator: str) -> dict:
        try:
            source_id = str(uuid.UUID(source_id))
        except (ValueError, TypeError, AttributeError):
            raise SubsetError("source_resource_id 必须是资源 UUID") from None
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise SubsetError("name 长度应为1至100个字符")
        source = next((row for row in self.legacy_store.resources if str(row.get("Id")) == source_id), None)
        if source is None or int(source.get("Type") or 0) != 4000001:
            raise SubsetError("源资源必须是 ImageAirImage")
        if self.get(source_id) is not None:
            raise SubsetError("不能从已有倾斜影像子资源再次创建子资源")
        if self.legacy_store.arlayer_db is None:
            raise SubsetError("源倾斜影像定位记录未镜像")
        geometry = canonical_geometry(geometry_value, crs)
        encoded = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        geometry_hash = hashlib.sha256(encoded.encode()).hexdigest()
        with sqlite3.connect(self.legacy_store.arlayer_db) as db:
            source_row = db.execute("SELECT remote_total FROM arlayer_resource WHERE resource_id=? AND complete=1", (source_id,)).fetchone()
        if source_row is None:
            raise SubsetError("源倾斜影像定位记录未完整镜像")
        source_version = hashlib.sha256(json.dumps({"source": source_id, "count": source_row[0],
            "upload_time": source.get("UploadTime"), "index_mtime": self.legacy_store.arlayer_db.stat().st_mtime_ns},
            sort_keys=True).encode()).hexdigest()
        with self._db() as db:
            existing = db.execute("SELECT id FROM oblique_subset WHERE source_resource_id=? AND source_version=? AND geometry_hash=?",
                                  (source_id, source_version, geometry_hash)).fetchone()
        if existing:
            return {**self.get(existing[0]), "reused_existing": True}

        with self.spatial_store.connect() as db:
            rows = db.execute("""SELECT file_key FROM oblique_image
                WHERE resource_id=%s::uuid AND camera_center IS NOT NULL
                AND ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),camera_center)
                ORDER BY file_key LIMIT %s""", (source_id, encoded, MAX_IMAGES + 1)).fetchall()
        if not rows:
            raise SubsetError("范围内没有相机位置记录")
        if len(rows) > MAX_IMAGES:
            raise SubsetError(f"范围内超过 {MAX_IMAGES} 张照片，请缩小标绘范围")
        keys = [row["file_key"] for row in rows]
        with sqlite3.connect(self.legacy_store.arlayer_db) as db:
            mapping = {row[0]: row[1] for row in db.execute(
                f"SELECT fastdfs_id,image_id FROM arlayer_photo WHERE resource_id=? AND fastdfs_id IN ({','.join('?' for _ in keys)})",
                [source_id, *keys])}
        if any(key not in mapping for key in keys):
            raise SubsetError("部分相机照片缺少旧 ARLayer 定位记录")
        image_ids = [mapping[key] for key in keys]
        subset_id = str(uuid.uuid4())
        resource = copy.deepcopy(source)
        resource.update(Id=subset_id, Label=name, FileCount=len(image_ids), FileSize=0,
                        Description=f"航空倾斜相机位置子资源；来源 {source_id}；按相机中心落在区域内筛选")
        try:
            metadata = json.loads(resource.get("MetaData") or "{}")
        except (ValueError, TypeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["ResourceSubset"] = {"source_resource_id": source_id, "source_version": source_version,
            "mode": "camera_centers_inside", "image_count": len(image_ids)}
        resource["MetaData"] = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        resource["num"] = max((int(row.get("num") or 0) for row in self.legacy_store.resources), default=0) + 1
        created_at = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("""INSERT INTO oblique_subset
                (id,source_resource_id,source_version,geometry_hash,geometry_json,resource_json,image_count,created_by,created_at)
                VALUES (?,?,?,?,?,?,?,?,?)""", (subset_id, source_id, source_version, geometry_hash, encoded,
                json.dumps(resource, ensure_ascii=False, separators=(",", ":")), len(image_ids), creator, created_at))
            db.executemany("INSERT INTO oblique_subset_image(subset_id,ordinal,source_image_id) VALUES (?,?,?)",
                           [(subset_id, ordinal, image_id) for ordinal, image_id in enumerate(image_ids)])
        return {**self.get(subset_id), "reused_existing": False}

    def preview(self, source_id: str, geometry_value: dict, crs: str = "EPSG:4326") -> dict:
        """Count camera-center matches without writing a subset."""
        try:
            source_id = str(uuid.UUID(source_id))
        except (ValueError, TypeError, AttributeError):
            raise SubsetError("source_resource_id 必须是资源 UUID") from None
        source = next((row for row in self.legacy_store.resources if str(row.get("Id")) == source_id), None)
        if source is None or int(source.get("Type") or 0) != 4000001:
            raise SubsetError("源资源必须是 ImageAirImage")
        if self.legacy_store.arlayer_db is None:
            raise SubsetError("源倾斜影像定位记录未镜像")
        with sqlite3.connect(self.legacy_store.arlayer_db) as db:
            row = db.execute("SELECT remote_total FROM arlayer_resource WHERE resource_id=? AND complete=1", (source_id,)).fetchone()
        if row is None:
            raise SubsetError("源倾斜影像定位记录未完整镜像")
        geometry = canonical_geometry(geometry_value, crs)
        encoded = json.dumps(geometry, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.spatial_store.connect() as db:
            counts = db.execute("""SELECT
                    COUNT(*) FILTER (WHERE camera_center IS NOT NULL AND
                        ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),camera_center)) AS center_hits,
                    COUNT(*) FILTER (WHERE candidate_footprint IS NOT NULL AND
                        ST_Intersects(candidate_footprint,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))) AS footprint_candidates,
                    COUNT(*) FILTER (WHERE camera_center IS NULL) AS unlocated,
                    COUNT(*) FILTER (WHERE camera_center IS NOT NULL AND
                        ST_Covers(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),camera_center)
                        AND file_status='present') AS readable_center_hits
                    FROM oblique_image WHERE resource_id=%s::uuid""",
                (encoded, encoded, encoded, source_id)).fetchone()
        matches = int(counts["center_hits"] or 0)
        return {"source_resource_id": source_id, "geometry": geometry,
                "mode": "camera_centers_inside", "camera_center_hits": matches,
                "estimated_footprint_candidates": int(counts["footprint_candidates"] or 0),
                "unlocated_image_count": int(counts["unlocated"] or 0),
                "readable_center_hits": int(counts["readable_center_hits"] or 0),
                "within_creation_limit": matches <= MAX_IMAGES,
                "creation_limit": MAX_IMAGES,
                "precision_notice": "子资源只按相机中心筛选；估算覆盖候选不能保证入选，范围外相机拍到的内容会遗漏。"}
