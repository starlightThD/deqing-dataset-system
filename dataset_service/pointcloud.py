"""Admin-managed point-cloud catalog, resumable uploads, and published asset reads."""

import hashlib
import json
import math
import mimetypes
import os
import posixpath
import re
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import quote, unquote, urlsplit

import psycopg
from psycopg.types.json import Jsonb
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from pyproj import CRS

from .spatial import SpatialStore, SpatialUnavailable
from .location_review import confirmed_location_review


SCHEMA = """
CREATE TABLE IF NOT EXISTS pointcloud_resource (
 id uuid PRIMARY KEY, name text NOT NULL, description text NOT NULL DEFAULT '',
 srs text NOT NULL DEFAULT '', metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
 spatial_extent geometry(Geometry,4326),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE pointcloud_resource ADD COLUMN IF NOT EXISTS spatial_extent geometry(Geometry,4326);
CREATE INDEX IF NOT EXISTS pointcloud_resource_spatial_extent_gix
 ON pointcloud_resource USING gist(spatial_extent) WHERE spatial_extent IS NOT NULL;
CREATE TABLE IF NOT EXISTS pointcloud_taxonomy (
 id uuid PRIMARY KEY, resource_id uuid NOT NULL REFERENCES pointcloud_resource(id) ON DELETE CASCADE,
 version integer NOT NULL, name text NOT NULL, classes jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(resource_id,version)
);
CREATE TABLE IF NOT EXISTS pointcloud_version (
 id uuid PRIMARY KEY, resource_id uuid NOT NULL REFERENCES pointcloud_resource(id) ON DELETE CASCADE,
 label text NOT NULL, format text NOT NULL CHECK(format IN ('ply','3dtiles')),
 purpose text NOT NULL CHECK(purpose IN ('source','segmentation')),
 status text NOT NULL CHECK(status IN ('uploading','published','failed')),
 entrypoint text NOT NULL, source_version_id uuid REFERENCES pointcloud_version(id),
 taxonomy_id uuid REFERENCES pointcloud_taxonomy(id), job_id uuid,
 metadata jsonb NOT NULL DEFAULT '{}'::jsonb, total_bytes bigint NOT NULL DEFAULT 0,
 file_count integer NOT NULL DEFAULT 0, manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz,
 UNIQUE(resource_id,label)
);
CREATE INDEX IF NOT EXISTS pointcloud_version_resource_idx ON pointcloud_version(resource_id,created_at DESC);
CREATE TABLE IF NOT EXISTS pointcloud_upload (
 id uuid PRIMARY KEY, version_id uuid NOT NULL REFERENCES pointcloud_version(id) ON DELETE CASCADE,
 relative_path text NOT NULL, expected_size bigint NOT NULL CHECK(expected_size >= 0),
 expected_sha256 text NOT NULL, chunk_size integer NOT NULL CHECK(chunk_size > 0),
 status text NOT NULL CHECK(status IN ('uploading','complete')),
 created_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
 UNIQUE(version_id,relative_path)
);
CREATE TABLE IF NOT EXISTS pointcloud_upload_chunk (
 upload_id uuid NOT NULL REFERENCES pointcloud_upload(id) ON DELETE CASCADE,
 chunk_index integer NOT NULL, size_bytes integer NOT NULL, sha256 text NOT NULL,
 PRIMARY KEY(upload_id,chunk_index)
);
CREATE TABLE IF NOT EXISTS pointcloud_asset (
 version_id uuid NOT NULL REFERENCES pointcloud_version(id) ON DELETE CASCADE,
 relative_path text NOT NULL, size_bytes bigint NOT NULL, sha256 text NOT NULL,
 media_type text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(version_id,relative_path)
);
CREATE TABLE IF NOT EXISTS pointcloud_job (
 id uuid PRIMARY KEY, resource_id uuid NOT NULL REFERENCES pointcloud_resource(id) ON DELETE CASCADE,
 source_version_id uuid NOT NULL REFERENCES pointcloud_version(id), taxonomy_id uuid REFERENCES pointcloud_taxonomy(id),
 algorithm text NOT NULL, algorithm_version text NOT NULL DEFAULT '', parameters jsonb NOT NULL DEFAULT '{}'::jsonb,
 status text NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled')),
 progress double precision NOT NULL DEFAULT 0 CHECK(progress >= 0 AND progress <= 100),
 output_version_id uuid REFERENCES pointcloud_version(id), error text NOT NULL DEFAULT '',
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pointcloud_job_resource_idx ON pointcloud_job(resource_id,created_at DESC);
"""

MAX_FILE_BYTES = int(os.getenv("DEQING_POINTCLOUD_MAX_FILE_BYTES", str(24 * 1024**3)))
MAX_VERSION_BYTES = int(os.getenv("DEQING_POINTCLOUD_MAX_VERSION_BYTES", str(100 * 1024**3)))
MAX_CHUNK_BYTES = int(os.getenv("DEQING_POINTCLOUD_MAX_CHUNK_BYTES", str(16 * 1024**2)))
MIN_CHUNK_BYTES = 64 * 1024
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[int] = set()


class ResourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    srs: str = Field(min_length=1, max_length=500)
    metadata: dict = Field(default_factory=dict)
    spatial_extent: dict
    location_review: dict


class ResourcePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    srs: str | None = Field(default=None, min_length=1, max_length=500)
    metadata: dict | None = None
    spatial_extent: dict | None = None
    location_review: dict | None = None


def _review_location(value: dict | None, source_srs: str, extent_sha256: str, reviewer: str) -> dict:
    if not isinstance(value, dict) or value.get("confirmed") is not True:
        raise HTTPException(422, "location_review.confirmed must be true after reviewing the source CRS and WGS84 extent")
    evidence = value.get("evidence")
    extent_method = value.get("extent_method")
    coordinate_order = value.get("extent_coordinate_order")
    review_srs = value.get("source_crs")
    if not isinstance(evidence, str) or len(evidence.strip()) < 5 or len(evidence) > 500:
        raise HTTPException(422, "location_review.evidence must identify the inspected source or documentation")
    if not isinstance(extent_method, str) or len(extent_method.strip()) < 3 or len(extent_method) > 200:
        raise HTTPException(422, "location_review.extent_method must describe how the WGS84 extent was obtained")
    if coordinate_order != "longitude,latitude":
        raise HTTPException(422, "location_review.extent_coordinate_order must be longitude,latitude")
    try:
        if not isinstance(review_srs, str) or CRS.from_user_input(review_srs) != CRS.from_user_input(source_srs):
            raise ValueError("CRS differs")
    except Exception as error:
        raise HTTPException(422, "location_review.source_crs must match the resource source CRS") from error
    return confirmed_location_review(
        source_crs=CRS.from_user_input(source_srs).to_string(), evidence=evidence,
        scope=["source_crs", "extent_geometry", "extent_coordinate_order", "extent_semantics"],
        reviewer=reviewer,
    ) | {"extent_method": extent_method.strip(), "extent_sha256": extent_sha256}


class VersionCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    format: str = Field(pattern="^(ply|3dtiles)$")
    purpose: str = Field(default="source", pattern="^(source|segmentation)$")
    entrypoint: str | None = Field(default=None, max_length=500)
    source_version_id: uuid.UUID | None = None
    taxonomy_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    metadata: dict = Field(default_factory=dict)
    location_review: dict


class FileInit(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    chunk_size: int = Field(default=8 * 1024**2, ge=MIN_CHUNK_BYTES, le=MAX_CHUNK_BYTES)


class PointClass(BaseModel):
    id: int = Field(ge=0, le=65535)
    code: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=200)
    color: str = Field(pattern="^#[0-9a-fA-F]{6}$")
    description: str = Field(default="", max_length=1000)


class TaxonomyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    classes: list[PointClass] = Field(min_length=1, max_length=1024)


class JobCreate(BaseModel):
    source_version_id: uuid.UUID
    taxonomy_id: uuid.UUID | None = None
    algorithm: str = Field(min_length=1, max_length=200)
    algorithm_version: str = Field(default="", max_length=100)
    parameters: dict = Field(default_factory=dict)


class JobPatch(BaseModel):
    status: str = Field(pattern="^(queued|running|failed|cancelled)$")
    progress: float = Field(default=0, ge=0, le=100)
    output_version_id: uuid.UUID | None = None
    error: str = Field(default="", max_length=4000)


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("path must be a relative POSIX path")
    decoded = unquote(value)
    if decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        raise ValueError("absolute paths are not allowed")
    pieces = decoded.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise ValueError("path contains an invalid segment")
    if any(ord(char) < 32 for char in decoded):
        raise ValueError("path contains control characters")
    return "/".join(pieces)


def _jsonable(row):
    result = dict(row)
    for key, value in list(result.items()):
        if isinstance(value, uuid.UUID):
            result[key] = str(value)
        elif isinstance(value, datetime):
            result[key] = value.isoformat()
    return result


def _encode_spatial_extent(value: dict | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise HTTPException(422, "spatial_extent must be a GeoJSON geometry object")
    if value["type"] not in {"Polygon", "MultiPolygon"}:
        raise HTTPException(422, "spatial_extent must be a Polygon or MultiPolygon coverage area")

    def validate_coordinates(node):
        if not isinstance(node, list) or not node:
            raise ValueError("spatial_extent coordinates must be nonempty arrays")
        if isinstance(node[0], (int, float)) and not isinstance(node[0], bool):
            if len(node) < 2 or any(not isinstance(item, (int, float)) or isinstance(item, bool) for item in node[:2]):
                raise ValueError("spatial_extent coordinates must contain longitude and latitude")
            lon, lat = float(node[0]), float(node[1])
            if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
                raise ValueError("spatial_extent coordinates must be WGS84 longitude,latitude")
            return
        for child in node:
            validate_coordinates(child)

    try:
        validate_coordinates(value.get("coordinates"))
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise HTTPException(422, str(error) or "spatial_extent must contain finite JSON values") from error
    if len(encoded.encode("utf-8")) > 256 * 1024:
        raise HTTPException(413, "spatial_extent is too large")
    return encoded


def _wgs84_bbox(value: str) -> list[float]:
    try:
        bounds = [float(part) for part in value.split(",")]
    except ValueError as error:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat") from error
    if (len(bounds) != 4 or not all(math.isfinite(part) for part in bounds)
            or not (-180 <= bounds[0] < bounds[2] <= 180)
            or not (-90 <= bounds[1] < bounds[3] <= 90)):
        raise HTTPException(422, "bbox must be a valid EPSG:4326 extent")
    return bounds


def _check_spatial_extent(connection, encoded: str | None) -> None:
    if encoded is None:
        return
    result = connection.execute("""SELECT ST_IsValid(g) AS valid,ST_IsEmpty(g) AS empty
        FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s),4326) AS g) AS input""", (encoded,)).fetchone()
    if not result["valid"] or result["empty"]:
        raise HTTPException(422, "spatial_extent must be a nonempty valid geometry")


def _new_spatial_extent_hash(connection, encoded: str) -> str:
    row = connection.execute("""SELECT ST_AsEWKB(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) AS wkb""",
                             (encoded,)).fetchone()
    return hashlib.sha256(bytes(row["wkb"])).hexdigest()


def _stored_spatial_extent_hash(wkb) -> str:
    return hashlib.sha256(bytes(wkb)).hexdigest()


def router(store: SpatialStore, reader: Callable, administrator: Callable) -> APIRouter:
    api = APIRouter(prefix="/pointcloud", tags=["point-cloud"])
    root = (store.data_dir / "pointcloud").resolve()
    files_root = root / "files"
    uploads_root = root / ".uploads"

    def ready():
        marker = id(store)
        if marker in _SCHEMA_READY:
            return
        with _SCHEMA_LOCK:
            if marker in _SCHEMA_READY:
                return
            try:
                store.ensure_schema()
                with store.connect() as connection:
                    connection.execute(SCHEMA)
            except (SpatialUnavailable, psycopg.Error) as error:
                raise HTTPException(503, "point-cloud database is unavailable") from error
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            uploads_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            _SCHEMA_READY.add(marker)

    def version_dir(resource_id: uuid.UUID, version_id: uuid.UUID) -> Path:
        base = (files_root / str(resource_id) / str(version_id)).resolve()
        if not base.is_relative_to(files_root):
            raise HTTPException(400, "invalid storage path")
        return base

    def draft_version(connection, version_id: uuid.UUID):
        row = connection.execute("SELECT * FROM pointcloud_version WHERE id=%s", (version_id,)).fetchone()
        if not row:
            raise HTTPException(404, "point-cloud version not found")
        if row["status"] != "uploading":
            raise HTTPException(409, "version is immutable after publication")
        return row

    def validate_taxonomy(connection, resource_id, taxonomy_id):
        if taxonomy_id is None:
            return
        if not connection.execute("SELECT 1 FROM pointcloud_taxonomy WHERE id=%s AND resource_id=%s",
                                  (taxonomy_id, resource_id)).fetchone():
            raise HTTPException(422, "taxonomy does not belong to this resource")

    # These low-level ingestion routes remain available to the package adapter
    # and existing clients, but are intentionally omitted from public Swagger.
    @api.post("/resources", status_code=201, include_in_schema=False)
    def create_resource(body: ResourceCreate, reviewer: Annotated[str, Depends(administrator)]):
        ready()
        try:
            source_crs = CRS.from_user_input(body.srs.strip())
        except Exception as error:
            raise HTTPException(422, "srs must identify a valid source coordinate reference system") from error
        extent_status = body.metadata.get("spatial_extent_status")
        if body.spatial_extent is None:
            raise HTTPException(422, "a trusted WGS84 spatial_extent is required before a resource can enter the map catalog")
        if extent_status not in (None, "declared_wgs84"):
            raise HTTPException(422, "metadata.spatial_extent_status must be declared_wgs84")
        content_kind = body.metadata.get("content_kind")
        if content_kind is not None and content_kind not in {"pointcloud", "mesh", "model", "terrain"}:
            raise HTTPException(422, "metadata.content_kind must be pointcloud, mesh, model or terrain")
        if "location_review" in body.metadata:
            raise HTTPException(422, "location_review must be submitted in the top-level field, not metadata")
        resource_id = uuid.uuid4()
        encoded_extent = _encode_spatial_extent(body.spatial_extent)
        with store.connect() as connection:
            try:
                _check_spatial_extent(connection, encoded_extent)
                review = _review_location(body.location_review, source_crs.to_string(),
                                          _new_spatial_extent_hash(connection, encoded_extent), reviewer)
                metadata = {**body.metadata, "spatial_extent_status": "declared_wgs84", "location_review": review}
                row = connection.execute("""INSERT INTO pointcloud_resource(id,name,description,srs,metadata,spatial_extent)
                    VALUES(%s,%s,%s,%s,%s,
                      ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                    RETURNING id,name,description,srs,metadata,ST_AsGeoJSON(spatial_extent)::json AS spatial_extent,
                              created_at,updated_at""",
                    (resource_id, body.name.strip(), body.description, body.srs, Jsonb(metadata),
                     encoded_extent)).fetchone()
            except psycopg.errors.DataException as error:
                raise HTTPException(422, "invalid spatial_extent GeoJSON") from error
        return _jsonable(row)

    @api.get("/spatial-resources")
    def query_spatial_resources(
        principal: Annotated[str, Depends(reader)],
        bbox: str,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        """Find point-cloud resources whose registered extent intersects a WGS84 bbox."""
        ready()
        bounds = _wgs84_bbox(bbox)
        envelope = "ST_MakeEnvelope(%s,%s,%s,%s,4326)"
        visibility = "" if principal == "admin" else (
            "AND EXISTS (SELECT 1 FROM pointcloud_version v WHERE v.resource_id=r.id AND v.status='published')"
        )
        params = [*bounds, *bounds]
        condition = (
            "r.spatial_extent IS NOT NULL "
            f"AND r.spatial_extent && {envelope} "
            f"AND ST_Intersects(r.spatial_extent,{envelope}) {visibility}"
        )
        with store.connect() as connection:
            total = connection.execute(
                f"SELECT count(*) AS n FROM pointcloud_resource r WHERE {condition}", params,
            ).fetchone()["n"]
            rows = connection.execute(
                f"""SELECT r.id,r.name,r.description,r.srs,r.metadata,
                           ST_AsGeoJSON(r.spatial_extent)::json AS spatial_extent,r.created_at,r.updated_at
                    FROM pointcloud_resource r WHERE {condition}
                    ORDER BY r.updated_at DESC,r.id LIMIT %s OFFSET %s""",
                params + [limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = _jsonable(row)
                versions = connection.execute("""SELECT id,label,format,purpose,status,entrypoint,total_bytes,file_count,published_at
                    FROM pointcloud_version WHERE resource_id=%s AND (%s OR status='published') ORDER BY created_at DESC""",
                    (row["id"], principal == "admin")).fetchall()
                item["versions"] = []
                for version in versions:
                    version_item = _jsonable(version)
                    version_id = version_item["id"]
                    entrypoint = quote(version_item["entrypoint"], safe="/")
                    version_item["manifest_url"] = f"/pointcloud/versions/{version_id}/manifest"
                    version_item["entrypoint_url"] = f"/pointcloud/versions/{version_id}/files/{entrypoint}"
                    item["versions"].append(version_item)
                items.append(item)
        return {
            "bbox": bounds, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(items) < total, "items": items,
            "notice": "按资源登记的 WGS84 范围筛选；匹配后返回该资源版本入口，不会裁切 PLY 或 3D Tiles 内容。",
        }

    @api.get("/resources")
    def list_resources(principal: Annotated[str, Depends(reader)], limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        ready()
        visibility = "" if principal == "admin" else "WHERE EXISTS (SELECT 1 FROM pointcloud_version v WHERE v.resource_id=r.id AND v.status='published')"
        with store.connect() as connection:
            total = connection.execute(f"SELECT count(*) AS n FROM pointcloud_resource r {visibility}").fetchone()["n"]
            rows = connection.execute(f"""SELECT r.id,r.name,r.description,r.srs,r.metadata,
                ST_AsGeoJSON(r.spatial_extent)::json AS spatial_extent,r.created_at,r.updated_at
                FROM pointcloud_resource r {visibility} ORDER BY r.updated_at DESC,r.id LIMIT %s OFFSET %s""",
                                      (limit, offset)).fetchall()
        items = []
        for row in rows:
            item = _jsonable(row)
            with store.connect() as connection:
                item["versions"] = connection.execute("""SELECT id,label,format,purpose,status,entrypoint,total_bytes,file_count,published_at
                    FROM pointcloud_version WHERE resource_id=%s AND (%s OR status='published') ORDER BY created_at DESC""",
                    (row["id"], principal == "admin")).fetchall()
            item["versions"] = [_jsonable(v) for v in item["versions"]]
            items.append(item)
        return {"total": total, "limit": limit, "offset": offset, "items": items}

    @api.get("/resources/{resource_id}")
    def get_resource(resource_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            row = connection.execute("""SELECT id,name,description,srs,metadata,
                ST_AsGeoJSON(spatial_extent)::json AS spatial_extent,created_at,updated_at
                FROM pointcloud_resource WHERE id=%s""", (resource_id,)).fetchone()
            if not row:
                raise HTTPException(404, "point-cloud resource not found")
            if principal != "admin" and not connection.execute(
                "SELECT 1 FROM pointcloud_version WHERE resource_id=%s AND status='published' LIMIT 1", (resource_id,)
            ).fetchone():
                raise HTTPException(404, "point-cloud resource not found")
            result = _jsonable(row)
            result["versions"] = [_jsonable(v) for v in connection.execute("""SELECT id,label,format,purpose,status,entrypoint,
                source_version_id,taxonomy_id,job_id,metadata,total_bytes,file_count,created_at,published_at
                FROM pointcloud_version WHERE resource_id=%s AND (%s OR status='published') ORDER BY created_at DESC""",
                (resource_id, principal == "admin")).fetchall()]
        return result

    @api.patch("/resources/{resource_id}")
    def patch_resource(resource_id: uuid.UUID, body: ResourcePatch, reviewer: Annotated[str, Depends(administrator)]):
        ready()
        fields = body.model_dump(exclude_unset=True)
        if not fields:
            raise HTTPException(400, "no fields supplied")
        if "location_review" in fields and fields["location_review"] is None:
            raise HTTPException(422, "location_review cannot be cleared")
        if "metadata" in fields and fields["metadata"] is not None and "location_review" in fields["metadata"]:
            raise HTTPException(422, "location_review is server-managed and must use the top-level review field")
        spatial_change = "srs" in fields or "spatial_extent" in fields
        if "location_review" in fields and not spatial_change:
            raise HTTPException(422, "a location review must accompany a source CRS or extent review")
        if spatial_change and "location_review" not in fields:
            raise HTTPException(422, "changing the source CRS or WGS84 extent requires a new location review")
        if "spatial_extent" in fields and fields["spatial_extent"] is None:
            raise HTTPException(422, "spatial_extent cannot be cleared; a map resource must retain a reviewed location")
        with store.connect() as connection:
            existing = connection.execute("SELECT srs,metadata,ST_AsEWKB(spatial_extent) AS extent_wkb FROM pointcloud_resource WHERE id=%s FOR UPDATE",
                                          (resource_id,)).fetchone()
            if not existing:
                raise HTTPException(404, "point-cloud resource not found")
            source_srs = fields.get("srs") or existing["srs"]
            encoded_extent = _encode_spatial_extent(fields["spatial_extent"]) if "spatial_extent" in fields else None
            if spatial_change and encoded_extent is None and existing["extent_wkb"] is None:
                raise HTTPException(422, "provide a reviewed WGS84 polygon before updating this resource location")
            metadata = dict(existing["metadata"] or {})
            if fields.get("metadata") is not None:
                patch_metadata = fields["metadata"]
                status = patch_metadata.get("spatial_extent_status", metadata.get("spatial_extent_status"))
                if status not in (None, "declared_wgs84"):
                    raise HTTPException(422, "metadata.spatial_extent_status must be declared_wgs84")
                metadata.update(patch_metadata)
            metadata["spatial_extent_status"] = "declared_wgs84"
            if spatial_change:
                published_versions = connection.execute(
                    "SELECT count(*) AS n FROM pointcloud_version WHERE resource_id=%s AND status='published'",
                    (resource_id,),
                ).fetchone()["n"]
                if published_versions:
                    raise HTTPException(409, "location is immutable after a version is published; create a new resource for a changed footprint")
                try:
                    CRS.from_user_input(source_srs)
                except Exception as error:
                    raise HTTPException(422, "srs must identify a valid source coordinate reference system") from error
                extent_hash = (_new_spatial_extent_hash(connection, encoded_extent) if encoded_extent is not None
                               else _stored_spatial_extent_hash(existing["extent_wkb"]))
                metadata["location_review"] = _review_location(fields["location_review"], source_srs, extent_hash, reviewer)
            values = {"name": fields.get("name"), "description": fields.get("description"),
                      "srs": fields.get("srs"), "metadata": Jsonb(metadata)}
            if values["name"] is not None:
                values["name"] = values["name"].strip()
            extent_sql = "spatial_extent=spatial_extent"
            if "spatial_extent" in fields:
                extent_sql = "spatial_extent=ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)"
            try:
                _check_spatial_extent(connection, encoded_extent)
                query_params = [values["name"], values["description"], values["srs"], values["metadata"]]
                if "spatial_extent" in fields:
                    query_params.append(encoded_extent)
                query_params.append(resource_id)
                row = connection.execute(f"""UPDATE pointcloud_resource SET
                    name=COALESCE(%s,name),description=COALESCE(%s,description),srs=COALESCE(%s,srs),
                    metadata=COALESCE(%s,metadata),{extent_sql},updated_at=now() WHERE id=%s
                    RETURNING id,name,description,srs,metadata,ST_AsGeoJSON(spatial_extent)::json AS spatial_extent,
                              created_at,updated_at""", query_params).fetchone()
            except psycopg.errors.DataException as error:
                raise HTTPException(422, "invalid spatial_extent GeoJSON") from error
        if not row:
            raise HTTPException(404, "point-cloud resource not found")
        return _jsonable(row)

    @api.post("/resources/{resource_id}/taxonomies", status_code=201)
    def create_taxonomy(resource_id: uuid.UUID, body: TaxonomyCreate, _: Annotated[str, Depends(administrator)]):
        ready()
        classes = [item.model_dump() for item in body.classes]
        if len({item["id"] for item in classes}) != len(classes) or len({item["code"] for item in classes}) != len(classes):
            raise HTTPException(422, "class ids and codes must be unique")
        taxonomy_id = uuid.uuid4()
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s",(resource_id,)).fetchone():
                raise HTTPException(404,"point-cloud resource not found")
            connection.execute("SELECT id FROM pointcloud_resource WHERE id=%s FOR UPDATE",(resource_id,))
            version = connection.execute("SELECT COALESCE(max(version),0)+1 AS n FROM pointcloud_taxonomy WHERE resource_id=%s",
                                         (resource_id,)).fetchone()["n"]
            row = connection.execute("""INSERT INTO pointcloud_taxonomy(id,resource_id,version,name,classes)
                VALUES(%s,%s,%s,%s,%s) RETURNING *""",
                (taxonomy_id,resource_id,version,body.name.strip(),Jsonb(classes))).fetchone()
        return _jsonable(row)

    @api.get("/resources/{resource_id}/taxonomies")
    def list_taxonomies(resource_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s",(resource_id,)).fetchone():
                raise HTTPException(404,"point-cloud resource not found")
            if principal != "admin" and not connection.execute(
                "SELECT 1 FROM pointcloud_version WHERE resource_id=%s AND status='published' LIMIT 1", (resource_id,)
            ).fetchone():
                raise HTTPException(404, "point-cloud resource not found")
            rows = connection.execute("""SELECT * FROM pointcloud_taxonomy t WHERE resource_id=%s AND
                (%s OR EXISTS (SELECT 1 FROM pointcloud_version v WHERE v.resource_id=t.resource_id
                    AND v.taxonomy_id=t.id AND v.status='published')) ORDER BY version DESC""",
                (resource_id, principal == "admin")).fetchall()
        return {"items": [_jsonable(row) for row in rows]}

    @api.post("/resources/{resource_id}/versions", status_code=201, include_in_schema=False)
    def create_version(resource_id: uuid.UUID, body: VersionCreate, reviewer: Annotated[str, Depends(administrator)]):
        ready()
        try:
            entrypoint = _safe_path(body.entrypoint or ("tileset.json" if body.format == "3dtiles" else "cloud.ply"))
        except ValueError as error:
            raise HTTPException(422,str(error)) from error
        if body.format == "3dtiles" and entrypoint != "tileset.json":
            raise HTTPException(422,"3D Tiles entrypoint must be tileset.json")
        if body.purpose == "segmentation" and (body.source_version_id is None or body.taxonomy_id is None):
            raise HTTPException(422,"segmentation versions require source_version_id and taxonomy_id")
        if body.purpose == "source" and (body.source_version_id is not None or body.taxonomy_id is not None):
            raise HTTPException(422,"source versions cannot reference a source version or taxonomy")
        version_id = uuid.uuid4()
        with store.connect() as connection:
            resource = connection.execute("SELECT srs,ST_AsEWKB(spatial_extent) AS extent_wkb,metadata FROM pointcloud_resource WHERE id=%s", (resource_id,)).fetchone()
            if not resource:
                raise HTTPException(404,"point-cloud resource not found")
            if resource["extent_wkb"] is None:
                raise HTTPException(422, "a version cannot be created before its resource has a reviewed WGS84 extent")
            resource_location_review = (resource["metadata"] or {}).get("location_review", {})
            current_extent_hash = _stored_spatial_extent_hash(resource["extent_wkb"])
            if (resource_location_review.get("status") != "confirmed"
                    or resource_location_review.get("extent_sha256") != current_extent_hash):
                raise HTTPException(422, "review the resource's current WGS84 extent before creating a version")
            if "location_review" in body.metadata:
                raise HTTPException(422, "location_review must be submitted in the top-level field, not metadata")
            location_review = _review_location(body.location_review, resource["srs"],
                                               _stored_spatial_extent_hash(resource["extent_wkb"]), reviewer)
            version_metadata = {**body.metadata, "location_review": location_review}
            if body.source_version_id is not None:
                source = connection.execute("SELECT resource_id,status FROM pointcloud_version WHERE id=%s",(body.source_version_id,)).fetchone()
                if not source or source["resource_id"] != resource_id or source["status"] != "published":
                    raise HTTPException(422,"source_version_id must reference a published version of this resource")
            validate_taxonomy(connection,resource_id,body.taxonomy_id)
            if body.job_id is not None:
                job = connection.execute("SELECT resource_id,source_version_id,taxonomy_id FROM pointcloud_job WHERE id=%s",(body.job_id,)).fetchone()
                if not job or job["resource_id"] != resource_id:
                    raise HTTPException(422,"job_id does not belong to this resource")
                if job["source_version_id"] != body.source_version_id or job["taxonomy_id"] != body.taxonomy_id:
                    raise HTTPException(422,"job source version and taxonomy must match the output version")
            row=connection.execute("""INSERT INTO pointcloud_version(id,resource_id,label,format,purpose,status,entrypoint,
                source_version_id,taxonomy_id,job_id,metadata) VALUES(%s,%s,%s,%s,%s,'uploading',%s,%s,%s,%s,%s) RETURNING *""",
                (version_id,resource_id,body.label.strip(),body.format,body.purpose,entrypoint,body.source_version_id,
                    body.taxonomy_id,body.job_id,Jsonb(version_metadata))).fetchone()
        version_dir(resource_id,version_id).mkdir(parents=True,exist_ok=True,mode=0o700)
        return _jsonable(row)

    @api.get("/resources/{resource_id}/versions")
    def list_versions(resource_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s",(resource_id,)).fetchone():
                raise HTTPException(404,"point-cloud resource not found")
            rows=connection.execute("""SELECT * FROM pointcloud_version WHERE resource_id=%s AND (%s OR status='published')
                ORDER BY created_at DESC""",(resource_id,principal == "admin")).fetchall()
        return {"items": [_jsonable(row) for row in rows]}

    @api.get("/versions/{version_id}")
    def get_version(version_id: uuid.UUID, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            row=connection.execute("SELECT * FROM pointcloud_version WHERE id=%s AND status='published'",(version_id,)).fetchone()
            if not row: raise HTTPException(404,"published point-cloud version not found")
            result=_jsonable(row)
            result["manifest"]=[_jsonable(a) for a in connection.execute("SELECT relative_path,size_bytes,sha256,media_type FROM pointcloud_asset WHERE version_id=%s ORDER BY relative_path",(version_id,)).fetchall()]
        return result

    @api.post("/versions/{version_id}/files", status_code=201, include_in_schema=False)
    def init_upload(version_id: uuid.UUID, body: FileInit, _: Annotated[str, Depends(administrator)]):
        ready()
        try: relative_path=_safe_path(body.path)
        except ValueError as error: raise HTTPException(422,str(error)) from error
        if body.size_bytes > MAX_FILE_BYTES: raise HTTPException(413,"file exceeds configured per-file limit")
        if not 0 < body.chunk_size <= MAX_CHUNK_BYTES: raise HTTPException(422,"invalid chunk_size")
        upload_id=uuid.uuid4()
        with store.connect() as connection:
            version=draft_version(connection,version_id)
            if connection.execute("SELECT 1 FROM pointcloud_asset WHERE version_id=%s AND relative_path=%s",(version_id,relative_path)).fetchone():
                raise HTTPException(409,"file is already complete")
            prior=connection.execute("SELECT * FROM pointcloud_upload WHERE version_id=%s AND relative_path=%s",(version_id,relative_path)).fetchone()
            if prior:
                if prior["expected_size"]==body.size_bytes and prior["expected_sha256"]==body.sha256.lower() and prior["chunk_size"]==body.chunk_size and prior["status"]=="uploading":
                    return {"upload_id":str(prior["id"]),"chunk_size":prior["chunk_size"],"status":"uploading","resumed":True}
                raise HTTPException(409,"an upload for this path already exists with different metadata")
            count=connection.execute("SELECT COALESCE(sum(expected_size),0) AS n FROM pointcloud_upload WHERE version_id=%s",(version_id,)).fetchone()["n"]
            if count+body.size_bytes > MAX_VERSION_BYTES: raise HTTPException(413,"version exceeds configured size limit")
            connection.execute("INSERT INTO pointcloud_upload(id,version_id,relative_path,expected_size,expected_sha256,chunk_size,status) VALUES(%s,%s,%s,%s,%s,%s,'uploading')",
                (upload_id,version_id,relative_path,body.size_bytes,body.sha256.lower(),body.chunk_size))
        return {"upload_id":str(upload_id),"chunk_size":body.chunk_size,"status":"uploading","resumed":False}

    @api.put("/uploads/{upload_id}/chunks/{chunk_index}", include_in_schema=False)
    async def upload_chunk(upload_id: uuid.UUID, chunk_index: int, request: Request,
                           _: Annotated[str, Depends(administrator)]):
        ready()
        if chunk_index < 0: raise HTTPException(422,"chunk_index must be nonnegative")
        with store.connect() as connection:
            upload=connection.execute("SELECT u.*,v.resource_id,v.status AS version_status FROM pointcloud_upload u JOIN pointcloud_version v ON v.id=u.version_id WHERE u.id=%s",(upload_id,)).fetchone()
            if not upload: raise HTTPException(404,"upload session not found")
            if upload["version_status"]!="uploading" or upload["status"]!="uploading": raise HTTPException(409,"upload session is closed")
        start=chunk_index*upload["chunk_size"]
        if start>=upload["expected_size"] and upload["expected_size"]!=0: raise HTTPException(416,"chunk index exceeds file length")
        expected=max(0,min(upload["chunk_size"],upload["expected_size"]-start))
        content_range=request.headers.get("content-range","")
        end=start+expected-1
        if content_range!=f"bytes {start}-{end}/{upload['expected_size']}":
            raise HTTPException(416,"Content-Range must match the registered chunk boundary")
        body=await request.body()
        if len(body)!=expected: raise HTTPException(400,"chunk length does not match Content-Range")
        if len(body)>MAX_CHUNK_BYTES: raise HTTPException(413,"chunk exceeds configured limit")
        digest=hashlib.sha256(body).hexdigest()
        with store.connect() as connection:
            prior=connection.execute("SELECT size_bytes,sha256 FROM pointcloud_upload_chunk WHERE upload_id=%s AND chunk_index=%s",(upload_id,chunk_index)).fetchone()
            if prior:
                if prior["size_bytes"]==len(body) and prior["sha256"]==digest:
                    return {"upload_id":str(upload_id),"chunk_index":chunk_index,"status":"already_received"}
                raise HTTPException(409,"chunk index was already uploaded with different content")
            part=uploads_root/str(upload_id)/"payload.part"
            part.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with part.open("r+b" if part.exists() else "w+b") as output:
                output.seek(start); output.write(body); output.flush(); os.fsync(output.fileno())
            connection.execute("INSERT INTO pointcloud_upload_chunk(upload_id,chunk_index,size_bytes,sha256) VALUES(%s,%s,%s,%s)",
                (upload_id,chunk_index,len(body),digest))
        return {"upload_id":str(upload_id),"chunk_index":chunk_index,"status":"received"}

    @api.get("/uploads/{upload_id}", include_in_schema=False)
    def upload_status(upload_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ready()
        with store.connect() as connection:
            upload=connection.execute("SELECT * FROM pointcloud_upload WHERE id=%s",(upload_id,)).fetchone()
            if not upload: raise HTTPException(404,"upload session not found")
            chunks=connection.execute("SELECT chunk_index,size_bytes,sha256 FROM pointcloud_upload_chunk WHERE upload_id=%s ORDER BY chunk_index",(upload_id,)).fetchall()
        result=_jsonable(upload); result["chunks"]=[_jsonable(c) for c in chunks]
        result["expected_chunks"]=(upload["expected_size"]+upload["chunk_size"]-1)//upload["chunk_size"]
        return result

    @api.post("/uploads/{upload_id}/complete", status_code=201, include_in_schema=False)
    def complete_upload(upload_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ready()
        with store.connect() as connection:
            upload=connection.execute("SELECT u.*,v.resource_id,v.status AS version_status FROM pointcloud_upload u JOIN pointcloud_version v ON v.id=u.version_id WHERE u.id=%s",(upload_id,)).fetchone()
            if not upload: raise HTTPException(404,"upload session not found")
            if upload["version_status"]!="uploading": raise HTTPException(409,"version is closed")
            if upload["status"]=="complete":
                return {"upload_id":str(upload_id),"status":"complete","path":upload["relative_path"]}
            chunks=connection.execute("SELECT chunk_index,size_bytes FROM pointcloud_upload_chunk WHERE upload_id=%s ORDER BY chunk_index",(upload_id,)).fetchall()
        expected_chunks=(upload["expected_size"]+upload["chunk_size"]-1)//upload["chunk_size"]
        if len(chunks)!=expected_chunks or [c["chunk_index"] for c in chunks]!=list(range(expected_chunks)):
            raise HTTPException(409,"upload has missing chunks")
        if sum(c["size_bytes"] for c in chunks)!=upload["expected_size"]:
            raise HTTPException(409,"uploaded chunks do not match expected file length")
        part=uploads_root/str(upload_id)/"payload.part"
        destination_root=version_dir(upload["resource_id"],upload["version_id"])
        destination=(destination_root/upload["relative_path"]).resolve()
        if not destination.is_relative_to(destination_root): raise HTTPException(422,"invalid destination path")
        source_file=part if part.is_file() else destination
        if not source_file.is_file() or source_file.stat().st_size!=upload["expected_size"]:
            raise HTTPException(409,"uploaded file is incomplete")
        digest=hashlib.sha256()
        with source_file.open("rb") as stream:
            for block in iter(lambda:stream.read(1024*1024),b""): digest.update(block)
        if digest.hexdigest()!=upload["expected_sha256"]:
            raise HTTPException(422,"uploaded file SHA-256 does not match registration")
        destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if source_file == part:
            os.replace(part,destination)
        media_type=mimetypes.guess_type(upload["relative_path"])[0] or "application/octet-stream"
        with store.connect() as connection:
            connection.execute("INSERT INTO pointcloud_asset(version_id,relative_path,size_bytes,sha256,media_type) VALUES(%s,%s,%s,%s,%s)",
                (upload["version_id"],upload["relative_path"],upload["expected_size"],digest.hexdigest(),media_type))
            connection.execute("UPDATE pointcloud_upload SET status='complete',completed_at=now() WHERE id=%s",(upload_id,))
        shutil.rmtree(uploads_root/str(upload_id),ignore_errors=True)
        return {"upload_id":str(upload_id),"status":"complete","path":upload["relative_path"],"size_bytes":upload["expected_size"],"sha256":digest.hexdigest()}

    @api.post("/versions/{version_id}/finalize", include_in_schema=False)
    def finalize_version(version_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ready()
        with store.connect() as connection:
            version=draft_version(connection,version_id)
            location=connection.execute("""SELECT srs,ST_AsEWKB(spatial_extent) AS extent_wkb,metadata FROM pointcloud_resource WHERE id=%s""",
                                        (version["resource_id"],)).fetchone()
            location_metadata = location["metadata"] if location else {}
            review = (location_metadata or {}).get("location_review", {})
            version_review = (version["metadata"] or {}).get("location_review", {})
            current_extent_hash = _stored_spatial_extent_hash(location["extent_wkb"]) if location and location["extent_wkb"] else None
            try:
                source_crs = CRS.from_user_input(location["srs"]).to_string() if location else None
            except Exception:
                source_crs = None
            if (location is None or location["extent_wkb"] is None
                    or review.get("status") != "confirmed"
                    or review.get("extent_sha256") != current_extent_hash
                    or version_review.get("status") != "confirmed"
                    or version_review.get("extent_sha256") != current_extent_hash
                    or version_review.get("source_crs") != source_crs):
                raise HTTPException(422, "cannot publish a point-cloud version without reviewed WGS84 resource and version locations")
            pending=connection.execute("SELECT count(*) AS n FROM pointcloud_upload WHERE version_id=%s AND status!='complete'",(version_id,)).fetchone()["n"]
            assets=connection.execute("SELECT relative_path,size_bytes,sha256,media_type FROM pointcloud_asset WHERE version_id=%s ORDER BY relative_path",(version_id,)).fetchall()
        if pending: raise HTTPException(409,"version has incomplete file uploads")
        asset_map={row["relative_path"]:row for row in assets}
        entry=version["entrypoint"]
        if entry not in asset_map: raise HTTPException(422,"version entrypoint has not been uploaded")
        base=version_dir(version["resource_id"],version_id)
        if version["format"]=="ply":
            _validate_ply(base/entry)
        else:
            _validate_tileset(base,entry,asset_map)
            # Publish only after the file and tile-node spatial catalog is complete.
            from .resource_index import index_pointcloud_version
            try:
                index_pointcloud_version(store,str(version["resource_id"]),str(version_id))
            except (OSError,ValueError,psycopg.Error) as error:
                raise HTTPException(422,{"code":"index_unavailable","detail":str(error)}) from error
        manifest=[{"path":a["relative_path"],"size_bytes":a["size_bytes"],"sha256":a["sha256"],"media_type":a["media_type"]} for a in assets]
        total=sum(a["size_bytes"] for a in assets)
        with store.connect() as connection:
            current_location=connection.execute("""SELECT srs,ST_AsEWKB(spatial_extent) AS extent_wkb,metadata
                FROM pointcloud_resource WHERE id=%s FOR UPDATE""",(version["resource_id"],)).fetchone()
            current_hash = _stored_spatial_extent_hash(current_location["extent_wkb"]) if current_location and current_location["extent_wkb"] else None
            current_review = (current_location["metadata"] or {}).get("location_review", {}) if current_location else {}
            current_version_review = (version["metadata"] or {}).get("location_review", {})
            try:
                current_source_crs = CRS.from_user_input(current_location["srs"]).to_string() if current_location else None
            except Exception:
                current_source_crs = None
            if (not current_location or current_hash is None
                    or current_review.get("status") != "confirmed"
                    or current_review.get("extent_sha256") != current_hash
                    or current_version_review.get("status") != "confirmed"
                    or current_version_review.get("extent_sha256") != current_hash
                    or current_version_review.get("source_crs") != current_source_crs):
                raise HTTPException(409, "resource location changed after this version was reviewed; review and upload a new version")
            row=connection.execute("""UPDATE pointcloud_version SET status='published',published_at=now(),total_bytes=%s,
                file_count=%s,manifest=%s WHERE id=%s AND status='uploading' RETURNING *""",
                (total,len(manifest),Jsonb(manifest),version_id)).fetchone()
            if version["job_id"]:
                connection.execute("UPDATE pointcloud_job SET status='completed',progress=100,output_version_id=%s,updated_at=now() WHERE id=%s",
                                   (version_id,version["job_id"]))
            connection.execute("UPDATE pointcloud_resource SET updated_at=now() WHERE id=%s",(version["resource_id"],))
        return _jsonable(row)

    @api.get("/versions/{version_id}/manifest")
    def get_manifest(version_id: uuid.UUID, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            row=connection.execute("SELECT id,resource_id,label,format,purpose,status,entrypoint,total_bytes,file_count,manifest FROM pointcloud_version WHERE id=%s AND status='published'",(version_id,)).fetchone()
        if not row: raise HTTPException(404,"published point-cloud version not found")
        return _jsonable(row)

    @api.get("/versions/{version_id}/files/{relative_path:path}")
    def get_asset(version_id: uuid.UUID, relative_path: str, _: Annotated[str, Depends(reader)]):
        ready()
        try: safe_path=_safe_path(relative_path)
        except ValueError as error: raise HTTPException(400,str(error)) from error
        with store.connect() as connection:
            row=connection.execute("""SELECT v.resource_id,v.status,a.media_type,a.size_bytes FROM pointcloud_version v
                JOIN pointcloud_asset a ON a.version_id=v.id WHERE v.id=%s AND a.relative_path=%s""",
                (version_id,safe_path)).fetchone()
        if not row or row["status"]!="published": raise HTTPException(404,"published point-cloud asset not found")
        base=version_dir(row["resource_id"],version_id)
        path=(base/safe_path).resolve()
        if not path.is_relative_to(base) or not path.is_file() or path.stat().st_size!=row["size_bytes"]:
            raise HTTPException(503,"point-cloud asset is missing or corrupt")
        return FileResponse(path,media_type=row["media_type"],headers={"Cache-Control":"private, no-store"})

    @api.post("/resources/{resource_id}/jobs", status_code=201)
    def create_job(resource_id: uuid.UUID, body: JobCreate, _: Annotated[str, Depends(administrator)]):
        ready(); job_id=uuid.uuid4()
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s",(resource_id,)).fetchone():
                raise HTTPException(404,"point-cloud resource not found")
            source=connection.execute("SELECT id FROM pointcloud_version WHERE id=%s AND resource_id=%s AND status='published'",(body.source_version_id,resource_id)).fetchone()
            if not source: raise HTTPException(422,"source_version_id must be a published version of this resource")
            validate_taxonomy(connection,resource_id,body.taxonomy_id)
            row=connection.execute("""INSERT INTO pointcloud_job(id,resource_id,source_version_id,taxonomy_id,algorithm,algorithm_version,parameters,status)
                VALUES(%s,%s,%s,%s,%s,%s,%s,'queued') RETURNING *""",
                (job_id,resource_id,body.source_version_id,body.taxonomy_id,body.algorithm,body.algorithm_version,Jsonb(body.parameters))).fetchone()
        return _jsonable(row)

    @api.get("/resources/{resource_id}/jobs")
    def list_jobs(resource_id: uuid.UUID, principal: Annotated[str, Depends(reader)], limit: int=Query(100,ge=1,le=1000),offset: int=Query(0,ge=0)):
        ready()
        with store.connect() as connection:
            if not connection.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s",(resource_id,)).fetchone():
                raise HTTPException(404,"point-cloud resource not found")
            if principal == "admin":
                rows=connection.execute("SELECT * FROM pointcloud_job WHERE resource_id=%s ORDER BY created_at DESC LIMIT %s OFFSET %s",(resource_id,limit,offset)).fetchall()
            else:
                rows=connection.execute("SELECT j.* FROM pointcloud_job j JOIN pointcloud_version v ON v.id=j.output_version_id WHERE j.resource_id=%s AND v.status='published' ORDER BY j.created_at DESC LIMIT %s OFFSET %s",(resource_id,limit,offset)).fetchall()
        return {"items": [_jsonable(r) for r in rows],"limit":limit,"offset":offset}

    @api.get("/jobs/{job_id}")
    def get_job(job_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            if principal == "admin":
                row=connection.execute("SELECT * FROM pointcloud_job WHERE id=%s",(job_id,)).fetchone()
            else:
                row=connection.execute("""SELECT j.* FROM pointcloud_job j JOIN pointcloud_version v
                    ON v.id=j.output_version_id WHERE j.id=%s AND v.status='published'""",(job_id,)).fetchone()
        if not row: raise HTTPException(404,"point-cloud job not found")
        return _jsonable(row)

    @api.patch("/jobs/{job_id}")
    def patch_job(job_id: uuid.UUID, body: JobPatch, _: Annotated[str, Depends(administrator)]):
        ready()
        with store.connect() as connection:
            job=connection.execute("SELECT * FROM pointcloud_job WHERE id=%s",(job_id,)).fetchone()
            if not job: raise HTTPException(404,"point-cloud job not found")
            if job["status"] in ("completed", "failed", "cancelled"):
                raise HTTPException(409,"terminal point-cloud jobs cannot be changed")
            allowed = {"queued": {"queued", "running", "failed", "cancelled"},
                       "running": {"running", "failed", "cancelled"}}
            if body.status not in allowed.get(job["status"], set()):
                raise HTTPException(409,"invalid point-cloud job state transition")
            if body.output_version_id is not None:
                version=connection.execute("SELECT resource_id,job_id FROM pointcloud_version WHERE id=%s",(body.output_version_id,)).fetchone()
                if not version or version["resource_id"]!=job["resource_id"] or version["job_id"]!=job_id:
                    raise HTTPException(422,"output_version_id must be a version created for this job")
            row=connection.execute("""UPDATE pointcloud_job SET status=%s,progress=%s,
                output_version_id=COALESCE(%s,output_version_id),error=%s,updated_at=now() WHERE id=%s RETURNING *""",
                (body.status,body.progress,body.output_version_id,body.error,job_id)).fetchone()
        return _jsonable(row)

    return api


def _validate_ply(path: Path):
    try:
        with path.open("rb") as stream:
            header=stream.read(1024*1024)
        end=header.find(b"end_header")
        if end<0: raise ValueError("PLY header is missing end_header")
        lines=header[:end].decode("ascii").splitlines()
    except (OSError,UnicodeError) as error:
        raise HTTPException(422,"PLY entrypoint cannot be read") from error
    if not lines or lines[0].strip()!="ply": raise HTTPException(422,"invalid PLY header")
    if not any(len(line.split()) >= 2 and line.startswith("format ") and line.split()[1] in ("ascii","binary_little_endian","binary_big_endian") for line in lines):
        raise HTTPException(422,"unsupported PLY encoding")
    try:
        vertex_count=next((int(line.split()[2]) for line in lines if line.startswith("element vertex ") and len(line.split())==3),None)
    except ValueError as error:
        raise HTTPException(422,"PLY vertex count is invalid") from error
    properties=set()
    in_vertices=False
    for line in lines:
        if line.startswith("element "):
            in_vertices=line.startswith("element vertex ")
        elif in_vertices and line.startswith("property ") and len(line.split())>=3:
            properties.add(line.split()[-1])
    if vertex_count is None or vertex_count<=0 or not {"x","y","z"}.issubset(properties):
        raise HTTPException(422,"PLY must define a positive vertex count and x/y/z properties")


def _validate_tileset(base: Path, entry: str, assets: dict):
    visited=set()
    def resolve(uri: str, parent: str):
        parsed=urlsplit(uri)
        if parsed.scheme or parsed.netloc:
            raise HTTPException(422,"external 3D Tiles URIs are not supported; upload the complete package")
        if uri.startswith("data:"):
            return None
        if re.search(r"\{[^}]+\}", parsed.path):
            raise HTTPException(422,"implicit tiling URI templates are not supported by this upload contract")
        decoded=unquote(parsed.path).replace("\\","/")
        target=posixpath.normpath(posixpath.join(posixpath.dirname(parent),decoded))
        if target==".." or target.startswith("../") or target.startswith("/"):
            raise HTTPException(422,"3D Tiles URI escapes the package root")
        if target not in assets:
            raise HTTPException(422,f"3D Tiles references a missing asset: {target}")
        return target
    def visit_tile(tile, current_file):
        if not isinstance(tile,dict): return
        contents=[]
        if isinstance(tile.get("content"),dict): contents.append(tile["content"])
        if isinstance(tile.get("contents"),list): contents.extend(c for c in tile["contents"] if isinstance(c,dict))
        for content in contents:
            uri=content.get("uri") or content.get("url")
            if not isinstance(uri,str): continue
            child=resolve(uri,current_file)
            if child and child.lower().endswith(".json") and child not in visited:
                visited.add(child)
                try: doc=json.loads((base/child).read_text(encoding="utf-8-sig"))
                except (OSError,UnicodeError,json.JSONDecodeError) as error:
                    raise HTTPException(422,f"invalid child tileset JSON: {child}") from error
                if isinstance(doc,dict):
                    visit_tile(doc.get("root",doc),child)
        for child in tile.get("children",[]) if isinstance(tile.get("children",[]),list) else []:
            visit_tile(child,current_file)
    try: document=json.loads((base/entry).read_text(encoding="utf-8-sig"))
    except (OSError,UnicodeError,json.JSONDecodeError) as error:
        raise HTTPException(422,"tileset.json is missing or invalid JSON") from error
    if not isinstance(document,dict) or not isinstance(document.get("asset"),dict) or not isinstance(document.get("root"),dict):
        raise HTTPException(422,"invalid 3D Tiles tileset: asset and root are required")
    visit_tile(document["root"],entry)
