"""Expose published versioned 3D Tiles through the legacy resource API."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import psycopg
from fastapi import HTTPException
from fastapi.responses import FileResponse

from .pointcloud import SCHEMA as POINTCLOUD_SCHEMA, _safe_path
from .spatial import SpatialStore, SpatialUnavailable


LOGGER = logging.getLogger(__name__)
MESH_RESOURCE_TYPE = 1000012
POINTCLOUD_RESOURCE_TYPE = 13000006
_EPSG_PATTERN = re.compile(r"(?:EPSG\s*:\s*)?(\d+)$", re.IGNORECASE)


class LegacyPointCloudBridge:
    """Add versioned 3D Tiles to the old catalog and serve their files by old IDs.

    The legacy frontend identifies the content category with its numeric resource
    type, while the versioned API stores the container format as ``3dtiles``.
    ``b3dm`` and ``pnts`` assets provide an unambiguous fallback when older
    versioned records do not yet declare ``content_kind`` metadata.
    """

    def __init__(self, store: SpatialStore, refresh_seconds: int = 60):
        self.store = store
        self.files_root = (store.data_dir / "pointcloud" / "files").resolve()
        self.refresh_seconds = max(5, refresh_seconds)
        self._lock = threading.RLock()
        self._expires_at = 0.0
        self._rows: list[dict] = []
        self._routes: dict[str, dict] = {}
        self._managed_ids: set[str] = set()

    @staticmethod
    def _resource_type(row: dict) -> int | None:
        resource_metadata = row.get("resource_metadata") or {}
        version_metadata = row.get("version_metadata") or {}
        for metadata in (resource_metadata, version_metadata):
            value = metadata.get("legacy_resource_type")
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = None
            if value in (MESH_RESOURCE_TYPE, POINTCLOUD_RESOURCE_TYPE):
                return value

            kind = str(metadata.get("content_kind") or metadata.get("data_kind") or "").strip().lower()
            if kind in {"mesh", "model", "terrain", "surface", "b3dm"}:
                return MESH_RESOURCE_TYPE
            if kind in {"pointcloud", "point_cloud", "point-cloud", "pnts"}:
                return POINTCLOUD_RESOURCE_TYPE

        has_mesh = row.get("b3dm_count", 0) > 0
        has_points = row.get("pnts_count", 0) > 0
        if has_mesh != has_points:
            return MESH_RESOURCE_TYPE if has_mesh else POINTCLOUD_RESOURCE_TYPE
        return None

    @staticmethod
    def _iso(value) -> str | None:
        return value.isoformat() if hasattr(value, "isoformat") else value

    @staticmethod
    def _catalog_row(row: dict, resource_type: int, ordinal: int) -> dict:
        resource_id = str(row["resource_id"])
        resource_metadata = row.get("resource_metadata") or {}
        version_metadata = row.get("version_metadata") or {}
        srs = str(row.get("srs") or "")
        legacy_origin = version_metadata.get("legacy_srs_origin") or resource_metadata.get("legacy_srs_origin")
        if not (isinstance(legacy_origin, list) and len(legacy_origin) == 3
                and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in legacy_origin)):
            legacy_origin = [0, 0, 0]
        epsg_match = _EPSG_PATTERN.fullmatch(srs.strip())
        epsg_id = int(epsg_match.group(1)) if epsg_match else 0
        content_kind = "pointcloud" if resource_type == POINTCLOUD_RESOURCE_TYPE else "mesh"
        metadata = {
            "RootFile": "tileset.json",
            "StorageFormat": "3dtiles",
            "ContentKind": content_kind,
            "PointCloudResourceId": resource_id,
            "PointCloudVersionId": str(row["version_id"]),
            "SRS": srs,
            # 3D Tiles already carries its world placement in the tileset
            # transform. The legacy frontend treats SRSOrigin as an extra
            # translation, so never feed it the projected source origin.
            "SRSOrigin": legacy_origin,
            "GeoreferenceMode": "tileset_transform",
        }
        if isinstance(resource_metadata.get("srs_origin"), list):
            metadata["SourceSRSOrigin"] = resource_metadata["srs_origin"]
        if resource_metadata.get("source_srs"):
            metadata["SourceSRS"] = resource_metadata["source_srs"]
        if isinstance(resource_metadata.get("anchor_wgs84"), dict):
            metadata["AnchorWGS84"] = resource_metadata["anchor_wgs84"]
        if version_metadata.get("partial"):
            metadata["Partial"] = True
            metadata["CompletenessScope"] = version_metadata.get("completeness_scope")

        label = str(row.get("name") or row.get("label") or resource_id)
        description = str(row.get("description") or "")
        version_label = str(row.get("label") or "")
        if version_label and version_label != label:
            description = f"{description}；发布版本：{version_label}" if description else f"发布版本：{version_label}"
        return {
            "Id": resource_id,
            "Label": label,
            "Name": label,
            "Description": description,
            "Type": resource_type,
            "TypeName": "PointCloud3Dtiles" if resource_type == POINTCLOUD_RESOURCE_TYPE else "Terrain3dtiles",
            "BigResourceType": 13 if resource_type == POINTCLOUD_RESOURCE_TYPE else 1,
            "DataSetId": str(resource_metadata.get("legacy_dataset_id") or resource_id),
            "FileSize": int(row.get("total_bytes") or 0),
            "FileCount": int(row.get("file_count") or 0),
            "EpsgId": epsg_id,
            "SrsId": None,
            "SrsDefinition": srs,
            "Range": row.get("spatial_extent_wkt"),
            "MetaData": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            "OriginalRootFileId": None,
            "DataUrl": None,
            "UploadTime": LegacyPointCloudBridge._iso(row.get("published_at")) or LegacyPointCloudBridge._iso(row.get("created_at")),
            "IsUploadCompleted": True,
            "IsDeleted": False,
            "IsPhysicalDeleted": False,
            "IsDisabled": False,
            "num": ordinal,
        }

    def _load_rows(self) -> tuple[list[dict], dict[str, dict]]:
        self.store.ensure_schema()
        with self.store.connect() as connection:
            connection.execute(POINTCLOUD_SCHEMA)
            records = connection.execute("""
                WITH latest AS (
                    SELECT DISTINCT ON (resource_id)
                           id,resource_id,label,entrypoint,metadata,total_bytes,file_count,
                           created_at,published_at
                    FROM pointcloud_version
                    WHERE status='published' AND format='3dtiles'
                    ORDER BY resource_id,published_at DESC NULLS LAST,created_at DESC,id DESC
                ), asset_kinds AS (
                    SELECT version_id,
                           count(*) FILTER (WHERE lower(relative_path) LIKE '%%.b3dm') AS b3dm_count,
                           count(*) FILTER (WHERE lower(relative_path) LIKE '%%.pnts') AS pnts_count
                    FROM pointcloud_asset
                    GROUP BY version_id
                )
                SELECT r.id AS resource_id,r.name,r.description,r.srs,r.metadata AS resource_metadata,
                       ST_AsText(r.spatial_extent) AS spatial_extent_wkt,
                       v.id AS version_id,v.label,v.entrypoint,v.metadata AS version_metadata,
                       v.total_bytes,v.file_count,v.created_at,v.published_at,
                       coalesce(k.b3dm_count,0) AS b3dm_count,
                       coalesce(k.pnts_count,0) AS pnts_count
                FROM latest v
                JOIN pointcloud_resource r ON r.id=v.resource_id
                LEFT JOIN asset_kinds k ON k.version_id=v.id
                ORDER BY r.name,r.id
            """).fetchall()

        rows: list[dict] = []
        routes: dict[str, dict] = {}
        for record in records:
            row = dict(record)
            # The legacy loaders request this fixed root path. Other entrypoint
            # layouts need an explicit root alias and are not advertised yet.
            if row.get("entrypoint") != "tileset.json":
                continue
            resource_type = self._resource_type(row)
            if resource_type is None:
                continue
            row_id = str(row["resource_id"])
            routes[row_id] = {
                "version_id": str(row["version_id"]),
                "resource_type": resource_type,
                "entrypoint": row["entrypoint"],
            }
            rows.append(self._catalog_row(row, resource_type, len(rows) + 1))
        return rows, routes

    def sync_resources(self, resources: list[dict]) -> None:
        """Refresh bridge rows in-place, retaining the legacy catalog on DB errors."""
        now = time.monotonic()
        with self._lock:
            if now >= self._expires_at:
                try:
                    rows, routes = self._load_rows()
                except (OSError, SpatialUnavailable, psycopg.Error) as error:
                    LOGGER.warning("Could not refresh versioned 3D Tiles in legacy catalog: %s", error)
                    self._expires_at = now + min(self.refresh_seconds, 10)
                else:
                    self._rows = rows
                    self._routes = routes
                    self._expires_at = now + self.refresh_seconds

            base = [row for row in resources if str(row.get("Id")) not in self._managed_ids]
            versioned_names = {str(row.get("Id")): row.get("Label") for row in self._rows}
            for row in base:
                label = versioned_names.get(str(row.get("Id")))
                if label is not None:
                    row["Label"] = label
                    if "Name" in row:
                        row["Name"] = label
            existing_ids = {str(row.get("Id")) for row in base}
            visible_rows = [dict(row) for row in self._rows if str(row.get("Id")) not in existing_ids]
            visible_ids = {str(row.get("Id")) for row in visible_rows}
            self._routes = {resource_id: route for resource_id, route in self._routes.items()
                            if resource_id in visible_ids}
            next_ordinal = max(
                (int(row.get("num") or 0) for row in base
                 if isinstance(row.get("num"), (int, float, str)) and str(row.get("num") or "0").isdigit()),
                default=0,
            ) + 1
            for offset, row in enumerate(visible_rows):
                row["num"] = next_ordinal + offset
            resources[:] = base + visible_rows
            self._managed_ids = visible_ids

    def file_response(self, resource_id: str, relative_path: str, resource_type: int):
        """Serve a registered published asset through the legacy Mesh/PointCload route."""
        with self._lock:
            route = self._routes.get(str(resource_id))
        if route is None or route["resource_type"] != resource_type:
            return None
        try:
            # Some OSGB exporters write harmless references such as
            # ``.//Data/Tile_x/tileset.json``. Collapse only empty and dot
            # segments; traversal, absolute paths and backslashes remain
            # rejected by this check and by ``_safe_path`` below.
            decoded_path = unquote(relative_path)
            if (not decoded_path or decoded_path.startswith("/") or "\\" in decoded_path
                    or "\x00" in decoded_path or any(part == ".." for part in decoded_path.split("/"))):
                raise ValueError("path contains an invalid segment")
            normalized_path = "/".join(
                part for part in decoded_path.split("/") if part not in ("", ".")
            )
            safe_path = _safe_path(normalized_path)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        if safe_path == "tileset.json":
            safe_path = route["entrypoint"]

        with self.store.connect() as connection:
            asset = connection.execute("""
                SELECT a.media_type,a.size_bytes
                FROM pointcloud_asset a
                JOIN pointcloud_version v ON v.id=a.version_id
                WHERE a.version_id=%s AND a.relative_path=%s AND v.resource_id=%s
                      AND v.status='published' AND v.format='3dtiles'
            """, (route["version_id"], safe_path, resource_id)).fetchone()
        if not asset:
            raise HTTPException(404, "published 3D Tiles asset not found")

        base = (self.files_root / str(resource_id) / route["version_id"]).resolve()
        path = (base / safe_path).resolve()
        if not path.is_relative_to(base):
            raise HTTPException(400, "invalid asset path")
        if not path.is_file() or path.stat().st_size != asset["size_bytes"]:
            raise HTTPException(503, "published 3D Tiles asset is missing or corrupt")
        return FileResponse(path, media_type=asset["media_type"], headers={"Cache-Control": "private, no-store"})
