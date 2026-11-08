"""Persistent catalog for original oblique photos and coarse ground candidates."""

import json
import math
import os
import re
import sqlite3
import statistics
import threading
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
import psycopg
from psycopg.types.json import Jsonb
from pyproj import Transformer

from .spatial import SpatialStore
from .location_review import confirmed_location_review


SCHEMA = """
CREATE TABLE IF NOT EXISTS oblique_resource (
 id uuid PRIMARY KEY, name text NOT NULL, source_srs text NOT NULL,
 assumed_agl_m double precision, ground_window_m double precision,
 buffer_m double precision, max_horizontal_factor double precision,
 location_review jsonb NOT NULL DEFAULT '{}'::jsonb,
 metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
 indexed_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE oblique_resource ADD COLUMN IF NOT EXISTS location_review jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE oblique_resource ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE oblique_resource ALTER COLUMN assumed_agl_m DROP NOT NULL;
ALTER TABLE oblique_resource ALTER COLUMN ground_window_m DROP NOT NULL;
ALTER TABLE oblique_resource ALTER COLUMN buffer_m DROP NOT NULL;
ALTER TABLE oblique_resource ALTER COLUMN max_horizontal_factor DROP NOT NULL;
CREATE TABLE IF NOT EXISTS oblique_group (
 resource_id uuid NOT NULL REFERENCES oblique_resource(id) ON DELETE CASCADE,
 group_id integer NOT NULL, width integer NOT NULL, height integer NOT NULL,
 focal_mm double precision, sensor_mm double precision,
 focal_px double precision NOT NULL, principal_x double precision NOT NULL,
 principal_y double precision NOT NULL, distortion jsonb NOT NULL DEFAULT '{}'::jsonb,
 focal_x_px double precision, focal_y_px double precision,
 principal_x_px double precision, principal_y_px double precision,
 source_model jsonb NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY(resource_id, group_id)
);
ALTER TABLE oblique_group ALTER COLUMN focal_mm DROP NOT NULL;
ALTER TABLE oblique_group ALTER COLUMN sensor_mm DROP NOT NULL;
ALTER TABLE oblique_group ADD COLUMN IF NOT EXISTS focal_x_px double precision;
ALTER TABLE oblique_group ADD COLUMN IF NOT EXISTS focal_y_px double precision;
ALTER TABLE oblique_group ADD COLUMN IF NOT EXISTS principal_x_px double precision;
ALTER TABLE oblique_group ADD COLUMN IF NOT EXISTS principal_y_px double precision;
ALTER TABLE oblique_group ADD COLUMN IF NOT EXISTS source_model jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE TABLE IF NOT EXISTS oblique_image (
 id uuid PRIMARY KEY, resource_id uuid NOT NULL, group_id integer NOT NULL,
 source_photo_id bigint NOT NULL, source_xml_id bigint,
 file_key text NOT NULL, storage_backend text NOT NULL,
 storage_key text, file_status text NOT NULL, size_bytes bigint, sha256 text,
 center_x double precision NOT NULL, center_y double precision NOT NULL,
 center_z double precision NOT NULL, rotation jsonb NOT NULL,
 camera_center geometry(Point,4326),
 candidate_footprint geometry(Polygon,4326),
 coverage_status text NOT NULL, coverage_method text NOT NULL,
 coverage_assumptions jsonb NOT NULL,
 capture_time_local timestamp without time zone,
 source_pose jsonb NOT NULL DEFAULT '{}'::jsonb,
 indexed_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(resource_id,source_photo_id), UNIQUE(file_key),
 FOREIGN KEY(resource_id,group_id) REFERENCES oblique_group(resource_id,group_id) ON DELETE CASCADE
);
ALTER TABLE oblique_image ADD COLUMN IF NOT EXISTS capture_time_local timestamp without time zone;
ALTER TABLE oblique_image ADD COLUMN IF NOT EXISTS source_pose jsonb NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS oblique_image_footprint_gix ON oblique_image USING gist(candidate_footprint);
CREATE INDEX IF NOT EXISTS oblique_image_center_gix ON oblique_image USING gist(camera_center);
CREATE INDEX IF NOT EXISTS oblique_image_resource_idx ON oblique_image(resource_id,group_id,id);
"""


def _key(value: str) -> str:
    value = value.replace("\\", "/")
    if value.startswith("//"):
        value = "/".join(value[2:].split("/")[2:])
    return value


def _xyz(node):
    return tuple(float(node.findtext(k)) for k in ("x", "y", "z"))


def _enu_lonlat(x, y, z, lat0, lon0):
    # WGS84 ENU -> ECEF -> geodetic. Keep z: discarding it shifts oblique rays.
    a, f = 6378137.0, 1 / 298.257223563
    e2 = f * (2 - f)
    p, l = math.radians(lat0), math.radians(lon0)
    n = a / math.sqrt(1 - e2 * math.sin(p)**2)
    x0, y0, z0 = (n * math.cos(p) * math.cos(l), n * math.cos(p) * math.sin(l), n * (1-e2) * math.sin(p))
    sp, cp, sl, cl = math.sin(p), math.cos(p), math.sin(l), math.cos(l)
    xx = x0 - sp*cl*x - sl*y + cp*cl*z
    yy = y0 - sp*sl*x + cl*y + cp*sl*z
    zz = z0 + cp*x + sp*z
    lon = math.atan2(yy, xx)
    radius = math.hypot(xx, yy)
    lat = math.atan2(zz, radius*(1-e2))
    for _ in range(8):
        nn = a / math.sqrt(1-e2*math.sin(lat)**2)
        lat = math.atan2(zz + e2*nn*math.sin(lat), radius)
    return math.degrees(lon), math.degrees(lat)


def _converter(srs: str):
    match = re.fullmatch(r"ENU:\s*([-\d.]+)\s*,\s*([-\d.]+)", srs)
    if match:
        lat0, lon0 = map(float, match.groups())
        return lambda x, y, z: _enu_lonlat(x, y, z, lat0, lon0)
    match = re.fullmatch(r"EPSG:(\d+)", srs)
    if match and int(match.group(1)) not in (4326, 4490):
        transform = Transformer.from_crs(int(match.group(1)), 4326, always_xy=True)
        return lambda x, y, z: transform.transform(x, y)
    raise ValueError(f"SRS cannot be used for metre-based ray projection: {srs}")


def _xml_groups(path: Path):
    root = ET.parse(path).getroot()
    block = root.find("Block")
    srs_id = block.findtext("SRSId")
    srs = next((n.findtext("Definition") for n in root.findall("./SpatialReferenceSystems/SRS") if n.findtext("Id") == srs_id), None)
    if not srs:
        raise ValueError(f"missing SRS in {path}")
    groups = []
    for group_id, node in enumerate(block.findall("./Photogroups/Photogroup"), 1):
        width = int(node.findtext("./ImageDimensions/Width"))
        height = int(node.findtext("./ImageDimensions/Height"))
        focal_mm = float(node.findtext("FocalLength"))
        sensor_mm = float(node.findtext("SensorSize"))
        distortion_node = node.find("Distortion")
        distortion = {child.tag: float(child.text) for child in distortion_node} if distortion_node is not None else {}
        group = dict(group_id=group_id, width=width, height=height, focal_mm=focal_mm,
                     sensor_mm=sensor_mm, focal_px=focal_mm*width/sensor_mm,
                     principal_x=float(node.findtext("./PrincipalPoint/x")),
                     principal_y=float(node.findtext("./PrincipalPoint/y")),
                     distortion=distortion, photos={})
        for photo in node.findall("Photo"):
            pose = photo.find("Pose")
            if pose is None or pose.find("Rotation") is None or pose.find("Center") is None:
                continue
            rot = pose.find("Rotation")
            matrix = [[float(rot.findtext(f"M_{r}{c}")) for c in range(3)] for r in range(3)]
            key = _key(photo.findtext("ImagePath") or "")
            group["photos"][key] = dict(xml_id=int(photo.findtext("Id")), center=_xyz(pose.find("Center")), rotation=matrix)
        groups.append(group)
    return srs, groups


def _candidate(photo, group, converter, ground_z, ground_window_m, buffer_m, max_horizontal_factor):
    cx, cy, cz = photo["center"]
    R = photo["rotation"]
    points = []
    for gz in (ground_z-ground_window_m, ground_z, ground_z+ground_window_m):
        agl = cz-gz
        if agl <= 0:
            return None, "invalid_ground_assumption"
        for px, py in ((0,0),(group["width"],0),(group["width"],group["height"]),(0,group["height"])):
            dc = (px-group["principal_x"], py-group["principal_y"], group["focal_px"])
            dw = [sum(R[i][j]*dc[j] for j in range(3)) for i in range(3)]
            if dw[2] >= -1e-8:
                return None, "horizon_or_upward_ray"
            t = -agl/dw[2]
            if t*math.hypot(dw[0],dw[1]) > max_horizontal_factor*agl:
                return None, "horizon_or_upward_ray"
            lon, lat = converter(cx+t*dw[0], cy+t*dw[1], gz)
            if not (math.isfinite(lon) and math.isfinite(lat) and -180<=lon<=180 and -90<=lat<=90):
                return None, "invalid_projection"
            points.append((lon,lat))
    wkt = "MULTIPOINT(" + ",".join(f"({x:.12f} {y:.12f})" for x,y in points) + ")"
    return wkt, "distortion_unmodeled" if group["distortion"] else "estimated"


def import_legacy_catalog(store: SpatialStore, source_root: Path, mirror_root: Path,
                          agl_m: float = 120, ground_window_m: float = 30,
                          buffer_m: float = 15, max_horizontal_factor: float = 8,
                          location_review_confirmed: bool = False,
                          location_review_source: str = "", reviewer: str = "") -> dict:
    """Idempotently index known files; never fetch or copy original images."""
    if not (agl_m > ground_window_m >= 0 and buffer_m >= 0 and max_horizontal_factor > 0):
        raise ValueError("invalid ground or buffer assumptions")
    if location_review_confirmed is not True or len(location_review_source.strip()) < 5:
        raise ValueError("operator must review the source CRS and document that candidate footprints are estimates")
    source_root, mirror_root = source_root.resolve(), mirror_root.resolve()
    source_db = sqlite3.connect(source_root / "catalog.db")
    source_db.row_factory = sqlite3.Row
    mirror_db = sqlite3.connect(mirror_root / "mirror.sqlite3")
    mirror_db.row_factory = sqlite3.Row
    cached = {}
    for row in mirror_db.execute("SELECT target,object_name,status,size_bytes,sha256 FROM http_cache WHERE method='GET' AND target LIKE '/api/DownLoad/GetFileStream?%' AND status=200"):
        key = unquote(row["target"].split("?",1)[1])
        object_name = row["object_name"]
        path = (mirror_root / "objects" / object_name).resolve()
        if path.is_relative_to(mirror_root / "objects") and path.is_file() and path.stat().st_size == row["size_bytes"]:
            cached[key] = (object_name, row["size_bytes"], row["sha256"])
    store.ensure_schema()
    totals = dict(resources=0, groups=0, images=0, files_present=0, coverage_estimated=0, coverage_unbounded=0, unmatched_pose=0)
    with store.connect() as db:
        db.execute(SCHEMA)
        for xml_path in sorted((source_root / "assets" / "camera_xml").glob("*.xml")):
            resource_id = xml_path.stem
            source_resource = source_db.execute("SELECT name FROM resource WHERE id=?", (resource_id,)).fetchone()
            if source_resource is None:
                continue
            srs, groups = _xml_groups(xml_path)
            converter = _converter(srs)
            location_review = confirmed_location_review(
                source_crs=srs, evidence=location_review_source,
                scope=["source_crs", "camera_centers", "estimated_candidate_footprints", "ground_assumptions"],
                reviewer=reviewer or "admin-cli",
            ) | {"extent_method": "reviewed_camera_pose_with_flat_ground_candidate_model",
                "coverage_accuracy": "estimated_not_guaranteed"}
            z_values = [p["center"][2] for group in groups for p in group["photos"].values()]
            ground_z = statistics.median(z_values)-agl_m
            db.execute("""INSERT INTO oblique_resource(id,name,source_srs,assumed_agl_m,ground_window_m,buffer_m,max_horizontal_factor,location_review)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_srs=excluded.source_srs,
                assumed_agl_m=excluded.assumed_agl_m,ground_window_m=excluded.ground_window_m,buffer_m=excluded.buffer_m,
                max_horizontal_factor=excluded.max_horizontal_factor,location_review=excluded.location_review,indexed_at=now()""",
                (resource_id, source_resource["name"], srs, agl_m, ground_window_m, buffer_m, max_horizontal_factor,
                 Jsonb(location_review)))
            totals["resources"] += 1
            for group in groups:
                db.execute("""INSERT INTO oblique_group(resource_id,group_id,width,height,focal_mm,sensor_mm,focal_px,principal_x,principal_y,distortion)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(resource_id,group_id) DO UPDATE SET
                    width=excluded.width,height=excluded.height,focal_mm=excluded.focal_mm,sensor_mm=excluded.sensor_mm,
                    focal_px=excluded.focal_px,principal_x=excluded.principal_x,principal_y=excluded.principal_y,distortion=excluded.distortion""",
                    (resource_id,group["group_id"],group["width"],group["height"],group["focal_mm"],group["sensor_mm"],
                     group["focal_px"],group["principal_x"],group["principal_y"],Jsonb(group["distortion"])))
                totals["groups"] += 1
            by_key = {key:(group,p) for group in groups for key,p in group["photos"].items()}
            photos = source_db.execute("SELECT photo_id,group_id,fastdfs_id FROM photo WHERE resource_id=?", (resource_id,)).fetchall()
            for row in photos:
                key = row["fastdfs_id"]
                pair = by_key.get(key)
                if pair is None:
                    totals["unmatched_pose"] += 1
                    continue
                group, photo = pair
                cx,cy,cz = photo["center"]
                lon,lat = converter(cx,cy,cz)
                candidate, coverage_status = _candidate(photo,group,converter,ground_z,ground_window_m,buffer_m,max_horizontal_factor)
                if candidate:
                    totals["coverage_estimated"] += 1
                else:
                    totals["coverage_unbounded"] += 1
                present = cached.get(key)
                if present:
                    totals["files_present"] += 1
                assumptions = dict(ground_model="horizontal_plane",ground_z=ground_z,ground_window_m=ground_window_m,
                                   assumed_agl_m=agl_m,buffer_m=buffer_m,max_horizontal_factor=max_horizontal_factor,
                                   distortion_applied=False,guaranteed_bound=False)
                db.execute("""INSERT INTO oblique_image(id,resource_id,group_id,source_photo_id,source_xml_id,file_key,
                    storage_backend,storage_key,file_status,size_bytes,sha256,center_x,center_y,center_z,rotation,camera_center,
                    candidate_footprint,coverage_status,coverage_method,coverage_assumptions)
                    VALUES(%s,%s,%s,%s,%s,%s,'legacy-mirror',%s,%s,%s,%s,%s,%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),
                    CASE WHEN %s::text IS NULL THEN NULL ELSE ST_Buffer(ST_ConvexHull(ST_GeomFromText(%s,4326))::geography,%s)::geometry END,
                    %s,'flat_ground_scenarios',%s)
                    ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id,source_photo_id=excluded.source_photo_id,
                    source_xml_id=excluded.source_xml_id,file_key=excluded.file_key,storage_backend=excluded.storage_backend,
                    storage_key=excluded.storage_key,file_status=excluded.file_status,size_bytes=excluded.size_bytes,
                    sha256=excluded.sha256,center_x=excluded.center_x,center_y=excluded.center_y,center_z=excluded.center_z,
                    rotation=excluded.rotation,camera_center=excluded.camera_center,
                    candidate_footprint=excluded.candidate_footprint,coverage_status=excluded.coverage_status,
                    coverage_method=excluded.coverage_method,coverage_assumptions=excluded.coverage_assumptions,indexed_at=now()""",
                    (str(uuid.uuid5(uuid.NAMESPACE_URL,"deqing-oblique:"+key)),resource_id,group["group_id"],row["photo_id"],
                     photo["xml_id"],key,present[0] if present else None,"present" if present else "missing",
                     present[1] if present else None,present[2] if present else None,cx,cy,cz,Jsonb(photo["rotation"]),
                     lon,lat,candidate,candidate,buffer_m,coverage_status,Jsonb(assumptions)))
                totals["images"] += 1
            db.commit()
    source_db.close(); mirror_db.close()
    return totals


def router(store: SpatialStore, reader: Callable, mirror_root: Path, data_dir: Path | None = None) -> APIRouter:
    api = APIRouter(prefix="/spatial/oblique-images", tags=["oblique-images"])
    volume_image_root = ((data_dir or mirror_root.parent) / "oblique-images").resolve()
    lock = threading.Lock()
    initialized = False

    def ready():
        nonlocal initialized
        if initialized:
            return
        with lock:
            if initialized:
                return
            store.ensure_schema()
            with store.connect() as db:
                db.execute(SCHEMA)
            initialized = True

    @api.get("")
    def list_images(_: Annotated[str, Depends(reader)], bbox: str | None = None,
                    resource_id: str | None = None, limit: int = Query(100,ge=1,le=1000), offset: int = Query(0,ge=0)):
        ready()
        bounds = None
        if bbox:
            try:
                bounds = [float(v) for v in bbox.split(",")]
                if len(bounds)!=4 or not (-180<=bounds[0]<bounds[2]<=180 and -90<=bounds[1]<bounds[3]<=90):
                    raise ValueError
            except ValueError:
                raise HTTPException(400,"bbox must be min_lon,min_lat,max_lon,max_lat")
        if resource_id:
            try: resource_id = str(uuid.UUID(resource_id))
            except ValueError: raise HTTPException(400,"invalid resource_id")
        where = ["(%s::uuid IS NULL OR i.resource_id=%s::uuid)"]
        args = [resource_id,resource_id]
        if bounds:
            where.append("i.candidate_footprint && ST_MakeEnvelope(%s,%s,%s,%s,4326) AND ST_Intersects(i.candidate_footprint,ST_MakeEnvelope(%s,%s,%s,%s,4326))")
            args += bounds+bounds
        condition = " AND ".join(where)
        with store.connect() as db:
            rows = db.execute(f"""SELECT i.id,i.resource_id,i.group_id,i.source_photo_id,i.file_status,i.size_bytes,
                i.capture_time_local,
                i.coverage_status,ST_AsGeoJSON(i.camera_center)::json AS camera_center,
                ST_AsGeoJSON(i.candidate_footprint)::json AS candidate_footprint,r.location_review
                FROM oblique_image i JOIN oblique_resource r ON r.id=i.resource_id
                WHERE {condition} ORDER BY i.resource_id,i.source_photo_id LIMIT %s OFFSET %s""",
                args+[limit,offset]).fetchall()
            total = db.execute(f"SELECT count(*) AS n FROM oblique_image i WHERE {condition}",args).fetchone()["n"]
            unbounded = db.execute("SELECT count(*) AS n FROM oblique_image WHERE candidate_footprint IS NULL AND (%s::uuid IS NULL OR resource_id=%s::uuid)",
                                   (resource_id,resource_id)).fetchone()["n"]
        return {"total":total,"unbounded_count":unbounded,"items":[{**row,"id":str(row["id"]),"resource_id":str(row["resource_id"]),
                "file_url":f"/spatial/oblique-images/{row['id']}/file"} for row in rows],
                "coverage_notice":"Candidate polygons depend on an assumed flat ground interval; unbounded_count is not spatially filtered."}

    @api.get("/resources")
    def list_resources(_: Annotated[str, Depends(reader)]):
        """List registered oblique image packages for the management catalog."""
        ready()
        with store.connect() as db:
            rows = db.execute("""SELECT r.id,r.name,r.source_srs,r.location_review,r.metadata,r.indexed_at,
                count(i.id) AS image_count,coalesce(sum(i.size_bytes),0) AS total_bytes,
                CASE WHEN count(i.camera_center)>0 THEN ARRAY[
                    ST_XMin(ST_Extent(i.camera_center)),ST_YMin(ST_Extent(i.camera_center)),
                    ST_XMax(ST_Extent(i.camera_center)),ST_YMax(ST_Extent(i.camera_center))
                ] ELSE NULL END AS camera_center_extent_bbox_wgs84,
                count(i.id) FILTER (WHERE i.candidate_footprint IS NULL) AS no_footprint_count
                FROM oblique_resource r LEFT JOIN oblique_image i ON i.resource_id=r.id
                GROUP BY r.id ORDER BY r.indexed_at DESC,r.name""").fetchall()
        return {"resources": [{**row,"id":str(row["id"]),"indexed_at":row["indexed_at"].isoformat()} for row in rows]}

    @api.post("/query")
    def query_images(body: dict, _: Annotated[str, Depends(reader)]):
        """Find image candidates by a WGS84 bbox and/or GeoJSON area geometry."""
        ready()
        bounds = None
        raw_bbox = body.get("bbox")
        if raw_bbox is not None:
            try:
                if isinstance(raw_bbox, str):
                    bounds = [float(v) for v in raw_bbox.split(",")]
                elif isinstance(raw_bbox, list):
                    bounds = [float(v) for v in raw_bbox]
                else:
                    raise ValueError
                if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
                        or not (-180 <= bounds[0] < bounds[2] <= 180)
                        or not (-90 <= bounds[1] < bounds[3] <= 90)):
                    raise ValueError
            except (TypeError, ValueError):
                raise HTTPException(422,"bbox must be min_lon,min_lat,max_lon,max_lat")
        geometry = body.get("geometry")
        encoded_geometry = None
        if geometry is not None:
            if not isinstance(geometry, dict) or geometry.get("type") not in {"Polygon", "MultiPolygon"} or "coordinates" not in geometry:
                raise HTTPException(422,"geometry must be a GeoJSON Polygon or MultiPolygon")
            try:
                def validate_coordinates(node):
                    if not isinstance(node, list) or not node:
                        raise ValueError("geometry coordinates must be nonempty arrays")
                    if isinstance(node[0], (int, float)) and not isinstance(node[0], bool):
                        if len(node) < 2 or not isinstance(node[1], (int, float)) or isinstance(node[1], bool):
                            raise ValueError("geometry coordinates must contain longitude and latitude")
                        lon, lat = float(node[0]), float(node[1])
                        if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
                            raise ValueError("geometry coordinates must be WGS84 longitude,latitude")
                        return
                    for child in node:
                        validate_coordinates(child)

                validate_coordinates(geometry["coordinates"])
                encoded_geometry = json.dumps(geometry,ensure_ascii=False,allow_nan=False,separators=(",",":"))
            except (TypeError,ValueError) as error:
                raise HTTPException(422,str(error) or "geometry must contain finite JSON values") from error
            if len(encoded_geometry.encode("utf-8")) > 256 * 1024:
                raise HTTPException(413,"query geometry is too large")
        if bounds is None and encoded_geometry is None:
            raise HTTPException(422,"provide bbox and/or geometry")
        resource_id = body.get("resource_id")
        if resource_id:
            try: resource_id = str(uuid.UUID(str(resource_id)))
            except ValueError: raise HTTPException(422,"invalid resource_id")
        try:
            limit, offset = int(body.get("limit",100)), int(body.get("offset",0))
        except (TypeError,ValueError,OverflowError) as error:
            raise HTTPException(422,"invalid pagination") from error
        if not 1 <= limit <= 1000 or offset < 0:
            raise HTTPException(422,"limit must be 1..1000 and offset must be nonnegative")

        where = ["(%s::uuid IS NULL OR i.resource_id=%s::uuid)"]
        args = [resource_id,resource_id]
        if bounds is not None:
            envelope = "ST_MakeEnvelope(%s,%s,%s,%s,4326)"
            where.append(f"i.candidate_footprint && {envelope} AND ST_Intersects(i.candidate_footprint,{envelope})")
            args += bounds + bounds
        if encoded_geometry is not None:
            expression = "ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)"
            where.append(f"i.candidate_footprint && {expression} AND ST_Intersects(i.candidate_footprint,{expression})")
            args += [encoded_geometry,encoded_geometry]
        condition = " AND ".join(where)
        try:
            with store.connect() as db:
                rows = db.execute(f"""SELECT i.id,i.resource_id,i.group_id,i.source_photo_id,i.file_status,i.size_bytes,
                    i.capture_time_local,
                    i.coverage_status,ST_AsGeoJSON(i.camera_center)::json AS camera_center,
                    ST_AsGeoJSON(i.candidate_footprint)::json AS candidate_footprint,r.location_review
                    FROM oblique_image i JOIN oblique_resource r ON r.id=i.resource_id
                    WHERE {condition} ORDER BY i.resource_id,i.source_photo_id LIMIT %s OFFSET %s""",
                    args+[limit,offset]).fetchall()
                total = db.execute(f"SELECT count(*) AS n FROM oblique_image i WHERE {condition}",args).fetchone()["n"]
                unbounded = db.execute("SELECT count(*) AS n FROM oblique_image WHERE candidate_footprint IS NULL AND (%s::uuid IS NULL OR resource_id=%s::uuid)",
                                       (resource_id,resource_id)).fetchone()["n"]
        except (psycopg.errors.InvalidParameterValue,psycopg.errors.DataException) as error:
            raise HTTPException(422,"invalid query geometry") from error
        return {"total":total,"unbounded_count":unbounded,"items":[{**row,"id":str(row["id"]),"resource_id":str(row["resource_id"]),
                "file_url":f"/spatial/oblique-images/{row['id']}/file"} for row in rows],
                "limit":limit,"offset":offset,"has_more":offset+len(rows)<total,
                "coverage_notice":"Candidate polygons depend on an assumed flat ground interval; unbounded_count is not spatially filtered."}

    @api.get("/{image_id}")
    def get_image(image_id: uuid.UUID, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as db:
            row = db.execute("""SELECT i.*,r.source_srs,r.location_review,r.metadata AS resource_metadata,
                g.width,g.height,g.focal_mm,g.sensor_mm,g.focal_px,g.focal_x_px,g.focal_y_px,
                g.principal_x,g.principal_y,g.principal_x_px,g.principal_y_px,g.distortion,g.source_model,
                ST_AsGeoJSON(i.camera_center)::json AS camera_center_geojson,
                ST_AsGeoJSON(i.candidate_footprint)::json AS candidate_footprint_geojson
                FROM oblique_image i JOIN oblique_resource r ON r.id=i.resource_id
                JOIN oblique_group g ON g.resource_id=i.resource_id AND g.group_id=i.group_id WHERE i.id=%s""",(image_id,)).fetchone()
        if not row: raise HTTPException(404,"image not found")
        row = dict(row)
        row.pop("camera_center"); row.pop("candidate_footprint")
        row.pop("storage_key")
        row["id"]=str(row["id"]); row["resource_id"]=str(row["resource_id"])
        row["indexed_at"]=row["indexed_at"].isoformat()
        row["file_url"]=f"/spatial/oblique-images/{image_id}/file"
        return row

    @api.get("/{image_id}/file")
    def get_file(image_id: uuid.UUID, _: Annotated[str, Depends(reader)]):
        ready()
        with store.connect() as db:
            row = db.execute("SELECT storage_backend,storage_key,file_status,size_bytes FROM oblique_image WHERE id=%s",(image_id,)).fetchone()
        if not row: raise HTTPException(404,"image not found")
        if row["file_status"]!="present" or not row["storage_key"]:
            raise HTTPException(404,"original image not stored locally")
        if row["storage_backend"] == "legacy-mirror":
            root = (mirror_root / "objects").resolve()
        elif row["storage_backend"] == "data-volume":
            root = volume_image_root
        else:
            raise HTTPException(404,"unsupported image storage backend")
        path = (root / row["storage_key"]).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != row["size_bytes"]:
            raise HTTPException(404,"original image is missing from storage")
        return FileResponse(path,media_type="image/jpeg",headers={"Cache-Control":"private, no-store"})

    return api
