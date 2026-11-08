"""PostGIS-backed Shapefile import and GeoJSON spatial queries."""

import codecs
import json
import math
import os
import shutil
import threading
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path, PurePath
from typing import Annotated, Callable

import psycopg
import shapefile
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from psycopg.rows import dict_row
from pyproj import CRS, Transformer

from .location_review import confirmed_location_review
from .poi_identity import POI_SOURCE_SQL, UNIFIED_POI_ID, UNIFIED_POI_NAME


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS spatial_dataset (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    source_filename text NOT NULL,
    source_crs_wkt text NOT NULL,
    source_epsg integer,
    encoding text NOT NULL,
    shape_type integer NOT NULL,
    fields jsonb NOT NULL,
    feature_count bigint NOT NULL DEFAULT 0,
    storage_bytes bigint NOT NULL DEFAULT 0,
    bounds jsonb,
    coordinate_dimension smallint NOT NULL DEFAULT 2,
    vertical_crs_wkt text,
    vertical_unit text,
    vertical_reference_status text NOT NULL DEFAULT 'not_applicable',
    z_min double precision,
    z_max double precision,
    location_review jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS coordinate_dimension smallint NOT NULL DEFAULT 2;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS storage_bytes bigint NOT NULL DEFAULT 0;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS vertical_crs_wkt text;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS vertical_unit text;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS vertical_reference_status text NOT NULL DEFAULT 'not_applicable';
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS z_min double precision;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS z_max double precision;
ALTER TABLE spatial_dataset ADD COLUMN IF NOT EXISTS location_review jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS spatial_feature (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES spatial_dataset(id) ON DELETE CASCADE,
    source_fid bigint NOT NULL,
    properties jsonb NOT NULL,
    geom geometry,
    UNIQUE(dataset_id, source_fid)
);
-- Widen legacy 2D typmod columns once so new records can retain XYZ ordinates.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_attribute
        WHERE attrelid = 'spatial_feature'::regclass AND attname = 'geom'
          AND NOT attisdropped AND atttypmod >= 0
    ) THEN
        ALTER TABLE spatial_feature ALTER COLUMN geom TYPE geometry USING geom::geometry;
    END IF;
END
$$;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'spatial_dataset'::regclass AND conname = 'spatial_dataset_dimension_check') THEN
        ALTER TABLE spatial_dataset ADD CONSTRAINT spatial_dataset_dimension_check
            CHECK (coordinate_dimension IN (2, 3));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'spatial_dataset'::regclass AND conname = 'spatial_dataset_vertical_status_check') THEN
        ALTER TABLE spatial_dataset ADD CONSTRAINT spatial_dataset_vertical_status_check
            CHECK (vertical_reference_status IN ('not_applicable', 'unknown', 'declared'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'spatial_dataset'::regclass AND conname = 'spatial_dataset_z_range_check') THEN
        ALTER TABLE spatial_dataset ADD CONSTRAINT spatial_dataset_z_range_check
            CHECK ((z_min IS NULL) = (z_max IS NULL) AND (z_min IS NULL OR z_min <= z_max));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'spatial_feature'::regclass AND conname = 'spatial_feature_geom_srid_check') THEN
        ALTER TABLE spatial_feature ADD CONSTRAINT spatial_feature_geom_srid_check
            CHECK (geom IS NULL OR ST_SRID(geom) = 4326);
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid = 'spatial_feature'::regclass AND conname = 'spatial_feature_geom_dimension_check') THEN
        ALTER TABLE spatial_feature ADD CONSTRAINT spatial_feature_geom_dimension_check
            CHECK (geom IS NULL OR ST_NDims(geom) IN (2, 3));
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS spatial_feature_dataset_idx ON spatial_feature(dataset_id, id);
CREATE INDEX IF NOT EXISTS spatial_feature_geom_idx ON spatial_feature USING gist(geom);
CREATE INDEX IF NOT EXISTS spatial_feature_properties_idx ON spatial_feature USING gin(properties);
-- All imported POI points live here once consolidated. Dataset IDs remain only
-- as category/provenance identifiers for existing scene references.
CREATE TABLE IF NOT EXISTS poi_feature (
    poi_id text PRIMARY KEY,
    dataset_id uuid NOT NULL REFERENCES spatial_dataset(id) ON DELETE CASCADE,
    feature_id bigint NOT NULL UNIQUE,
    source_fid bigint NOT NULL,
    properties jsonb NOT NULL,
    geom geometry NOT NULL CHECK (ST_GeometryType(geom)='ST_Point' AND ST_SRID(geom)=4326),
    sources jsonb NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS poi_feature_dataset_feature_idx ON poi_feature(dataset_id, feature_id);
CREATE INDEX IF NOT EXISTS poi_feature_geom_idx ON poi_feature USING gist(geom);
CREATE INDEX IF NOT EXISTS poi_feature_properties_idx ON poi_feature USING gin(properties);
CREATE INDEX IF NOT EXISTS poi_feature_sources_idx ON poi_feature USING gin(sources);
WITH dataset_sizes AS (
    SELECT f.dataset_id, SUM(pg_column_size(f))::bigint AS storage_bytes
    FROM spatial_feature AS f
    GROUP BY f.dataset_id
)
UPDATE spatial_dataset AS dataset
SET storage_bytes = COALESCE(dataset_sizes.storage_bytes, 0)
FROM dataset_sizes
WHERE dataset.id = dataset_sizes.dataset_id AND dataset.storage_bytes = 0;
CREATE TABLE IF NOT EXISTS raster_collection (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    crs_epsg integer NOT NULL CHECK (crs_epsg IN (4326, 3857)),
    tile_scheme text NOT NULL CHECK (tile_scheme IN ('tms', 'xyz')),
    tile_size integer NOT NULL CHECK (tile_size IN (256, 512)),
    tile_format text NOT NULL,
    min_zoom integer NOT NULL CHECK (min_zoom >= 0),
    max_zoom integer NOT NULL CHECK (max_zoom >= min_zoom),
    bounds geometry(Polygon, 4326) NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raster_collection_bounds_gix ON raster_collection USING gist(bounds);
CREATE TABLE IF NOT EXISTS raster_epoch (
    collection_id uuid NOT NULL REFERENCES raster_collection(id) ON DELETE CASCADE,
    acquisition_date date NOT NULL,
    storage_path text NOT NULL,
    tile_count bigint NOT NULL CHECK (tile_count >= 0),
    total_bytes bigint NOT NULL CHECK (total_bytes >= 0),
    status text NOT NULL DEFAULT 'published' CHECK (status IN ('staging', 'published', 'failed')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(collection_id, acquisition_date)
);
CREATE TABLE IF NOT EXISTS user_annotation (
    id uuid PRIMARY KEY,
    owner_user_id uuid NOT NULL,
    name text NOT NULL,
    properties jsonb NOT NULL DEFAULT '{}'::jsonb,
    geom geometry(Geometry, 4326) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_annotation_owner_updated_idx ON user_annotation(owner_user_id, updated_at DESC, id);
CREATE INDEX IF NOT EXISTS user_annotation_geom_idx ON user_annotation USING gist(geom);
CREATE TABLE IF NOT EXISTS user_annotation_file (
    id uuid PRIMARY KEY,
    annotation_id uuid NOT NULL REFERENCES user_annotation(id) ON DELETE CASCADE,
    owner_user_id uuid NOT NULL,
    original_name text NOT NULL,
    content_type text NOT NULL,
    relative_path text NOT NULL UNIQUE,
    size_bytes bigint NOT NULL CHECK(size_bytes >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS user_annotation_file_owner_idx
    ON user_annotation_file(owner_user_id, annotation_id, created_at, id);
"""
MAX_UPLOAD_BYTES = int(os.getenv("DEQING_MAX_SHAPEFILE_BYTES", str(4 * 1024**3)))
MAX_QUERY_GEOMETRY_BYTES = 256 * 1024
SUPPORTED_SHAPE_TYPES = {1, 3, 5, 8, 11, 13, 15, 18}  # 2D and Z point, line, polygon, multipoint
Z_SHAPE_TYPES = {11, 13, 15, 18}
SHAPE_TYPES_WITH_PARTS = {3, 5, 13, 15}


class SpatialUnavailable(Exception):
    pass


class SpatialStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._initialized = False
        self._lock = threading.Lock()

    def _dsn(self) -> str:
        secret_path = os.getenv("DEQING_POSTGIS_DSN_FILE", "").strip()
        if secret_path:
            path = Path(secret_path)
            if path.stat().st_mode & 0o077:
                raise SpatialUnavailable("PostGIS DSN file must be owner-only")
            return path.read_text().strip()
        dsn = os.getenv("DEQING_POSTGIS_DSN", "").strip()
        if not dsn:
            raise SpatialUnavailable("PostGIS is not configured")
        return dsn

    def connect(self):
        try:
            return psycopg.connect(self._dsn(), connect_timeout=5, row_factory=dict_row)
        except (OSError, psycopg.OperationalError) as error:
            raise SpatialUnavailable("PostGIS is temporarily unavailable") from error

    def ensure_schema(self):
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            with self.connect() as connection:
                connection.execute(SCHEMA)
            self._initialized = True


def _json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _transform_geometry(geometry: dict, transformer: Transformer) -> tuple[dict, list[float]]:
    bounds = [math.inf, math.inf, -math.inf, -math.inf]

    def transform_coordinates(value):
        if value and isinstance(value[0], (int, float)):
            x, y = transformer.transform(value[0], value[1])
            if not math.isfinite(x) or not math.isfinite(y):
                raise ValueError("geometry contains coordinates that cannot be transformed")
            if not (-180 <= x <= 180 and -90 <= y <= 90):
                raise ValueError("transformed geometry falls outside EPSG:4326 bounds")
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
            return [x, y, *value[2:]]
        return [transform_coordinates(child) for child in value]

    transformed = {"type": geometry["type"], "coordinates": transform_coordinates(geometry["coordinates"])}
    if math.isinf(bounds[0]):
        raise ValueError("geometry has no coordinates")
    return transformed, bounds


def _shape_geometry(shape) -> tuple[dict, list[list[float]]]:
    """Build GeoJSON directly from the source point sequence, retaining Z safely."""
    shape_type = shape.shapeType
    has_z = shape_type in Z_SHAPE_TYPES
    points = shape.points
    if not points:
        raise ValueError("non-null geometry has no coordinates")

    z_values = getattr(shape, "z", None) if has_z else None
    if has_z and (z_values is None or len(z_values) != len(points)):
        raise ValueError("Z geometry has missing or incomplete Z ordinates")

    coordinates = []
    for index, point in enumerate(points):
        x, y = float(point[0]), float(point[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("geometry contains non-finite XY coordinates")
        coordinate = [x, y]
        if has_z:
            z = z_values[index]
            if z is None or not math.isfinite(float(z)) or abs(float(z)) >= 1e38:
                raise ValueError("Z geometry contains a missing or invalid Z ordinate")
            coordinate.append(float(z))
        coordinates.append(coordinate)

    if shape_type in (1, 11):
        if len(coordinates) != 1:
            raise ValueError("point geometry must contain exactly one coordinate")
        return {"type": "Point", "coordinates": coordinates[0]}, [coordinates[0]]
    if shape_type in (8, 18):
        return {"type": "MultiPoint", "coordinates": coordinates}, coordinates
    if shape_type in SHAPE_TYPES_WITH_PARTS:
        starts = list(shape.parts) + [len(coordinates)]
        parts = [coordinates[starts[i]:starts[i + 1]] for i in range(len(starts) - 1)]
        if any(not part for part in parts):
            raise ValueError("geometry contains an empty part")
        if shape_type in (3, 13):
            if len(parts) == 1:
                return {"type": "LineString", "coordinates": parts[0]}, parts[0]
            return {"type": "MultiLineString", "coordinates": parts}, [point for part in parts for point in part]
        # Shapefile polygon parts are rings, not GeoJSON polygon boundaries: use
        # ring winding/containment to preserve holes and disjoint outer polygons.
        polygons = shapefile.organize_polygon_rings(parts)
        flattened = [point for polygon in polygons for ring in polygon for point in ring]
        if len(polygons) == 1:
            return {"type": "Polygon", "coordinates": polygons[0]}, flattened
        return {"type": "MultiPolygon", "coordinates": polygons}, flattened
    raise ValueError(f"unsupported Shapefile shape type: {shape_type}")


def _vertical_metadata(crs: CRS, coordinate_dimension: int,
                       vertical_crs_input: str | None,
                       vertical_unit_input: str | None) -> tuple[str | None, str | None, str]:
    if coordinate_dimension == 2:
        if vertical_crs_input or vertical_unit_input:
            raise ValueError("vertical CRS/unit can only be set for a Z-dimensional Shapefile")
        return None, None, "not_applicable"

    vertical_crs = CRS.from_user_input(vertical_crs_input) if vertical_crs_input else None
    if vertical_crs is None and crs.is_compound:
        vertical_crs = next((part for part in crs.sub_crs_list if part.is_vertical), None)
    vertical_component = None
    if vertical_crs is not None:
        if vertical_crs.is_vertical:
            vertical_component = vertical_crs
        elif vertical_crs.is_compound:
            vertical_component = next((part for part in vertical_crs.sub_crs_list if part.is_vertical), None)
        if vertical_component is None:
            raise ValueError("vertical_crs must identify a vertical CRS or a compound CRS containing one")
    if vertical_unit_input is not None and (not vertical_unit_input.strip() or len(vertical_unit_input.strip()) > 100):
        raise ValueError("vertical_unit must be 1 to 100 characters")
    axis = vertical_component.axis_info[0] if vertical_component and vertical_component.axis_info else None
    unit = vertical_unit_input.strip() if vertical_unit_input else (axis.unit_name if axis else None)
    return (vertical_crs.to_wkt() if vertical_crs else None, unit,
            "declared" if vertical_crs else "unknown")


def _encoding(cpg_path: Path | None, override: str | None) -> str:
    selected = override or (cpg_path.read_text(encoding="utf-8-sig").strip() if cpg_path else "utf-8")
    selected = selected.strip().strip('"')
    if selected.isdecimal():
        selected = "utf-8" if selected == "65001" else "cp" + selected
    try:
        return codecs.lookup(selected).name
    except LookupError as error:
        raise ValueError(f"unsupported DBF encoding: {selected}") from error


def _source_crs(prj_path: Path | None, source_srid: int | None) -> CRS:
    if source_srid is not None and source_srid <= 0:
        raise ValueError("source_srid must be positive")
    if prj_path:
        crs = CRS.from_wkt(prj_path.read_text(encoding="utf-8-sig"))
        if source_srid is not None and crs != CRS.from_epsg(source_srid):
            raise ValueError("source_srid does not match the .prj file")
        return crs
    if source_srid is None:
        raise ValueError("a .prj file or source_srid is required")
    return CRS.from_epsg(source_srid)


def _file_name(upload: UploadFile, suffix: str) -> tuple[str, str]:
    filename = PurePath((upload.filename or "").replace("\\", "/")).name
    path = Path(filename)
    if not filename or path.suffix.lower() != suffix or not path.stem:
        raise HTTPException(422, f"{suffix} upload requires a matching filename")
    return filename, path.stem


def _save_uploads(target: Path, uploads: dict[str, UploadFile | None]) -> str:
    names = []
    total = 0
    for suffix, upload in uploads.items():
        if upload is None:
            continue
        filename, stem = _file_name(upload, suffix)
        names.append(stem.casefold())
        with (target / ("source" + suffix)).open("xb") as output:
            while chunk := upload.file.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, "Shapefile upload is too large")
                output.write(chunk)
    if len(set(names)) != 1:
        raise HTTPException(422, "Shapefile components must have the same basename")
    return _file_name(uploads[".shp"], ".shp")[0]


def _dataset_record(row: dict) -> dict:
    return {**row, "id": str(row["id"])}


def _ensure_unified_poi_dataset(connection) -> None:
    existing = connection.execute("SELECT source_filename FROM spatial_dataset WHERE id=%s",
                                  (UNIFIED_POI_ID,)).fetchone()
    if existing and existing["source_filename"] != "poi_feature":
        raise ValueError("unified POI resource ID collides with an existing dataset")
    fields = [{"name": name, "type": "C", "width": 0, "decimals": 0}
              for name in ("id", "name", "type", "adcode")]
    connection.execute("""INSERT INTO spatial_dataset
        (id,name,source_filename,source_crs_wkt,source_epsg,encoding,shape_type,fields,location_review)
        VALUES (%s,%s,'poi_feature',%s,4326,'UTF-8',1,%s::jsonb,%s::jsonb)
        ON CONFLICT (id) DO NOTHING""",
        (UNIFIED_POI_ID, UNIFIED_POI_NAME, CRS.from_epsg(4326).to_wkt(),
         json.dumps(fields), json.dumps({"status": "derived_from_reviewed_sources"})))


def _refresh_unified_poi_dataset(connection) -> None:
    connection.execute("""WITH extent AS (
        SELECT count(*) AS n,min(ST_X(geom)) AS west,min(ST_Y(geom)) AS south,
               max(ST_X(geom)) AS east,max(ST_Y(geom)) AS north FROM poi_feature)
        UPDATE spatial_dataset d SET feature_count=extent.n,
          bounds=CASE WHEN extent.n=0 THEN NULL ELSE
            to_jsonb(ARRAY[extent.west,extent.south,extent.east,extent.north]) END,
          storage_bytes=pg_total_relation_size('poi_feature')
        FROM extent WHERE d.id=%s""", (UNIFIED_POI_ID,))


def _import_dataset(store: SpatialStore, directory: Path, dataset_id: uuid.UUID,
                    name: str, source_filename: str, source_srid: int | None,
                    encoding_override: str | None, vertical_crs_input: str | None,
                    vertical_unit_input: str | None, location_review_source: str,
                    reviewer: str) -> dict:
    prj_path = directory / "source.prj"
    cpg_path = directory / "source.cpg"
    crs = _source_crs(prj_path if prj_path.exists() else None, source_srid)
    encoding = _encoding(cpg_path if cpg_path.exists() else None, encoding_override)
    transformer = Transformer.from_crs(crs, CRS.from_epsg(4326), always_xy=True)
    with shapefile.Reader(str(directory / "source.shp"), encoding=encoding, encodingErrors="strict") as reader:
        if reader.shapeType not in SUPPORTED_SHAPE_TYPES:
            raise ValueError("supported types are 2D or XYZ point, line, polygon and multipoint; M-only and MultiPatch types are unsupported")
        coordinate_dimension = 3 if reader.shapeType in Z_SHAPE_TYPES else 2
        vertical_crs_wkt, vertical_unit, vertical_status = _vertical_metadata(
            crs, coordinate_dimension, vertical_crs_input, vertical_unit_input,
        )
        fields = [
            {"name": field[0], "type": field[1], "width": field[2], "decimals": field[3]}
            for field in reader.fields[1:]
        ]
        source_epsg = crs.to_epsg()
        count = 0
        bounds = [math.inf, math.inf, -math.inf, -math.inf]
        z_bounds = [math.inf, -math.inf]
        with store.connect() as connection:
            connection.execute(
                """INSERT INTO spatial_dataset
                   (id, name, source_filename, source_crs_wkt, source_epsg, encoding, shape_type, fields,
                    coordinate_dimension, vertical_crs_wkt, vertical_unit, vertical_reference_status,location_review)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s::jsonb)""",
                (dataset_id, name, source_filename, crs.to_wkt(), source_epsg,
                 encoding, reader.shapeType, json.dumps(fields), coordinate_dimension,
                 vertical_crs_wkt, vertical_unit, vertical_status,
                 json.dumps(confirmed_location_review(
                     source_crs=crs.to_string(), evidence=location_review_source,
                     scope=["source_crs", "source_axis_order", "transformed_geometries", "wgs84_bounds"],
                     reviewer=reviewer,
                 ), ensure_ascii=False)),
            )
            with connection.cursor() as cursor:
                batch = []
                for source_fid, record in enumerate(reader.iterShapeRecords()):
                    geometry = None
                    if record.shape.shapeType != 0:
                        source_geometry, source_coordinates = _shape_geometry(record.shape)
                        converted, feature_bounds = _transform_geometry(source_geometry, transformer)
                        geometry = json.dumps(converted, allow_nan=False)
                        bounds = [min(bounds[0], feature_bounds[0]), min(bounds[1], feature_bounds[1]),
                                  max(bounds[2], feature_bounds[2]), max(bounds[3], feature_bounds[3])]
                        if coordinate_dimension == 3:
                            feature_z = [coordinate[2] for coordinate in source_coordinates]
                            z_bounds[0] = min(z_bounds[0], min(feature_z))
                            z_bounds[1] = max(z_bounds[1], max(feature_z))
                    properties = {
                        field["name"]: _json_value(value)
                        for field, value in zip(fields, record.record)
                    }
                    batch.append((dataset_id, source_fid, json.dumps(properties, ensure_ascii=False, allow_nan=False), geometry))
                    count += 1
                    if len(batch) >= 500:
                        cursor.executemany(
                            """INSERT INTO spatial_feature(dataset_id, source_fid, properties, geom)
                               VALUES (%s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))""",
                            batch,
                        )
                        batch.clear()
                if batch:
                    cursor.executemany(
                        """INSERT INTO spatial_feature(dataset_id, source_fid, properties, geom)
                           VALUES (%s, %s, %s::jsonb, ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326))""",
                        batch,
                    )
            spatial_quality = connection.execute("""SELECT
                    count(*) FILTER (WHERE geom IS NOT NULL) AS located_features,
                    count(*) FILTER (WHERE geom IS NOT NULL AND NOT ST_IsValid(geom)) AS invalid_features
                FROM spatial_feature WHERE dataset_id=%s""", (dataset_id,)).fetchone()
            if spatial_quality["located_features"] == 0:
                raise ValueError("Shapefile has no located geometries and cannot be rendered on a map")
            if spatial_quality["invalid_features"]:
                raise ValueError(f"Shapefile contains {spatial_quality['invalid_features']} invalid geometries")
            bbox = None if math.isinf(bounds[0]) else bounds
            z_min = None if math.isinf(z_bounds[0]) else z_bounds[0]
            z_max = None if math.isinf(z_bounds[1]) else z_bounds[1]
            stored_size = connection.execute(
                "SELECT COALESCE(SUM(pg_column_size(f)), 0)::bigint AS storage_bytes "
                "FROM spatial_feature AS f WHERE dataset_id=%s",
                (dataset_id,),
            ).fetchone()["storage_bytes"]
            row = connection.execute(
                """UPDATE spatial_dataset SET feature_count=%s, storage_bytes=%s,
                          bounds=%s::jsonb, z_min=%s, z_max=%s
                   WHERE id=%s RETURNING *""",
                (count, stored_size, json.dumps(bbox), z_min, z_max, dataset_id),
            ).fetchone()
            field_names = {field["name"] for field in fields}
            if reader.shapeType in (1, 11) and {"id", "name", "type", "adcode"} <= field_names:
                if connection.execute("SELECT to_regclass('poi_feature') AS table_name").fetchone()["table_name"]:
                    _ensure_unified_poi_dataset(connection)
                    invalid = connection.execute("""SELECT count(*) AS n FROM spatial_feature
                        WHERE dataset_id=%s AND (properties->>'id' IS NULL OR geom IS NULL
                        OR ST_GeometryType(geom) <> 'ST_Point')""", (dataset_id,)).fetchone()["n"]
                    if invalid:
                        raise ValueError(f"{invalid} POI records lack an ID or point geometry")
                    duplicate = connection.execute("""SELECT count(*) AS n FROM spatial_feature f
                        WHERE f.dataset_id=%s AND EXISTS
                          (SELECT 1 FROM poi_feature p WHERE p.poi_id=f.properties->>'id')""",
                        (dataset_id,)).fetchone()["n"]
                    if duplicate:
                        raise ValueError(f"{duplicate} POI IDs already exist; import as a supplemental source instead")
                    repeated = connection.execute("""SELECT count(*) AS n FROM (
                        SELECT properties->>'id' FROM spatial_feature WHERE dataset_id=%s
                        GROUP BY properties->>'id' HAVING count(*)>1) duplicates""",
                        (dataset_id,)).fetchone()["n"]
                    if repeated:
                        raise ValueError(f"{repeated} POI IDs are repeated within the package")
                    connection.execute("""INSERT INTO poi_feature
                        (poi_id,dataset_id,feature_id,source_fid,properties,geom,sources)
                        SELECT f.properties->>'id',%s,f.id,f.source_fid,f.properties,f.geom,
                          jsonb_build_array(jsonb_build_object(
                            'role','canonical','dataset_id',f.dataset_id::text,
                            'dataset_name',%s,'feature_id',f.id,'source_fid',f.source_fid))
                        FROM spatial_feature f WHERE f.dataset_id=%s""", (UNIFIED_POI_ID, name, dataset_id))
                    connection.execute("DELETE FROM spatial_feature WHERE dataset_id=%s", (dataset_id,))
                    _refresh_unified_poi_dataset(connection)
            elif reader.shapeType in (1, 11) and {"POI_ID", "NAME", "TYPE", "ADCODE"} <= field_names:
                if connection.execute("SELECT to_regclass('poi_feature') AS table_name").fetchone()["table_name"]:
                    invalid = connection.execute("""SELECT count(*) AS n FROM spatial_feature
                        WHERE dataset_id=%s AND (properties->>'POI_ID' IS NULL OR geom IS NULL
                        OR ST_GeometryType(geom) <> 'ST_Point')""", (dataset_id,)).fetchone()["n"]
                    if invalid:
                        raise ValueError(f"{invalid} supplemental POI records lack an ID or point geometry")
                    unmatched = connection.execute("""SELECT count(*) AS n FROM spatial_feature f
                        WHERE f.dataset_id=%s AND NOT EXISTS
                          (SELECT 1 FROM poi_feature p WHERE p.poi_id=f.properties->>'POI_ID')""",
                        (dataset_id,)).fetchone()["n"]
                    if unmatched:
                        raise ValueError(f"{unmatched} supplemental POIs have no canonical record")
                    connection.execute("""WITH grouped AS (
                        SELECT f.properties->>'POI_ID' AS poi_id,
                          jsonb_agg(jsonb_build_object(
                            'role','supplemental','dataset_id',f.dataset_id::text,
                            'dataset_name',%s,'feature_id',f.id,'source_fid',f.source_fid,
                            'name',f.properties->>'NAME',
                            'name_differs',(f.properties->>'NAME') IS DISTINCT FROM (p.properties->>'name'),
                            'coordinate_offset_m',round(ST_Distance(f.geom::geography,p.geom::geography)::numeric,6),
                            'properties',f.properties,'geometry',ST_AsGeoJSON(f.geom)::jsonb)
                            ORDER BY f.id) AS records
                        FROM spatial_feature f JOIN poi_feature p ON p.poi_id=f.properties->>'POI_ID'
                        WHERE f.dataset_id=%s GROUP BY f.properties->>'POI_ID'
                    ) UPDATE poi_feature p SET sources=p.sources || grouped.records
                      FROM grouped WHERE p.poi_id=grouped.poi_id""", (name, dataset_id))
                    connection.execute("DELETE FROM spatial_feature WHERE dataset_id=%s", (dataset_id,))
                    _refresh_unified_poi_dataset(connection)
    return _dataset_record(row)


def _bbox(value: str | None) -> list[float] | None:
    if value is None:
        return None
    try:
        parts = [float(part) for part in value.split(",")]
    except ValueError as error:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat") from error
    if len(parts) != 4 or not all(math.isfinite(part) for part in parts):
        raise HTTPException(422, "bbox must contain four finite numbers")
    if parts[0] >= parts[2] or parts[1] >= parts[3] or not (-180 <= parts[0] < parts[2] <= 180) or not (-90 <= parts[1] < parts[3] <= 90):
        raise HTTPException(422, "bbox must be a valid EPSG:4326 extent")
    return parts


def _feature(row: dict) -> dict:
    return {"type": "Feature", "id": row["id"], "geometry": row["geometry"],
            "properties": row["properties"], "source_fid": row["source_fid"]}


def router(store: SpatialStore, reader: Callable, administrator: Callable, account: Callable) -> APIRouter:
    api = APIRouter(prefix="/spatial", tags=["spatial"])

    def ready():
        try:
            store.ensure_schema()
        except SpatialUnavailable as error:
            raise HTTPException(503, str(error)) from error
        except psycopg.Error as error:
            raise HTTPException(503, "PostGIS schema is unavailable") from error

    @api.get("/healthz")
    def spatial_health(_: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            return {"status": "ok", "postgis": connection.execute("SELECT PostGIS_Version() AS version").fetchone()["version"]}

    # Kept as a runtime compatibility route for existing internal tooling. New
    # data ingress is documented through the unified package upload API.
    @api.post("/datasets/import", status_code=201, include_in_schema=False)
    def import_shapefile(
        reviewer: Annotated[str, Depends(administrator)],
        shp: UploadFile = File(...), shx: UploadFile = File(...), dbf: UploadFile = File(...),
        prj: UploadFile | None = File(None), cpg: UploadFile | None = File(None),
        name: str | None = Form(None), source_srid: int | None = Form(None),
        encoding: str | None = Form(None), vertical_crs: str | None = Form(None),
        vertical_unit: str | None = Form(None),
        location_review_confirmed: bool = Form(False),
        location_review_source: str = Form(..., min_length=5, max_length=500),
    ):
        ready()
        if not location_review_confirmed:
            raise HTTPException(422, "location review confirmation is required before importing a map dataset")
        if not location_review_source.strip():
            raise HTTPException(422, "location review evidence is required")
        if name is not None and (not name.strip() or len(name) > 200):
            raise HTTPException(422, "name must be 1 to 200 characters")
        dataset_id = uuid.uuid4()
        sources = store.data_dir / "spatial" / "sources"
        sources.mkdir(parents=True, exist_ok=True)
        directory = sources / str(dataset_id)
        directory.mkdir(mode=0o700)
        try:
            original_name = _save_uploads(directory, {
                ".shp": shp, ".shx": shx, ".dbf": dbf, ".prj": prj, ".cpg": cpg,
            })
            return _import_dataset(store, directory, dataset_id, name.strip() if name else Path(original_name).stem,
                                   original_name, source_srid, encoding, vertical_crs, vertical_unit,
                                   location_review_source.strip(), reviewer)
        except HTTPException:
            shutil.rmtree(directory)
            raise
        except (ValueError, UnicodeError, shapefile.ShapefileException) as error:
            shutil.rmtree(directory)
            raise HTTPException(422, str(error)) from error
        except (SpatialUnavailable, psycopg.Error) as error:
            shutil.rmtree(directory)
            raise HTTPException(503, "PostGIS import failed") from error
        except Exception:
            shutil.rmtree(directory)
            raise

    @api.get("/datasets")
    def list_datasets(_: Annotated[str, Depends(reader)], limit: int = 100, offset: int = 0):
        ready()
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1..1000 and offset must be nonnegative")
        with store.connect() as connection:
            rows = connection.execute(
                f"""SELECT * FROM spatial_dataset
                    WHERE id=%s OR NOT {POI_SOURCE_SQL}
                    ORDER BY created_at DESC, id LIMIT %s OFFSET %s""",
                (UNIFIED_POI_ID, limit, offset),
            ).fetchall()
        return {"datasets": [_dataset_record(row) for row in rows], "limit": limit, "offset": offset}

    @api.get("/datasets/{dataset_id}")
    def get_dataset(dataset_id: uuid.UUID, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM spatial_dataset WHERE id=%s", (dataset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "spatial dataset not found")
        names = {field.get("name") for field in row["fields"] if isinstance(field, dict)}
        if dataset_id != UNIFIED_POI_ID and row["shape_type"] in (1, 11) and (
            {"id", "name", "type", "adcode"} <= names or
            {"POI_ID", "NAME", "TYPE", "ADCODE"} <= names
        ):
            raise HTTPException(410, "POI source metadata is consolidated; use /spatial/pois/summary")
        return _dataset_record(row)

    def query_features(dataset_id: uuid.UUID, bbox: list[float] | None, geometry: dict | None,
                       properties: dict | None, limit: int, offset: int):
        ready()
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1..1000 and offset must be nonnegative")
        conditions = ["dataset_id=%s"]
        params: list = [dataset_id]
        if bbox is not None:
            envelope = "ST_MakeEnvelope(%s, %s, %s, %s, 4326)"
            conditions.append(f"geom && {envelope} AND ST_Intersects(geom, {envelope})")
            params.extend(bbox + bbox)
        if geometry is not None:
            if not isinstance(geometry, dict) or "type" not in geometry or "coordinates" not in geometry:
                raise HTTPException(422, "geometry must be a GeoJSON geometry object")
            encoded = json.dumps(geometry, allow_nan=False)
            if len(encoded.encode()) > MAX_QUERY_GEOMETRY_BYTES:
                raise HTTPException(413, "query geometry is too large")
            expression = "ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)"
            conditions.append(f"geom && {expression} AND ST_Intersects(geom, {expression})")
            params.extend([encoded, encoded])
        if properties is not None:
            if not isinstance(properties, dict):
                raise HTTPException(422, "properties must be a JSON object")
            conditions.append("properties @> %s::jsonb")
            params.append(json.dumps(properties, ensure_ascii=False, allow_nan=False))
        with store.connect() as connection:
            dataset = connection.execute("SELECT shape_type,fields,feature_count FROM spatial_dataset WHERE id=%s", (dataset_id,)).fetchone()
            if dataset is None:
                raise HTTPException(404, "spatial dataset not found")
            field_names = {field.get("name") for field in dataset["fields"] if isinstance(field, dict)}
            is_core_poi = dataset["shape_type"] in (1, 11) and {"id", "name", "type", "adcode"} <= field_names
            is_supplement = dataset["shape_type"] in (1, 11) and {"POI_ID", "NAME", "TYPE", "ADCODE"} <= field_names
            consolidated = connection.execute("SELECT EXISTS(SELECT 1 FROM poi_feature LIMIT 1) AS yes").fetchone()["yes"]
            if consolidated and (is_supplement or (is_core_poi and dataset_id != UNIFIED_POI_ID)):
                raise HTTPException(410, "POI source dataset is consolidated; use /spatial/pois/query")
            table = "poi_feature" if consolidated and is_core_poi else "spatial_feature"
            identifier = "feature_id" if table == "poi_feature" else "id"
            rows = connection.execute(
                f"""SELECT {identifier} AS id, source_fid, properties, ST_AsGeoJSON(geom)::jsonb AS geometry
                    FROM {table} WHERE {' AND '.join(conditions)}
                    ORDER BY {identifier} LIMIT %s OFFSET %s""",
                params + [limit + 1, offset],
            ).fetchall()
        return {"type": "FeatureCollection", "features": [_feature(row) for row in rows[:limit]],
                "limit": limit, "offset": offset, "has_more": len(rows) > limit}

    @api.get("/datasets/{dataset_id}/features")
    def get_features(dataset_id: uuid.UUID, _: Annotated[str, Depends(reader)],
                     bbox: str | None = None, limit: int = 100, offset: int = 0):
        return query_features(dataset_id, _bbox(bbox), None, None, limit, offset)

    @api.post("/datasets/{dataset_id}/query")
    def spatial_query(dataset_id: uuid.UUID, body: dict, _: Annotated[str, Depends(reader)]):
        try:
            bbox = body.get("bbox")
            parsed_bbox = _bbox(",".join(str(value) for value in bbox)) if bbox is not None else None
            limit, offset = int(body.get("limit", 100)), int(body.get("offset", 0))
            return query_features(dataset_id, parsed_bbox, body.get("geometry"), body.get("properties"), limit, offset)
        except (TypeError, ValueError, OverflowError) as error:
            raise HTTPException(422, "invalid spatial query") from error
        except psycopg.errors.InvalidParameterValue as error:
            raise HTTPException(422, "invalid query geometry") from error

    def annotation_geometry(value) -> str:
        if not isinstance(value, dict) or not isinstance(value.get("type"), str) or "coordinates" not in value:
            raise HTTPException(422, "geometry must be a GeoJSON geometry object")
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise HTTPException(422, "geometry must contain finite JSON values") from error
        if len(encoded.encode("utf-8")) > MAX_QUERY_GEOMETRY_BYTES:
            raise HTTPException(413, "annotation geometry is too large")
        return encoded

    def annotation_row(row: dict) -> dict:
        return {"type": "Feature", "id": str(row["id"]), "geometry": row["geometry"],
                "properties": {**row["properties"], "name": row["name"]},
                "created_at": row["created_at"].isoformat(), "updated_at": row["updated_at"].isoformat()}

    @api.get("/annotations")
    def list_annotations(user: Annotated[dict, Depends(account)], bbox: str | None = None,
                         limit: int = 100, offset: int = 0):
        ready()
        bounds = _bbox(bbox)
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1..1000 and offset must be nonnegative")
        condition = "owner_user_id=%s"
        params: list = [user["id"]]
        if bounds is not None:
            condition += " AND geom && ST_MakeEnvelope(%s,%s,%s,%s,4326) AND ST_Intersects(geom,ST_MakeEnvelope(%s,%s,%s,%s,4326))"
            params.extend(bounds + bounds)
        with store.connect() as connection:
            rows = connection.execute(
                f"""SELECT id, name, properties, ST_AsGeoJSON(geom)::jsonb AS geometry, created_at, updated_at
                    FROM user_annotation WHERE {condition} ORDER BY updated_at DESC,id LIMIT %s OFFSET %s""",
                params + [limit + 1, offset],
            ).fetchall()
        return {"type": "FeatureCollection", "features": [annotation_row(row) for row in rows[:limit]],
                "limit": limit, "offset": offset, "has_more": len(rows) > limit}

    @api.post("/annotations/query")
    def query_annotations(body: dict, user: Annotated[dict, Depends(account)]):
        """Find the current account's annotations intersecting a WGS84 bbox and/or GeoJSON geometry."""
        ready()
        try:
            raw_bbox = body.get("bbox")
            bounds = _bbox(",".join(str(value) for value in raw_bbox)) if raw_bbox is not None else None
            encoded_geometry = annotation_geometry(body["geometry"]) if body.get("geometry") is not None else None
            limit, offset = int(body.get("limit", 100)), int(body.get("offset", 0))
        except (TypeError, ValueError, OverflowError) as error:
            raise HTTPException(422, "invalid annotation spatial query") from error
        if bounds is None and encoded_geometry is None:
            raise HTTPException(422, "provide bbox and/or geometry")
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422, "limit must be 1..1000 and offset must be nonnegative")

        conditions = ["owner_user_id=%s"]
        params: list = [user["id"]]
        if bounds is not None:
            envelope = "ST_MakeEnvelope(%s,%s,%s,%s,4326)"
            conditions.append(f"geom && {envelope} AND ST_Intersects(geom,{envelope})")
            params.extend(bounds + bounds)
        if encoded_geometry is not None:
            expression = "ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)"
            conditions.append(f"geom && {expression} AND ST_Intersects(geom,{expression})")
            params.extend([encoded_geometry, encoded_geometry])
        where = " AND ".join(conditions)
        try:
            with store.connect() as connection:
                rows = connection.execute(
                    f"""SELECT id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,created_at,updated_at
                        FROM user_annotation WHERE {where} ORDER BY updated_at DESC,id LIMIT %s OFFSET %s""",
                    params + [limit + 1, offset],
                ).fetchall()
        except (psycopg.errors.InvalidParameterValue, psycopg.errors.DataException) as error:
            raise HTTPException(422, "invalid annotation query geometry") from error
        return {"type": "FeatureCollection", "features": [annotation_row(row) for row in rows[:limit]],
                "limit": limit, "offset": offset, "has_more": len(rows) > limit}

    @api.post("/annotations", status_code=201)
    def create_annotation(body: dict, user: Annotated[dict, Depends(account)]):
        ready()
        name = body.get("name")
        properties = body.get("properties", {})
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
            raise HTTPException(422, "name must be 1 to 200 characters")
        if not isinstance(properties, dict):
            raise HTTPException(422, "properties must be a JSON object")
        geometry = annotation_geometry(body.get("geometry"))
        try:
            encoded_properties = json.dumps(properties, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise HTTPException(422, "properties must contain finite JSON values") from error
        annotation_id = uuid.uuid4()
        try:
            with store.connect() as connection:
                row = connection.execute(
                    """INSERT INTO user_annotation(id,owner_user_id,name,properties,geom)
                       VALUES (%s,%s,%s,%s::jsonb,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                       RETURNING id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,created_at,updated_at""",
                    (annotation_id, user["id"], name.strip(), encoded_properties, geometry),
                ).fetchone()
        except psycopg.errors.DataException as error:
            raise HTTPException(422, "invalid annotation GeoJSON") from error
        return annotation_row(row)

    @api.get("/annotations/{annotation_id}")
    def get_annotation(annotation_id: uuid.UUID, user: Annotated[dict, Depends(account)]):
        ready()
        with store.connect() as connection:
            row = connection.execute(
                """SELECT id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,created_at,updated_at
                   FROM user_annotation WHERE id=%s AND owner_user_id=%s""",
                (annotation_id, user["id"]),
            ).fetchone()
        if row is None:
            raise HTTPException(404, "annotation not found")
        return annotation_row(row)

    @api.patch("/annotations/{annotation_id}")
    def update_annotation(annotation_id: uuid.UUID, body: dict, user: Annotated[dict, Depends(account)]):
        ready()
        if not body or not set(body).issubset({"name", "geometry", "properties"}):
            raise HTTPException(422, "provide name, geometry, and/or properties")
        assignments = []
        params: list = []
        if "name" in body:
            name = body["name"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                raise HTTPException(422, "name must be 1 to 200 characters")
            assignments.append("name=%s")
            params.append(name.strip())
        if "properties" in body:
            properties = body["properties"]
            if not isinstance(properties, dict):
                raise HTTPException(422, "properties must be a JSON object")
            try:
                encoded_properties = json.dumps(properties, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as error:
                raise HTTPException(422, "properties must contain finite JSON values") from error
            assignments.append("properties=%s::jsonb")
            params.append(encoded_properties)
        if "geometry" in body:
            assignments.append("geom=ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)")
            params.append(annotation_geometry(body["geometry"]))
        assignments.append("updated_at=now()")
        params.extend([annotation_id, user["id"]])
        try:
            with store.connect() as connection:
                row = connection.execute(
                    f"""UPDATE user_annotation SET {', '.join(assignments)}
                        WHERE id=%s AND owner_user_id=%s
                        RETURNING id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,created_at,updated_at""",
                    params,
                ).fetchone()
        except psycopg.errors.DataException as error:
            raise HTTPException(422, "invalid annotation GeoJSON") from error
        if row is None:
            raise HTTPException(404, "annotation not found")
        return annotation_row(row)

    @api.delete("/annotations/{annotation_id}", status_code=204)
    def delete_annotation(annotation_id: uuid.UUID, user: Annotated[dict, Depends(account)]):
        ready()
        with store.connect() as connection:
            result = connection.execute(
                "DELETE FROM user_annotation WHERE id=%s AND owner_user_id=%s",
                (annotation_id, user["id"]),
            )
        if result.rowcount == 0:
            raise HTTPException(404, "annotation not found")
        return None

    @api.get("/datasets/{dataset_id}/features/{feature_id}")
    def get_feature(dataset_id: uuid.UUID, feature_id: int, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as connection:
            if dataset_id != UNIFIED_POI_ID:
                source = connection.execute("SELECT shape_type,fields FROM spatial_dataset WHERE id=%s", (dataset_id,)).fetchone()
                if source and source["shape_type"] in (1, 11):
                    names = {field.get("name") for field in source["fields"] if isinstance(field, dict)}
                    if {"id", "name", "type", "adcode"} <= names or {"POI_ID", "NAME", "TYPE", "ADCODE"} <= names:
                        raise HTTPException(410, "POI source is consolidated; use /spatial/pois/{poi_id}")
            row = connection.execute(
                """SELECT id, source_fid, properties, ST_AsGeoJSON(geom)::jsonb AS geometry
                   FROM spatial_feature WHERE dataset_id=%s AND id=%s""",
                (dataset_id, feature_id),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT feature_id AS id, source_fid, properties,
                              ST_AsGeoJSON(geom)::jsonb AS geometry
                       FROM poi_feature WHERE dataset_id=%s AND feature_id=%s""",
                    (dataset_id, feature_id),
                ).fetchone()
        if row is None:
            raise HTTPException(404, "spatial feature not found")
        return _feature(row)

    return api
