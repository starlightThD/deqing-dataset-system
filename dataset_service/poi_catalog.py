"""One read-only API backed by the consolidated poi_feature table."""

import base64
import binascii
import hashlib
import json
import uuid
from typing import Annotated, Callable

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .legacy_subsets import SubsetError, canonical_geometry
from .poi_identity import UNIFIED_POI_ID, UNIFIED_POI_NAME
from .spatial import SpatialStore, SpatialUnavailable, _bbox


CORE_FIELDS = {"id", "name", "type", "adcode"}
SUPPLEMENT_FIELDS = {"POI_ID", "NAME", "TYPE", "ADCODE"}


class PoiQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bbox: list[float] | None = None
    geometry: dict | None = None
    crs: str = "EPSG:4326"
    dataset_ids: list[uuid.UUID] | None = Field(default=None, max_length=100)
    limit: int = Field(default=200, ge=1, le=1000)
    cursor: str | None = Field(default=None, max_length=4096)


def _catalog(db) -> tuple[dict | None, str]:
    row = db.execute("""SELECT id::text AS id,name,feature_count,bounds,created_at
                        FROM spatial_dataset WHERE id=%s""", (UNIFIED_POI_ID,)).fetchone()
    if row is None:
        return None, ""
    item = {"id": row["id"], "name": row["name"], "feature_count": row["feature_count"],
            "bounds": row["bounds"]}
    signature = [row["id"], row["name"], row["feature_count"], row["bounds"]]
    version = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()[:20]
    return item, version


def _query_geometry(body: PoiQuery) -> str:
    if (body.bbox is None) == (body.geometry is None):
        raise HTTPException(422, "provide exactly one of bbox or geometry")
    if body.bbox is not None:
        bounds = _bbox(",".join(str(value) for value in body.bbox))
        west, south, east, north = bounds
        raw = {"type": "Polygon", "coordinates": [[
            [west, south], [east, south], [east, north], [west, north], [west, south],
        ]]}
    else:
        raw = body.geometry
    try:
        normalized = canonical_geometry(raw, body.crs, unlimited_area=True)
    except SubsetError as error:
        raise HTTPException(422, str(error)) from error
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _scope_hash(version: str, geometry: str) -> str:
    raw = json.dumps([version, geometry], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _decode_cursor(cursor: str | None, scope_hash: str) -> str:
    if cursor is None:
        return ""
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, dict) or value.get("scope") != scope_hash:
            raise ValueError
        last = value.get("last")
        if not isinstance(last, str) or not last or len(last) > 200:
            raise ValueError
        return last
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise HTTPException(409, "cursor is invalid or the POI catalog/query changed") from None


def _encode_cursor(scope_hash: str, last: str) -> str:
    raw = json.dumps({"scope": scope_hash, "last": last}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


def _features(canonical_rows: list[dict], *, detail: bool = False) -> list[dict]:
    features = []
    for row in canonical_rows:
        supplemental = {}
        known_keys = {key.lower() for key in row["properties"]}
        sources = row["sources"]
        for source in sources:
            if source.get("role") != "supplemental":
                continue
            for key, value in source.get("properties", {}).items():
                normalized_key = key.lower()
                if normalized_key not in known_keys and normalized_key not in supplemental and value not in (None, ""):
                    supplemental[normalized_key] = value
        source_count = len(sources)
        if not detail:
            sources = [{"role": source.get("role"), "feature_id": source.get("feature_id")}
                       for source in sources]
        features.append({"type": "Feature", "id": row["poi_id"], "geometry": row["geometry"],
                         "properties": row["properties"],
                         "poi": {"poi_id": row["poi_id"], "name": row["properties"].get("name"),
                                 "category": row["dataset_name"], "dataset_id": str(UNIFIED_POI_ID),
                                 "feature_id": row["feature_id"], "source_fid": row["source_fid"],
                                 "supplemental_properties": supplemental,
                                 "source_count": source_count, "sources": sources}})
    return features


def router(store: SpatialStore, reader: Callable) -> APIRouter:
    api = APIRouter(prefix="/spatial/pois", tags=["unified-poi"])

    def ready() -> None:
        try:
            store.ensure_schema()
        except (SpatialUnavailable, psycopg.Error) as error:
            raise HTTPException(503, "PostGIS POI catalog is unavailable") from error

    @api.get("/summary")
    def summary(_: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as db:
            catalog, version = _catalog(db)
            unique = db.execute("SELECT count(*) AS n FROM poi_feature").fetchone()["n"]
        return {"catalog_version": version, "id": str(UNIFIED_POI_ID),
                "name": catalog["name"] if catalog else UNIFIED_POI_NAME, "feature_count": unique,
                "unique_poi_count": unique, "bounds": catalog["bounds"] if catalog else None,
                "categories": [catalog] if catalog else [], "supplemental_datasets": [],
                "coordinate_system": "EPSG:4326"}

    @api.post("/query")
    def query(body: PoiQuery, _: Annotated[str, Depends(reader)]):
        ready()
        geometry = _query_geometry(body)
        with store.connect() as db:
            catalog, version = _catalog(db)
            if catalog is None:
                raise HTTPException(409, "the unified POI catalog is not registered")
            if body.dataset_ids is not None and set(body.dataset_ids) != {UNIFIED_POI_ID}:
                raise HTTPException(422, "dataset_ids may only contain the unified POI ID")
            scope = _scope_hash(version, geometry)
            last = _decode_cursor(body.cursor, scope)
            rows = db.execute("""SELECT f.poi_id,f.feature_id,f.dataset_id::text AS dataset_id,
                    d.name AS dataset_name,f.source_fid,f.properties,f.sources,
                    ST_AsGeoJSON(f.geom)::jsonb AS geometry
                FROM poi_feature f JOIN spatial_dataset d ON d.id=f.dataset_id
                WHERE f.poi_id>%s
                  AND f.geom && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND ST_Intersects(f.geom,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                ORDER BY f.poi_id LIMIT %s""",
                (last, geometry, geometry, body.limit + 1)).fetchall()
            more = len(rows) > body.limit
            rows = rows[:body.limit]
            features = _features(rows)
        return {"type": "FeatureCollection", "features": features,
                "catalog_version": version, "resource_id": str(UNIFIED_POI_ID),
                "limit": body.limit, "has_more": more,
                "next_cursor": _encode_cursor(scope, rows[-1]["poi_id"]) if more else None,
                "match_basis": "canonical_point_geometry", "coordinate_system": "EPSG:4326"}

    @api.get("/{poi_id}")
    def detail(poi_id: str, _: Annotated[str, Depends(reader)]):
        if not 1 <= len(poi_id) <= 200 or any(ord(char) < 32 for char in poi_id):
            raise HTTPException(422, "invalid POI ID")
        ready()
        with store.connect() as db:
            rows = db.execute("""SELECT f.poi_id,f.feature_id,f.dataset_id::text AS dataset_id,
                           d.name AS dataset_name,f.source_fid,f.properties,f.sources,
                           ST_AsGeoJSON(f.geom)::jsonb AS geometry
                         FROM poi_feature f JOIN spatial_dataset d ON d.id=f.dataset_id
                         WHERE f.poi_id=%s""", (poi_id,)).fetchall()
            if not rows:
                raise HTTPException(404, "POI not found")
            feature = _features(rows, detail=True)[0]
        return feature

    return api
