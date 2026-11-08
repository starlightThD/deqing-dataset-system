"""Admin-managed geospatial video groups with resumable, file-preserving uploads."""

import hashlib
import hmac
import json
import math
import mimetypes
import os
import re
import shutil
import secrets
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Annotated, Callable, Literal
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from psycopg.types.json import Jsonb
from pyproj import CRS, Transformer

from .spatial import SpatialStore
from .location_review import confirmed_location_review


SCHEMA = """
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE TABLE IF NOT EXISTS video_group (
 id uuid PRIMARY KEY, name text NOT NULL,
 status text NOT NULL CHECK(status IN ('uploading','published')),
 source_srs text NOT NULL, metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
 spatial_extent geometry(Geometry,4326),
 camera_center geometry(Point,4326),
 camera_center_status text NOT NULL DEFAULT 'unavailable'
   CHECK(camera_center_status IN ('verified','inferred','unavailable')),
 total_bytes bigint NOT NULL DEFAULT 0, asset_count integer NOT NULL DEFAULT 0,
 created_at timestamptz NOT NULL DEFAULT now(), published_at timestamptz
);
ALTER TABLE video_group ADD COLUMN IF NOT EXISTS camera_center geometry(Point,4326);
ALTER TABLE video_group ADD COLUMN IF NOT EXISTS camera_center_status text NOT NULL DEFAULT 'unavailable';
ALTER TABLE video_group ALTER COLUMN spatial_extent DROP NOT NULL;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='video_group'::regclass AND conname='video_group_camera_center_status_check') THEN
        ALTER TABLE video_group ADD CONSTRAINT video_group_camera_center_status_check
            CHECK(camera_center_status IN ('verified','inferred','unavailable'));
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS video_group_extent_gix
 ON video_group USING gist(spatial_extent);
CREATE INDEX IF NOT EXISTS video_group_camera_center_gix
 ON video_group USING gist(camera_center);
CREATE INDEX IF NOT EXISTS video_group_status_created_idx
 ON video_group(status,created_at DESC);
CREATE TABLE IF NOT EXISTS video_group_asset (
 group_id uuid NOT NULL REFERENCES video_group(id) ON DELETE CASCADE,
 relative_path text NOT NULL, role text NOT NULL,
 size_bytes bigint NOT NULL CHECK(size_bytes > 0), sha256 text NOT NULL,
 media_type text NOT NULL,
 status text NOT NULL CHECK(status IN ('uploading','complete')),
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(group_id,relative_path)
);
CREATE INDEX IF NOT EXISTS video_group_asset_group_idx
 ON video_group_asset(group_id,relative_path);
CREATE TABLE IF NOT EXISTS video_upload (
 id uuid PRIMARY KEY,
 group_id uuid NOT NULL REFERENCES video_group(id) ON DELETE CASCADE,
 relative_path text NOT NULL, expected_size bigint NOT NULL CHECK(expected_size > 0),
 expected_sha256 text NOT NULL, chunk_size integer NOT NULL CHECK(chunk_size > 0),
 status text NOT NULL CHECK(status IN ('uploading','complete')),
 created_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz,
 UNIQUE(group_id,relative_path)
);
CREATE TABLE IF NOT EXISTS video_upload_chunk (
 upload_id uuid NOT NULL REFERENCES video_upload(id) ON DELETE CASCADE,
 chunk_index integer NOT NULL, size_bytes integer NOT NULL, sha256 text NOT NULL,
 PRIMARY KEY(upload_id,chunk_index)
);
"""

MAX_FILE_BYTES = int(os.getenv("DEQING_VIDEO_MAX_FILE_BYTES", str(24 * 1024**3)))
MAX_GROUP_BYTES = int(os.getenv("DEQING_VIDEO_MAX_GROUP_BYTES", str(100 * 1024**3)))
MAX_CHUNK_BYTES = int(os.getenv("DEQING_VIDEO_MAX_CHUNK_BYTES", str(16 * 1024**2)))
MIN_CHUNK_BYTES = 64 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_ASSETS = 10000
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY: set[int] = set()
_CAMERA_BACKFILL_LOCK = threading.Lock()
_CAMERA_BACKFILL_READY: set[int] = set()
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
_ALLOWED_ROLES = {"video", "evidence", "model", "raster", "document", "other"}


def _status(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized == "verified" or normalized.startswith("verified_"):
        return "verified"
    if "inferred" in normalized:
        return "inferred"
    return "unavailable"


def _camera_pose_payload(source_srs: str, source_axis_order: str,
                         calibration: dict) -> dict:
    pose = calibration.get("pose") or {}
    position_unit = calibration.get("position_unit")
    vertical_unit = calibration.get("vertical_unit", position_unit)
    angle_unit = calibration.get("angle_unit")
    source_crs_name = calibration.get("coordinate_reference") or source_srs
    crs_status = _status(calibration.get("coordinate_reference_status"))
    axis_order = (source_axis_order or "").strip().lower()
    raw_pose = {
        **pose,
        "position_unit": position_unit,
        "vertical_unit": vertical_unit,
        "angle_unit": angle_unit,
    }
    source = {
        "horizontal_crs": source_crs_name or None,
        "axis_order": axis_order or None,
        "coordinate_reference_status": crs_status,
        "axis_order_status": crs_status if axis_order else "unavailable",
        "vertical_crs": None,
        "vertical_reference_status": "unavailable",
    }
    center = {
        "longitude": None,
        "latitude": None,
        "elevation_m": None,
        "coordinate_order": ["longitude", "latitude", "elevation_m"],
        "horizontal_status": "unavailable",
        "elevation_status": "unavailable",
    }
    conversion = {
        "target_crs": "EPSG:4326",
        "output_axis_order": ["longitude", "latitude"],
        "horizontal_method": None,
        "horizontal_status": "unavailable",
        "vertical_method": None,
        "vertical_status": "unavailable",
    }
    reasons = []

    try:
        if not all(key in pose for key in ("x", "y", "z", "phi", "omega", "kappa")):
            raise ValueError("raw pose is incomplete")
        if crs_status == "unavailable":
            raise ValueError("source horizontal CRS is not verified or inferred")
        source_crs = CRS.from_user_input(source_crs_name)
        declared_srs = CRS.from_user_input(source_srs)
        if source_crs != declared_srs:
            raise ValueError("Calibration/Pose CRS differs from SpatialReference horizontal CRS")
        if axis_order == "easting,northing" and source_crs.is_projected:
            x, y = float(pose["x"]), float(pose["y"])
        elif axis_order == "northing,easting" and source_crs.is_projected:
            x, y = float(pose["y"]), float(pose["x"])
        elif axis_order == "longitude,latitude" and source_crs.is_geographic:
            x, y = float(pose["x"]), float(pose["y"])
        elif axis_order == "latitude,longitude" and source_crs.is_geographic:
            x, y = float(pose["y"]), float(pose["x"])
        else:
            raise ValueError("source axis order is missing or incompatible with its CRS")
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("source camera center contains non-finite horizontal coordinates")
        if source_crs.is_projected:
            axes = source_crs.axis_info
            units = {axis.unit_name.casefold() for axis in axes[:2]}
            if position_unit not in {"metre", "meter", "metres", "meters"} or not units.issubset({"metre", "meter"}):
                raise ValueError("projected coordinate units are not confirmed to be metres")
        elif position_unit not in {"degree", "degrees"}:
            raise ValueError("geographic coordinate units are not confirmed to be degrees")
        transformer = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True)
        longitude, latitude = transformer.transform(x, y)
        if not (math.isfinite(longitude) and math.isfinite(latitude)
                and -180 <= longitude <= 180 and -90 <= latitude <= 90):
            raise ValueError("transformed camera center is outside EPSG:4326 bounds")
        center.update({
            "longitude": longitude,
            "latitude": latitude,
            "horizontal_status": crs_status,
        })
        conversion.update({
            "horizontal_method": (
                "pyproj Transformer.from_crs(source_crs, EPSG:4326, always_xy=True); "
                f"interpreted source values as {axis_order}"
            ),
            "input_axis_order": axis_order,
            "horizontal_status": crs_status,
        })
    except Exception as error:
        # Invalid/missing georeferencing must not prevent reading the video group.
        reasons.append(str(error))

    if vertical_unit not in {"metre", "meter", "metres", "meters"}:
        reasons.append("source vertical unit is not confirmed as metres")
    else:
        reasons.append("source Z has no declared vertical CRS/datum; WGS84 elevation cannot be derived")
    reasons.append(
        "source provides phi/omega/kappa values but no camera/world axes, rotation order, handedness, or rotation direction"
    )

    return {
        "status": center["horizontal_status"],
        "center_wgs84": center,
        "orientation": {
            "status": "unavailable",
            "heading_pitch_roll_degrees": None,
            "world_axes": {"x": "unavailable", "y": "unavailable", "z": "unavailable"},
            "camera_axes": {"x": "unavailable", "y": "unavailable", "z": "unavailable"},
            "angle_convention": {
                "status": "unavailable",
                "rotation_order": "unavailable",
                "intrinsic_or_extrinsic": "unavailable",
                "handedness": "unavailable",
                "rotation_direction": "unavailable",
                "unit": angle_unit,
            },
            "angle_unit": angle_unit,
            "raw_angles": {key: pose.get(key) for key in ("phi", "omega", "kappa")},
            "reason": reasons[-1],
        },
        "original_pose": raw_pose,
        "original_crs": source,
        "conversion": conversion,
        "temporal_model": {
            "status": "unavailable",
            "kind": "single_calibration_solution",
            "is_fixed_camera": None,
            "pose_sample_count": 1,
            "reason": "No timestamped pose sequence is present, and the single calibration solution does not establish that the camera stayed fixed throughout the video.",
        },
        "unavailable_reasons": reasons,
    }


def _ensure_schema(store: SpatialStore) -> None:
    key = id(store)
    if key in _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        store.ensure_schema()
        with store.connect() as connection:
            connection.execute(SCHEMA)
        _SCHEMA_READY.add(key)


def _ensure_camera_pose_backfill(store: SpatialStore, files_root: Path, staging_root: Path) -> None:
    key = id(store)
    if key in _CAMERA_BACKFILL_READY:
        return
    with _CAMERA_BACKFILL_LOCK:
        if key in _CAMERA_BACKFILL_READY:
            return
        with store.connect() as connection:
            rows = connection.execute(
                "SELECT id,source_srs,metadata FROM video_group WHERE NOT (metadata ? 'camera_pose')"
            ).fetchall()
            for row in rows:
                group_id = str(row["id"])
                xml_path = files_root / group_id / "calibration.xml"
                if not xml_path.is_file():
                    xml_path = staging_root / group_id / "calibration.xml"
                metadata = row["metadata"] or {}
                if xml_path.is_file():
                    try:
                        _, _, _, parsed_metadata, _ = _parse_calibration_xml(xml_path.read_bytes())
                        camera_pose = parsed_metadata["camera_pose"]
                    except Exception:
                        calibration = metadata.get("calibration", {})
                        camera_pose = _camera_pose_payload(
                            row["source_srs"], calibration.get("source_axis_order", ""), calibration,
                        )
                else:
                    calibration = metadata.get("calibration", {})
                    camera_pose = _camera_pose_payload(
                        row["source_srs"], calibration.get("source_axis_order", ""), calibration,
                    )
                center = camera_pose["center_wgs84"]
                longitude, latitude = center.get("longitude"), center.get("latitude")
                connection.execute(
                    """UPDATE video_group SET
                           camera_center=CASE WHEN %s::double precision IS NULL OR %s::double precision IS NULL
                               THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END,
                           camera_center_status=%s,
                           metadata=metadata || %s
                       WHERE id=%s""",
                    (longitude, latitude, longitude, latitude, center["horizontal_status"],
                     Jsonb({"camera_pose": camera_pose}), row["id"]),
                )
        _CAMERA_BACKFILL_READY.add(key)


def _safe_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise HTTPException(422, "path must be a relative POSIX path")
    decoded = unquote(value)
    if decoded.startswith("/") or re.match(r"^[A-Za-z]:", decoded):
        raise HTTPException(422, "absolute paths are not allowed")
    pieces = decoded.split("/")
    if any(piece in ("", ".", "..") for piece in pieces):
        raise HTTPException(422, "path contains an invalid segment")
    if any(ord(char) < 32 for char in decoded):
        raise HTTPException(422, "path contains control characters")
    return "/".join(pieces)


def _parse_calibration_xml(body: bytes, reviewer: str = "admin") -> tuple[str, str, dict, dict, list[dict]]:
    if not body or len(body) > MAX_XML_BYTES:
        raise HTTPException(413, "calibration.xml must be between 1 byte and 1 MiB")
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise HTTPException(422, "DTD and entity declarations are not allowed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise HTTPException(422, "calibration.xml is not valid XML") from error
    if root.tag != "VideoGroup" or root.get("contractVersion") != "1.0":
        raise HTTPException(422, "expected VideoGroup contractVersion=1.0")
    name = (root.get("name") or "").strip()
    if not name or len(name) > 200:
        raise HTTPException(422, "VideoGroup name is required and must be at most 200 characters")
    spatial_index = (root.get("spatialIndex") or "footprint").strip()
    if spatial_index != "footprint":
        raise HTTPException(422, "map-ready video registration requires a sourced product footprint")

    spatial_ref = root.find("SpatialReference")
    source_srs = (spatial_ref.get("horizontal") or "").strip() if spatial_ref is not None else ""
    if not source_srs or len(source_srs) > 200:
        raise HTTPException(422, "SpatialReference horizontal is required")
    source_axis_order = (spatial_ref.get("axisOrder") or "").strip() if spatial_ref is not None else ""
    if source_axis_order not in {"easting,northing", "northing,easting", "longitude,latitude", "latitude,longitude"}:
        raise HTTPException(422, "SpatialReference axisOrder must explicitly identify the source X/Y order")
    spatial_source = (spatial_ref.get("source") or "").strip() if spatial_ref is not None else ""
    if not spatial_source:
        raise HTTPException(422, "SpatialReference source must identify the CRS evidence")
    try:
        source_crs_object = CRS.from_user_input(source_srs)
    except Exception as error:
        raise HTTPException(422, "SpatialReference horizontal must be a valid CRS") from error
    if (source_axis_order in {"easting,northing", "northing,easting"}) != source_crs_object.is_projected:
        raise HTTPException(422, "SpatialReference axisOrder does not match whether its CRS is projected or geographic")

    coverage = root.find("Coverage")
    extent = None
    coverage_metadata = {"status": "unavailable", "source": None, "source_crs": None,
                         "semantics": None, "reason": None}
    if coverage is None or coverage.get("geometryCRS") != "EPSG:4326":
        raise HTTPException(422, "footprint indexing requires Coverage geometryCRS=EPSG:4326")
    coverage_source = (coverage.get("source") or "").strip()
    coverage_source_crs = (coverage.get("sourceCRS") or "").strip()
    coverage_semantics = (coverage.get("semantics") or "").strip()
    if not coverage_source or not coverage_source_crs or not coverage_semantics:
        raise HTTPException(422, "Coverage must identify its source, source CRS and footprint semantics")
    try:
        CRS.from_user_input(coverage_source_crs)
    except Exception as error:
        raise HTTPException(422, "Coverage sourceCRS must be a valid CRS") from error
    points = []
    for point in coverage.findall("./Polygon/Point"):
        try:
            lon = float(point.attrib["lon"])
            lat = float(point.attrib["lat"])
        except (KeyError, ValueError) as error:
            raise HTTPException(422, "Coverage Polygon points need numeric lon and lat") from error
        if not math.isfinite(lon) or not math.isfinite(lat) or not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
            raise HTTPException(422, "Coverage coordinates must be WGS84 longitude,latitude")
        points.append([lon, lat])
    if len(points) < 4 or points[0] != points[-1]:
        raise HTTPException(422, "Coverage Polygon must have at least four points and be closed")
    extent = {"type": "Polygon", "coordinates": [points]}
    coverage_metadata = {"status": "indexed", "source": coverage_source,
                         "source_crs": coverage_source_crs, "semantics": coverage_semantics,
                         "reason": None}

    review_node = root.find("LocationReview")
    review_source = (review_node.get("source") or "").strip() if review_node is not None else ""
    review_fields = (review_node.get("fields") or "").split(",") if review_node is not None else []
    required_review_fields = {"source_crs", "axis_order", "camera_center_wgs84", "coverage_wgs84"}
    if (review_node is None or review_node.get("status") != "confirmed"
            or review_node.get("coordinateOrder") != "longitude,latitude"
            or len(review_source) < 5 or len(review_source) > 500
            or not required_review_fields.issubset({field.strip() for field in review_fields})):
        raise HTTPException(422, "LocationReview must confirm the CRS, axis order, WGS84 camera center and product coverage")
    location_review = confirmed_location_review(
        source_crs=source_srs, evidence=review_source,
        scope=sorted(required_review_fields), reviewer=reviewer,
    )

    calibration = root.find("Calibration")
    if calibration is None:
        raise HTTPException(422, "Calibration element is required")
    calibration_source = (calibration.get("source") or "").strip()
    calibration_crs = (calibration.get("coordinateReference") or "").strip()
    calibration_status = (calibration.get("coordinateReferenceStatus") or "").strip()
    calibration_method = (calibration.get("method") or "").strip()
    if not calibration_source or not calibration_crs or not calibration_method:
        raise HTTPException(422, "Calibration must identify its source, coordinate reference and method")
    if calibration_status not in {"verified", "inferred"}:
        raise HTTPException(422, "Calibration coordinateReferenceStatus must be verified or inferred")
    try:
        if CRS.from_user_input(calibration_crs) != source_crs_object:
            raise HTTPException(422, "Calibration coordinateReference must match SpatialReference horizontal")
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(422, "Calibration coordinateReference must be a valid CRS") from error

    def numeric_attributes(node_name: str, names: tuple[str, ...]) -> dict[str, float]:
        node = calibration.find(node_name)
        if node is None:
            raise HTTPException(422, f"Calibration/{node_name} is required")
        result = {}
        for name in names:
            try:
                value = float(node.attrib[name])
            except (KeyError, ValueError) as error:
                raise HTTPException(422, f"Calibration/{node_name} requires numeric {name}") from error
            if not math.isfinite(value):
                raise HTTPException(422, f"Calibration/{node_name}/{name} must be finite")
            result[name] = value
        return result

    intrinsics = numeric_attributes("Intrinsics", ("fx", "fy", "cx", "cy"))
    if intrinsics["fx"] <= 0 or intrinsics["fy"] <= 0:
        raise HTTPException(422, "camera focal lengths must be positive")
    intrinsics_node = calibration.find("Intrinsics")
    if intrinsics_node.get("unit") != "pixel":
        raise HTTPException(422, "Calibration/Intrinsics unit must be pixel")
    pose_node = calibration.find("Pose")
    pose = numeric_attributes("Pose", ("x", "y", "z", "phi", "omega", "kappa"))
    if pose_node.get("positionUnit") not in {"metre", "meter", "metres", "meters", "degree", "degrees"}:
        raise HTTPException(422, "Calibration/Pose positionUnit must be explicit")
    if pose_node.get("angleUnit") not in {"degree", "degrees", "radian", "radians"}:
        raise HTTPException(422, "Calibration/Pose angleUnit must be explicit")
    if source_crs_object.is_projected and pose_node.get("positionUnit") not in {"metre", "meter", "metres", "meters"}:
        raise HTTPException(422, "projected camera coordinates must use metres")
    if source_crs_object.is_geographic and pose_node.get("positionUnit") not in {"degree", "degrees"}:
        raise HTTPException(422, "geographic camera coordinates must use degrees")
    vertical_unit = pose_node.get("verticalUnit")
    if vertical_unit not in {"metre", "meter", "metres", "meters", "foot", "feet"}:
        raise HTTPException(422, "Calibration/Pose verticalUnit must be explicit")
    distortion = numeric_attributes("Distortion", ("k1", "k2", "p1", "p2"))
    distortion_node = calibration.find("Distortion")
    if not (distortion_node.get("model") or "").strip():
        raise HTTPException(422, "Calibration/Distortion model must be explicit")
    accuracy_node = calibration.find("Accuracy")
    if accuracy_node is None or accuracy_node.get("metric") != "mean_reprojection_error":
        raise HTTPException(422, "Calibration/Accuracy mean_reprojection_error is required")
    try:
        accuracy = float(accuracy_node.attrib["value"])
    except (KeyError, ValueError) as error:
        raise HTTPException(422, "Calibration/Accuracy value must be numeric") from error
    if not math.isfinite(accuracy) or accuracy < 0:
        raise HTTPException(422, "Calibration/Accuracy value must be finite and nonnegative")
    if accuracy_node.get("unit") != "pixel":
        raise HTTPException(422, "Calibration/Accuracy unit must be pixel")

    assets_node = root.find("Assets")
    if assets_node is None:
        raise HTTPException(422, "Assets is required and must declare video.mp4")
    assets = []
    seen = set()
    for asset in assets_node.findall("Asset"):
        path = _safe_path(asset.get("path") or "")
        if path == "calibration.xml" or path in seen:
            raise HTTPException(422, "asset paths must be unique and calibration.xml is reserved")
        seen.add(path)
        role = (asset.get("role") or "").strip()
        if role not in _ALLOWED_ROLES:
            raise HTTPException(422, f"unsupported asset role for {path}")
        try:
            size_bytes = int(asset.attrib["sizeBytes"])
        except (KeyError, ValueError) as error:
            raise HTTPException(422, f"asset {path} requires integer sizeBytes") from error
        digest = (asset.get("sha256") or "").lower()
        if size_bytes <= 0 or size_bytes > MAX_FILE_BYTES or not _SHA256_RE.fullmatch(digest):
            raise HTTPException(422, f"asset {path} has invalid sizeBytes or sha256")
        media_type = (asset.get("mediaType") or mimetypes.guess_type(path)[0] or "application/octet-stream").strip()
        if len(media_type) > 200:
            raise HTTPException(422, f"asset {path} mediaType is too long")
        assets.append({"path": path, "role": role, "size_bytes": size_bytes, "sha256": digest,
                       "media_type": media_type})
    if len(assets) > MAX_ASSETS:
        raise HTTPException(413, f"at most {MAX_ASSETS} assets may be declared")
    asset_paths = sorted(seen)
    if any(right.startswith(left + "/") for left, right in zip(asset_paths, asset_paths[1:])):
        raise HTTPException(422, "an asset path cannot also be a parent directory")
    videos = [asset for asset in assets if asset["role"] == "video"]
    if len(videos) != 1 or videos[0]["path"] != "video.mp4" or videos[0]["media_type"] != "video/mp4":
        raise HTTPException(422, "exactly one required video asset must be video.mp4 with mediaType video/mp4")
    fine_calibration = [asset for asset in assets
                        if asset["role"] == "document" and asset["path"].lower().endswith(".txt")
                        and re.search(r"精标定|fine[-_ ]?calibration", asset["path"], re.IGNORECASE)]
    if not fine_calibration:
        raise HTTPException(422, "a fine-calibration TXT source file is required in the asset manifest")
    metadata = {
        "calibration": {
            "source": calibration.get("source", ""),
            "coordinate_reference": calibration.get("coordinateReference", ""),
            "coordinate_reference_status": calibration.get("coordinateReferenceStatus", "unspecified"),
            "method": calibration.get("method", ""),
            "source_axis_order": source_axis_order,
            "intrinsics_unit": calibration.find("Intrinsics").get("unit") if calibration.find("Intrinsics") is not None else None,
            "intrinsics": intrinsics,
            "pose": pose,
            "position_unit": pose_node.get("positionUnit"),
            "vertical_unit": vertical_unit,
            "angle_unit": pose_node.get("angleUnit"),
            "distortion": distortion,
            "distortion_model": calibration.find("Distortion").get("model") if calibration.find("Distortion") is not None else None,
            "mean_reprojection_error_pixels": accuracy,
            "accuracy_unit": accuracy_node.get("unit"),
        },
        "coverage": coverage_metadata,
        "spatial_index": spatial_index,
        "location_review": location_review,
    }
    video_metadata = root.find("VideoMetadata")
    if video_metadata is not None:
        metadata["video"] = dict(video_metadata.attrib)
    metadata["camera_pose"] = _camera_pose_payload(source_srs, source_axis_order, metadata["calibration"])
    camera_center = metadata["camera_pose"]["center_wgs84"]
    if camera_center["horizontal_status"] not in {"verified", "inferred"}:
        reason = "; ".join(metadata["camera_pose"].get("unavailable_reasons", [])) or "cannot transform camera position"
        raise HTTPException(422, f"camera center cannot be transformed to WGS84: {reason}")
    return name, source_srs, extent, metadata, assets


def _bbox(value: str) -> list[float]:
    try:
        bounds = [float(part) for part in value.split(",")]
    except ValueError as error:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat") from error
    if (len(bounds) != 4 or not all(math.isfinite(part) for part in bounds)
            or not (-180 <= bounds[0] < bounds[2] <= 180)
            or not (-90 <= bounds[1] < bounds[3] <= 90)):
        raise HTTPException(422, "bbox must be a valid EPSG:4326 extent")
    return bounds


def _asset_json(row: dict, group_id: str) -> dict:
    path = row["relative_path"]
    return {
        "path": path,
        "role": row["role"],
        "size_bytes": row["size_bytes"],
        "sha256": row["sha256"],
        "media_type": row["media_type"],
        "url": f"/videos/groups/{group_id}/files/{quote(path, safe='/')}" if row["status"] == "complete" else None,
        "status": row["status"],
    }


def _file_tree(assets: list[dict]) -> list[dict]:
    root: dict = {}
    for asset in assets:
        parts = asset["path"].split("/")
        node = root
        for part in parts[:-1]:
            node = node.setdefault(part, {"_children": {}})["_children"]
        node[parts[-1]] = {"_asset": asset}

    def render(nodes: dict) -> list[dict]:
        result = []
        for name in sorted(nodes, key=str.casefold):
            value = nodes[name]
            if "_asset" in value:
                result.append({"type": "file", "name": name, **value["_asset"]})
            else:
                result.append({"type": "directory", "name": name, "children": render(value["_children"])})
        return result
    return render(root)


def router(store: SpatialStore, reader: Callable, administrator: Callable, data_dir: Path) -> APIRouter:
    api = APIRouter(prefix="/videos", tags=["video-groups"])
    root = data_dir.resolve()
    staging_root = root / ".staging"
    upload_root = root / ".uploads"
    files_root = root / "groups"
    playback_secret_path = root / ".playback-secret"

    def playback_secret() -> bytes:
        try:
            descriptor = os.open(playback_secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "wb") as output:
                output.write(secrets.token_bytes(32))
        for _ in range(20):
            key = playback_secret_path.read_bytes()
            if len(key) == 32:
                return key
            time.sleep(0.01)
        raise HTTPException(503, "playback signing key unavailable")

    def playback_signature(group_id: uuid.UUID, expires: int, filename: str = "video.mp4") -> str:
        return hmac.new(playback_secret(), f"{group_id}:{expires}:{filename}".encode(), hashlib.sha256).hexdigest()

    def ensure() -> None:
        _ensure_schema(store)
        _ensure_camera_pose_backfill(store, files_root, staging_root)

    def group_row(group_id: uuid.UUID, principal: str) -> dict:
        ensure()
        visibility = "" if principal == "admin" else "AND status='published'"
        with store.connect() as connection:
            row = connection.execute(f"""SELECT id,name,status,source_srs,metadata,total_bytes,asset_count,
                created_at,published_at,ST_AsGeoJSON(spatial_extent) AS spatial_extent
                FROM video_group WHERE id=%s {visibility}""", (group_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "video group not found")
        return row

    def asset_rows(group_id: uuid.UUID) -> list[dict]:
        with store.connect() as connection:
            rows = connection.execute("""SELECT relative_path,role,size_bytes,sha256,media_type,status
                FROM video_group_asset WHERE group_id=%s ORDER BY relative_path""", (group_id,)).fetchall()
        return rows

    # Legacy low-level video upload workflow is retained for the unified package
    # adapter but hidden from Swagger as a separate upload entry point.
    @api.post("/groups", status_code=201, include_in_schema=False)
    async def create_group(request: Request, reviewer: Annotated[str, Depends(administrator)]):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/xml", "text/xml"}:
            raise HTTPException(415, "Content-Type must be application/xml")
        body = await request.body()
        name, source_srs, extent, metadata, assets = _parse_calibration_xml(body, reviewer)
        total = len(body) + sum(asset["size_bytes"] for asset in assets)
        if total > MAX_GROUP_BYTES:
            raise HTTPException(413, "video group exceeds configured total size limit")
        ensure()
        group_id = uuid.uuid4()
        stage = staging_root / str(group_id)
        calibration_path = stage / "calibration.xml"
        calibration_path.parent.mkdir(parents=True, exist_ok=True)
        calibration_path.write_bytes(body)
        calibration_hash = hashlib.sha256(body).hexdigest()
        upload_descriptors = []
        try:
            with store.connect() as connection:
                extent_json = json.dumps(extent, separators=(",", ":")) if extent is not None else None
                if extent_json is not None:
                    valid = connection.execute("""SELECT ST_IsValid(g) AS valid,ST_IsEmpty(g) AS empty,ST_Area(g) AS area
                        FROM (SELECT ST_SetSRID(ST_GeomFromGeoJSON(%s),4326) AS g) input""",
                        (extent_json,)).fetchone()
                    if not valid["valid"] or valid["empty"] or valid["area"] <= 0:
                        raise HTTPException(422, "Coverage must be a nonempty valid WGS84 polygon with positive area")
                camera = metadata["camera_pose"]["center_wgs84"]
                longitude, latitude = camera["longitude"], camera["latitude"]
                connection.execute("""INSERT INTO video_group
                    (id,name,status,source_srs,metadata,spatial_extent,camera_center,camera_center_status)
                    VALUES(%s,%s,'uploading',%s,%s,
                        CASE WHEN %s::text IS NULL THEN NULL::geometry
                             ELSE ST_SetSRID(ST_GeomFromGeoJSON(%s),4326) END,
                        CASE WHEN %s::double precision IS NULL OR %s::double precision IS NULL
                            THEN NULL ELSE ST_SetSRID(ST_MakePoint(%s::double precision,%s::double precision),4326) END,
                        %s)""",
                    (group_id, name, source_srs, Jsonb(metadata), extent_json, extent_json,
                     longitude, latitude, longitude, latitude, camera["horizontal_status"]))
                connection.execute("""INSERT INTO video_group_asset(group_id,relative_path,role,size_bytes,sha256,media_type,status)
                    VALUES(%s,'calibration.xml','calibration',%s,%s,'application/xml','complete')""",
                    (group_id, len(body), calibration_hash))
                for asset in assets:
                    upload_id = uuid.uuid4()
                    chunk_size = min(8 * 1024**2, MAX_CHUNK_BYTES)
                    connection.execute("""INSERT INTO video_group_asset(group_id,relative_path,role,size_bytes,sha256,media_type,status)
                        VALUES(%s,%s,%s,%s,%s,%s,'uploading')""",
                        (group_id, asset["path"], asset["role"], asset["size_bytes"], asset["sha256"], asset["media_type"]))
                    connection.execute("""INSERT INTO video_upload(id,group_id,relative_path,expected_size,expected_sha256,chunk_size,status)
                        VALUES(%s,%s,%s,%s,%s,%s,'uploading')""",
                        (upload_id, group_id, asset["path"], asset["size_bytes"], asset["sha256"], chunk_size))
                    upload_descriptors.append({"path": asset["path"], "upload_id": str(upload_id), "chunk_size": chunk_size})
        except Exception:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        return {
            "id": str(group_id), "name": name, "status": "uploading",
            "required_assets": ["video.mp4", "calibration.xml"],
            "calibration_file": "calibration.xml", "uploads": upload_descriptors,
            "finalize_url": f"/videos/groups/{group_id}/finalize",
        }

    @api.get("/groups")
    def list_groups(principal: Annotated[str, Depends(reader)], bbox: str | None = None,
                    spatial_filter: Literal["footprint_intersects", "camera_center_inside"] | None = None,
                    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0)):
        ensure()
        clauses = []
        params: list = []
        if principal != "admin":
            clauses.append("status='published'")
        filter_mode = spatial_filter or ("footprint_intersects" if bbox is not None else None)
        if filter_mode is not None and bbox is None:
            raise HTTPException(422, "bbox is required when spatial_filter is provided")
        if bbox is not None:
            min_lon, min_lat, max_lon, max_lat = _bbox(bbox)
            envelope = "ST_MakeEnvelope(%s,%s,%s,%s,4326)"
            if filter_mode == "camera_center_inside":
                clauses.append(f"camera_center IS NOT NULL AND camera_center_status IN ('verified','inferred') AND camera_center && {envelope} AND ST_Contains({envelope},camera_center)")
            else:
                clauses.append(f"spatial_extent && {envelope} AND ST_Intersects(spatial_extent,{envelope})")
            params.extend([min_lon,min_lat,max_lon,max_lat,min_lon,min_lat,max_lon,max_lat])
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        camera_columns = ",ST_AsGeoJSON(camera_center)::jsonb AS camera_center,camera_center_status,metadata"
        with store.connect() as connection:
            count = connection.execute(f"SELECT count(*) AS n FROM video_group {where}", params).fetchone()["n"]
            rows = connection.execute(f"""SELECT id,name,status,source_srs,total_bytes,asset_count,created_at,published_at,
                ST_AsGeoJSON(spatial_extent)::jsonb AS spatial_extent{camera_columns}
                FROM video_group {where} ORDER BY created_at DESC,id LIMIT %s OFFSET %s""",
                [*params, limit, offset]).fetchall()
        items = []
        for row in rows:
            item = {key: (str(value) if isinstance(value, uuid.UUID) else value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in row.items() if key not in {"spatial_extent", "camera_center", "camera_center_status", "metadata"}}
            item["spatial_extent"] = row["spatial_extent"]
            metadata = row["metadata"] or {}
            camera_pose = metadata.get("camera_pose", {})
            item["camera_center_wgs84"] = camera_pose.get("center_wgs84")
            item["camera_center_status"] = row["camera_center_status"]
            item["spatial_index"] = metadata.get("spatial_index", "footprint")
            if filter_mode == "footprint_intersects":
                item["match_reason"] = {
                    "condition": "footprint_intersects",
                    "geometry": "spatial_extent",
                    "meaning": "orthophoto_product_footprint",
                }
            elif filter_mode == "camera_center_inside":
                item["match_reason"] = {
                    "condition": "camera_center_inside",
                    "geometry": "calibrated_camera_center",
                    "position_status": row["camera_center_status"],
                    "temporal_status": camera_pose.get("temporal_model", {}).get("status", "unavailable"),
                    "meaning": "the single calibrated camera center is strictly inside the query bbox; this does not establish the camera path throughout the video",
                }
            items.append(item)
        return {"total": count, "limit": limit, "offset": offset, "items": items}

    @api.get("/groups/{group_id}")
    def get_group(group_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        row = group_row(group_id, principal)
        row = {key: (str(value) if isinstance(value, uuid.UUID) else value.isoformat() if isinstance(value, datetime) else value)
               for key, value in row.items() if key not in {"spatial_extent"}}
        with store.connect() as connection:
            geojson = connection.execute("SELECT ST_AsGeoJSON(spatial_extent) AS geom FROM video_group WHERE id=%s", (group_id,)).fetchone()["geom"]
        assets = [_asset_json(asset, str(group_id)) for asset in asset_rows(group_id)]
        camera_pose = (row.get("metadata") or {}).get("camera_pose")
        if camera_pose is None:
            calibration = (row.get("metadata") or {}).get("calibration", {})
            camera_pose = _camera_pose_payload(row["source_srs"], calibration.get("source_axis_order", ""), calibration)
        spatial_index = (row.get("metadata") or {}).get("spatial_index", "footprint")
        row["metadata"] = {key: value for key, value in (row.get("metadata") or {}).items() if key != "camera_pose"}
        return {**row, "spatial_extent": json.loads(geojson) if geojson else None,
                "spatial_index": spatial_index, "camera_pose": camera_pose,
                "files": assets, "directory": _file_tree(assets)}

    @api.get("/groups/{group_id}/uploads", include_in_schema=False)
    def list_uploads(group_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ensure()
        with store.connect() as connection:
            group = connection.execute("SELECT id FROM video_group WHERE id=%s", (group_id,)).fetchone()
            if group is None:
                raise HTTPException(404, "video group not found")
            rows = connection.execute("""SELECT u.id,u.relative_path,u.expected_size,u.expected_sha256,u.chunk_size,u.status,
                COALESCE(json_agg(c.chunk_index ORDER BY c.chunk_index) FILTER(WHERE c.chunk_index IS NOT NULL),'[]') AS received_chunks
                FROM video_upload u LEFT JOIN video_upload_chunk c ON c.upload_id=u.id
                WHERE u.group_id=%s GROUP BY u.id ORDER BY u.relative_path""", (group_id,)).fetchall()
        return [{**{k: (str(v) if isinstance(v, uuid.UUID) else v) for k,v in row.items()}} for row in rows]

    @api.put("/uploads/{upload_id}/chunks/{chunk_index}", include_in_schema=False)
    async def upload_chunk(upload_id: uuid.UUID, chunk_index: int, request: Request,
                           _: Annotated[str, Depends(administrator)]):
        ensure()
        if chunk_index < 0:
            raise HTTPException(422, "chunk_index must be nonnegative")
        with store.connect() as connection:
            upload = connection.execute("SELECT * FROM video_upload WHERE id=%s", (upload_id,)).fetchone()
        if upload is None:
            raise HTTPException(404, "upload not found")
        if upload["status"] == "complete":
            raise HTTPException(409, "upload is already complete")
        start = chunk_index * upload["chunk_size"]
        if start >= upload["expected_size"]:
            raise HTTPException(416, "chunk index is outside the file")
        end = min(upload["expected_size"] - 1, start + upload["chunk_size"] - 1)
        expected_length = end - start + 1
        content_range = request.headers.get("content-range", "")
        match = _CONTENT_RANGE_RE.fullmatch(content_range)
        if not match or tuple(map(int, match.groups())) != (start, end, upload["expected_size"]):
            raise HTTPException(416, "Content-Range does not match the registered chunk boundary")
        body = await request.body()
        if len(body) != expected_length or len(body) > MAX_CHUNK_BYTES:
            raise HTTPException(413, "chunk byte length does not match the registered range")
        digest = hashlib.sha256(body).hexdigest()
        with store.connect() as connection:
            existing = connection.execute("SELECT sha256 FROM video_upload_chunk WHERE upload_id=%s AND chunk_index=%s",
                                           (upload_id, chunk_index)).fetchone()
        chunk_path = upload_root / str(upload_id) / f"{chunk_index:08d}.chunk"
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None:
            if existing["sha256"] != digest:
                raise HTTPException(409, "chunk index was already uploaded with different content")
            return {"upload_id": str(upload_id), "chunk_index": chunk_index, "received": True, "idempotent": True}
        temporary = chunk_path.with_suffix(".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, chunk_path)
        try:
            with store.connect() as connection:
                connection.execute("INSERT INTO video_upload_chunk(upload_id,chunk_index,size_bytes,sha256) VALUES(%s,%s,%s,%s)",
                                   (upload_id, chunk_index, len(body), digest))
        except Exception:
            chunk_path.unlink(missing_ok=True)
            raise
        return {"upload_id": str(upload_id), "chunk_index": chunk_index, "received": True, "idempotent": False}

    @api.get("/uploads/{upload_id}", include_in_schema=False)
    def upload_status(upload_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ensure()
        with store.connect() as connection:
            row = connection.execute("SELECT * FROM video_upload WHERE id=%s", (upload_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "upload not found")
            chunks = connection.execute("SELECT chunk_index FROM video_upload_chunk WHERE upload_id=%s ORDER BY chunk_index",
                                        (upload_id,)).fetchall()
        return {"upload_id": str(upload_id), "group_id": str(row["group_id"]), "path": row["relative_path"],
                "size_bytes": row["expected_size"], "chunk_size": row["chunk_size"], "status": row["status"],
                "received_chunks": [chunk["chunk_index"] for chunk in chunks]}

    @api.post("/uploads/{upload_id}/complete", include_in_schema=False)
    def complete_upload(upload_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ensure()
        with store.connect() as connection:
            upload = connection.execute("SELECT * FROM video_upload WHERE id=%s", (upload_id,)).fetchone()
            if upload is None:
                raise HTTPException(404, "upload not found")
            if upload["status"] == "complete":
                return {"upload_id": str(upload_id), "path": upload["relative_path"], "status": "complete"}
            chunks = connection.execute("SELECT chunk_index,size_bytes FROM video_upload_chunk WHERE upload_id=%s ORDER BY chunk_index",
                                        (upload_id,)).fetchall()
        expected_count = (upload["expected_size"] + upload["chunk_size"] - 1) // upload["chunk_size"]
        if [chunk["chunk_index"] for chunk in chunks] != list(range(expected_count)):
            raise HTTPException(409, "one or more upload chunks are missing")
        stage = staging_root / str(upload["group_id"])
        target = stage / upload["relative_path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".{upload_id}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("wb") as output:
                for index in range(expected_count):
                    chunk_path = upload_root / str(upload_id) / f"{index:08d}.chunk"
                    if not chunk_path.is_file():
                        raise HTTPException(409, f"chunk {index} is missing from upload storage")
                    with chunk_path.open("rb") as source:
                        while True:
                            block = source.read(1024 * 1024)
                            if not block:
                                break
                            output.write(block)
                            digest.update(block)
                            size += len(block)
            if size != upload["expected_size"] or digest.hexdigest() != upload["expected_sha256"]:
                raise HTTPException(422, "assembled file size or SHA-256 does not match the manifest")
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        with store.connect() as connection:
            connection.execute("UPDATE video_upload SET status='complete',completed_at=now() WHERE id=%s", (upload_id,))
            connection.execute("UPDATE video_group_asset SET status='complete' WHERE group_id=%s AND relative_path=%s",
                               (upload["group_id"], upload["relative_path"]))
        shutil.rmtree(upload_root / str(upload_id), ignore_errors=True)
        return {"upload_id": str(upload_id), "path": upload["relative_path"], "size_bytes": size,
                "sha256": digest.hexdigest(), "status": "complete"}

    @api.post("/groups/{group_id}/finalize", include_in_schema=False)
    def finalize_group(group_id: uuid.UUID, _: Annotated[str, Depends(administrator)]):
        ensure()
        destination = files_root / str(group_id)
        stage = staging_root / str(group_id)
        try:
            with store.connect() as connection:
                group = connection.execute("SELECT * FROM video_group WHERE id=%s FOR UPDATE", (group_id,)).fetchone()
                if group is None:
                    raise HTTPException(404, "video group not found")
                if group["status"] == "published":
                    return {"id": str(group_id), "status": "published", "total_bytes": group["total_bytes"],
                            "asset_count": group["asset_count"]}
                review = (group["metadata"] or {}).get("location_review", {})
                if (group["spatial_extent"] is None or group["camera_center"] is None
                        or group["camera_center_status"] not in {"verified", "inferred"}
                        or review.get("status") != "confirmed"):
                    raise HTTPException(422, "cannot publish a video group without reviewed WGS84 coverage and camera position")
                assets = connection.execute("SELECT * FROM video_group_asset WHERE group_id=%s ORDER BY relative_path",
                                            (group_id,)).fetchall()
                completed_paths = {asset["relative_path"] for asset in assets if asset["status"] == "complete"}
                if "video.mp4" not in completed_paths or "calibration.xml" not in completed_paths:
                    raise HTTPException(409, "video.mp4 and calibration.xml are required before publishing")
                if len(completed_paths) != len(assets):
                    raise HTTPException(409, "all declared group files must be uploaded before publishing")
                total_bytes = sum(asset["size_bytes"] for asset in assets)
                if total_bytes > MAX_GROUP_BYTES:
                    raise HTTPException(413, "video group exceeds configured total size limit")
                for asset in assets:
                    local = stage / asset["relative_path"]
                    if not local.is_file() or local.stat().st_size != asset["size_bytes"]:
                        raise HTTPException(409, f"uploaded file is missing or has wrong size: {asset['relative_path']}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise HTTPException(409, "published file directory already exists")
                os.replace(stage, destination)
                connection.execute("""UPDATE video_group SET status='published',total_bytes=%s,asset_count=%s,published_at=now()
                    WHERE id=%s""", (total_bytes, len(assets), group_id))
            from .resource_index import index_native_assets
            index_native_assets(store,"video_group",str(group_id))
        except Exception:
            with store.connect() as connection:
                connection.execute("UPDATE video_group SET status='uploading',published_at=NULL WHERE id=%s AND status='published'",
                                   (group_id,))
            if destination.exists() and not stage.exists():
                stage.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, stage)
            raise
        return {"id": str(group_id), "status": "published", "total_bytes": total_bytes,
                "asset_count": len(assets), "url": f"/videos/groups/{group_id}"}

    @api.get("/groups/{group_id}/files/{relative_path:path}")
    def get_file(group_id: uuid.UUID, relative_path: str, principal: Annotated[str, Depends(reader)]):
        path = _safe_path(relative_path)
        group = group_row(group_id, principal)
        with store.connect() as connection:
            asset = connection.execute("""SELECT media_type,status FROM video_group_asset
                WHERE group_id=%s AND relative_path=%s""", (group_id, path)).fetchone()
        if asset is None or asset["status"] != "complete":
            raise HTTPException(404, "video group file not found")
        base = (files_root / str(group_id)).resolve() if group["status"] == "published" else (staging_root / str(group_id)).resolve()
        file_path = (base / path).resolve()
        if not file_path.is_relative_to(base) or not file_path.is_file():
            raise HTTPException(404, "video group file not found")
        return FileResponse(file_path, media_type=asset["media_type"], headers={"Cache-Control": "private, no-store"})

    @api.post("/groups/{group_id}/playback-ticket")
    def playback_ticket(group_id: uuid.UUID, principal: Annotated[str, Depends(reader)]):
        group_row(group_id, principal)
        with store.connect() as connection:
            asset = connection.execute(
                "SELECT status FROM video_group_asset WHERE group_id=%s AND relative_path='video.mp4'",
                (group_id,),
            ).fetchone()
        if asset is None or asset["status"] != "complete" or not (files_root / str(group_id) / "video.mp4").is_file():
            raise HTTPException(404, "video file not found")
        expires = int(time.time()) + 6 * 3600
        preview = files_root / str(group_id) / "preview_h264.mp4"
        filename = "preview_h264.mp4" if preview.is_file() and preview.stat().st_size > 0 else "video.mp4"
        signature = playback_signature(group_id, expires, filename)
        return {
            "url": f"/videos/groups/{group_id}/play/{filename}?expires={expires}&signature={signature}",
            "expires_at": expires,
            "codec": "h264" if filename == "preview_h264.mp4" else "source",
        }

    @api.get("/groups/{group_id}/play/video.mp4")
    def playback_file(group_id: uuid.UUID, expires: int, signature: str):
        if expires < time.time() or expires > time.time() + 6 * 3600 + 60:
            raise HTTPException(403, "playback ticket expired")
        if not hmac.compare_digest(signature, playback_signature(group_id, expires)):
            raise HTTPException(403, "invalid playback ticket")
        group = group_row(group_id, "reader")
        if group["status"] != "published":
            raise HTTPException(404, "video group not found")
        with store.connect() as connection:
            asset = connection.execute(
                "SELECT status FROM video_group_asset WHERE group_id=%s AND relative_path='video.mp4'",
                (group_id,),
            ).fetchone()
        file_path = files_root / str(group_id) / "video.mp4"
        if asset is None or asset["status"] != "complete" or not file_path.is_file():
            raise HTTPException(404, "video file not found")
        return FileResponse(file_path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    @api.get("/groups/{group_id}/play/preview_h264.mp4")
    def playback_preview(group_id: uuid.UUID, expires: int, signature: str):
        if expires < time.time() or expires > time.time() + 6 * 3600 + 60:
            raise HTTPException(403, "playback ticket expired")
        if not hmac.compare_digest(signature, playback_signature(group_id, expires, "preview_h264.mp4")):
            raise HTTPException(403, "invalid playback ticket")
        group = group_row(group_id, "reader")
        if group["status"] != "published":
            raise HTTPException(404, "video group not found")
        file_path = files_root / str(group_id) / "preview_h264.mp4"
        if not file_path.is_file():
            raise HTTPException(404, "browser preview not available")
        return FileResponse(file_path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})

    return api
