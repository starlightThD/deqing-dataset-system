#!/usr/bin/env python3
"""Validate a deqing.oblique-resource.v1 package and register its images in PostGIS."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from psycopg.types.json import Jsonb
from pyproj import CRS, Transformer

from dataset_service.oblique import SCHEMA
from dataset_service.spatial import SpatialStore


NAMESPACE = uuid.UUID("20fc4b9e-a9c6-4b98-8f78-cf5e938fd459")


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _finite(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a finite number") from error
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _rotation_determinant(rotation: list[list[float]]) -> float:
    a, b, c = rotation
    return (a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0]))


def load_package(root: Path, resource_name: str) -> tuple[dict, dict, list[dict], str]:
    """Validate provider files and generate the manifest metadata in memory."""
    root = root.resolve()
    groups_doc = json.loads((root / "camera_groups.json").read_text(encoding="utf-8"))
    groups = groups_doc.get("camera_groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("camera_groups.json must contain at least one camera group")
    group_by_id = {group.get("camera_group_id"): group for group in groups}
    if None in group_by_id or len(group_by_id) != len(groups):
        raise ValueError("camera_group_id values must be present and unique")
    for group in groups:
        group_id = str(group["camera_group_id"])
        try:
            int(group_id.removeprefix("camera_group_"))
        except ValueError as error:
            raise ValueError(f"camera_group_id must be camera_group_<integer>: {group_id}") from error
        dimensions = group.get("image_dimensions_px") or {}
        intrinsics = group.get("intrinsics") or {}
        focal = intrinsics.get("focal_length_px") or {}
        principal = intrinsics.get("principal_point_px_from_upper_left") or {}
        if int(dimensions.get("width", 0)) <= 0 or int(dimensions.get("height", 0)) <= 0:
            raise ValueError(f"invalid image dimensions in camera group {group['camera_group_id']}")
        if _finite(focal.get("fx"), "focal fx") <= 0 or _finite(focal.get("fy"), "focal fy") <= 0:
            raise ValueError(f"focal lengths must be positive in camera group {group['camera_group_id']}")
        _finite(principal.get("x"), "principal point x")
        _finite(principal.get("y"), "principal point y")

    poses_path = root / "poses.jsonl"
    records = [json.loads(line) for line in poses_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not records:
        raise ValueError("poses.jsonl must contain at least one image pose")
    provenance_path = root / "provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.is_file() else {}
    if provenance and provenance.get("schema") != "deqing.oblique-provenance.v1":
        raise ValueError("unsupported provenance.json schema")

    images_root = (root / "images").resolve()
    if not images_root.is_dir():
        raise ValueError("package must contain an images/ directory")
    actual_image_files = {p.resolve() for p in images_root.rglob("*") if p.is_file()}
    if not actual_image_files:
        raise ValueError("images/ must contain at least one original image")

    source_crs = None
    source_crs_object = None
    to_wgs84 = None
    seen_paths: set[str] = set()
    seen_names: set[str] = set()
    source_centers: list[list[float]] = []
    wgs84_centers: list[list[float]] = []
    axis_order = None
    vertical_datums: set[str] = set()
    camera_axes = None
    world_axes = None
    for record in records:
        image = record.get("image") or {}
        relative = Path(image.get("path", ""))
        target = (root / relative).resolve()
        filename = image.get("filename") or target.name
        if not target.is_relative_to(images_root) or not target.is_file():
            raise ValueError(f"image path is missing or outside images/: {relative}")
        if target in seen_paths or filename in seen_names or Path(filename).name != filename:
            raise ValueError(f"image paths and filenames must be unique safe names: {filename}")
        if target.suffix.lower() not in {".jpg", ".jpeg", ".png", ".tif", ".tiff"}:
            raise ValueError(f"unsupported image format: {filename}")
        seen_paths.add(target)
        seen_names.add(filename)
        image["path"] = relative.as_posix()
        image["filename"] = filename
        actual_size, actual_hash = _sha256(target)
        if image.get("size_bytes") is not None and int(image["size_bytes"]) != actual_size:
            raise ValueError(f"declared image size does not match file: {filename}")
        if image.get("sha256") and str(image["sha256"]).lower() != actual_hash:
            raise ValueError(f"declared SHA-256 does not match file: {filename}")
        image["size_bytes"] = actual_size
        image["sha256"] = actual_hash
        if record.get("camera_group_id") not in group_by_id:
            raise ValueError(f"unknown camera group for {filename}")
        pose_status = record.get("pose_status")
        if not isinstance(pose_status, str) or not pose_status.strip() or pose_status.lower().startswith("unavailable"):
            raise ValueError(f"pose is missing or unavailable for {filename}")
        center = record.get("camera_center") or {}
        current_crs = center.get("crs")
        if not current_crs:
            raise ValueError(f"camera center CRS is required for {filename}")
        if source_crs is None:
            source_crs = str(current_crs)
            source_crs_object = CRS.from_user_input(source_crs)
            to_wgs84 = Transformer.from_crs(source_crs, "EPSG:4326", always_xy=True)
        elif str(current_crs) != source_crs:
            raise ValueError("all camera centers in one package must use the same source CRS")
        coords = center.get("coordinates")
        if not isinstance(coords, list) or len(coords) != 3:
            raise ValueError(f"camera center must contain three coordinates: {filename}")
        coords = [_finite(value, f"camera center coordinate {index} for {filename}") for index, value in enumerate(coords)]
        current_axis_order = center.get("axis_order")
        if not isinstance(current_axis_order, list) or len(current_axis_order) != 3:
            raise ValueError(f"camera center axis_order must declare three axes: {filename}")
        expected_xy = ["longitude", "latitude"] if source_crs_object.is_geographic else ["easting", "northing"]
        if current_axis_order != [*expected_xy, "height_m"]:
            raise ValueError(f"camera coordinates must use CRS X,Y and metre height order {[*expected_xy, 'height_m']}: {filename}")
        if axis_order is None:
            axis_order = current_axis_order
        elif current_axis_order != axis_order:
            raise ValueError("camera center axis_order must be consistent across the package")
        pose = record.get("camera_to_world_matrix") or {}
        current_camera_axes = pose.get("camera_axes")
        current_world_axes = pose.get("world_axes")
        if not isinstance(current_camera_axes, dict) or not isinstance(current_world_axes, dict):
            raise ValueError(f"camera_axes and world_axes are required for {filename}")
        if camera_axes is None:
            camera_axes, world_axes = current_camera_axes, current_world_axes
        elif current_camera_axes != camera_axes or current_world_axes != world_axes:
            raise ValueError("camera/world axis conventions must be consistent across the package")
        matrix = pose.get("matrix")
        if not isinstance(matrix, list) or len(matrix) != 4 or any(not isinstance(row, list) or len(row) != 4 for row in matrix):
            raise ValueError(f"invalid 4x4 camera_to_world matrix: {filename}")
        matrix = [[_finite(value, f"matrix value for {filename}") for value in row] for row in matrix]
        if pose.get("translation_crs") != source_crs or any(abs(matrix[i][3] - coords[i]) > 1e-5 for i in range(3)):
            raise ValueError(f"camera matrix translation disagrees with center or CRS: {filename}")
        if any(abs(matrix[3][i] - [0.0, 0.0, 0.0, 1.0][i]) > 1e-8 for i in range(4)):
            raise ValueError(f"invalid homogeneous matrix last row: {filename}")
        rotation = [row[:3] for row in matrix[:3]]
        for row_index in range(3):
            for other_index in range(3):
                dot = sum(rotation[row_index][k] * rotation[other_index][k] for k in range(3))
                if abs(dot - (1.0 if row_index == other_index else 0.0)) > 1e-3:
                    raise ValueError(f"rotation matrix is not orthonormal: {filename}")
        if abs(_rotation_determinant(rotation) - 1.0) > 1e-3:
            raise ValueError(f"rotation matrix determinant must be approximately +1: {filename}")
        lon, lat = to_wgs84.transform(coords[0], coords[1])
        point = record.get("camera_center_wgs84")
        if point:
            if (abs(lon - _finite(point.get("longitude"), f"longitude for {filename}")) > 1e-6
                    or abs(lat - _finite(point.get("latitude"), f"latitude for {filename}")) > 1e-6
                    or abs(coords[2] - _finite(point.get("height_m"), f"height for {filename}")) > 1e-4):
                raise ValueError(f"WGS84 camera center disagrees with source CRS: {filename}")
        else:
            point = {"longitude": lon, "latitude": lat, "height_m": coords[2],
                     "vertical_datum": center.get("vertical_datum", "unknown; no vertical transformation applied")}
            record["camera_center_wgs84"] = point
        source_centers.append(coords)
        wgs84_centers.append([lon, lat])
        vertical_datums.add(str(center.get("vertical_datum") or "unknown"))

    if seen_paths != actual_image_files:
        unreferenced = sorted(p.relative_to(root).as_posix() for p in actual_image_files - seen_paths)
        raise ValueError(f"every file in images/ must have exactly one pose record; unreferenced: {unreferenced[:5]}")

    crs = source_crs_object
    provenance = {"schema": "deqing.oblique-provenance.v1", **provenance}
    manifest = {
        "schema": "deqing.oblique-resource.v1",
        "resource_name": resource_name,
        "data_type": "oblique_imagery",
        "image_count": len(records),
        "image_bytes_total": sum(record["image"]["size_bytes"] for record in records),
        "files": {"images": "images/", "camera_groups": "camera_groups.json", "poses": "poses.jsonl",
                  **({"provenance": "provenance.json"} if provenance_path.is_file() else {})},
        "source": provenance,
        "spatial_reference": {
            "source_crs": source_crs,
            "source_crs_wkt": crs.to_wkt(),
            "axis_order": axis_order,
            "camera_center_bounds_wgs84": [min(p[0] for p in wgs84_centers), min(p[1] for p in wgs84_centers),
                                           max(p[0] for p in wgs84_centers), max(p[1] for p in wgs84_centers)],
            "camera_center_height_range_m": [min(p[2] for p in source_centers), max(p[2] for p in source_centers)],
            "vertical_datum": sorted(vertical_datums),
        },
        "pose_convention": {"canonical": "camera_to_world 4x4 matrix in source CRS",
                            "camera_axes": camera_axes, "world_axes": world_axes},
        "coverage": {"status": "unavailable", "footprints_included": False,
                     "reason": "No surface/depth model was supplied; camera centers are not image coverage."},
        "validation": {"all_images_have_exactly_one_pose": True, "all_files_hashed_by_importer": True,
                       "matrix_translation_matches_camera_center": True,
                       "wgs84_centers_recomputed_from_source_crs": True},
    }
    digest_input = {"manifest": {key: value for key, value in manifest.items() if key != "resource_name"},
                    "camera_groups": groups, "poses": records}
    package_digest = hashlib.sha256(json.dumps(digest_input, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    return manifest, group_by_id, records, package_digest


def copy_images(root: Path, records: list[dict], data_dir: Path, resource_id: uuid.UUID) -> None:
    image_root = (data_dir / "oblique-images" / str(resource_id)).resolve()
    image_root.mkdir(parents=True, exist_ok=True)
    for record in records:
        image = record["image"]
        source = (root / image["path"]).resolve()
        target = (image_root / image["filename"]).resolve()
        if not target.is_relative_to(image_root):
            raise ValueError(f"unsafe image filename: {image['filename']}")
        size, sha256 = _sha256(source)
        if size != int(image["size_bytes"]) or sha256 != image["sha256"].lower():
            raise ValueError(f"size or SHA-256 mismatch: {image['filename']}")
        if target.is_file() and target.stat().st_size == size:
            _, existing_digest = _sha256(target)
            if existing_digest == sha256:
                continue
        temporary = target.with_suffix(target.suffix + ".partial")
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != size:
            temporary.unlink(missing_ok=True)
            raise IOError(f"copy size mismatch: {image['filename']}")
        temporary.replace(target)


def import_package(root: Path, manifest: dict, groups: dict, records: list[dict],
                   store: SpatialStore, data_dir: Path, resource_id: uuid.UUID,
                   location_review_source: str) -> dict:
    resource_name = manifest.get("resource_name") or "未命名倾斜影像资源"
    source_srs = manifest["spatial_reference"]["source_crs"]
    location_review = {
        "status": "confirmed",
        "confirmed": True,
        "source_crs": source_srs,
        "evidence": location_review_source.strip(),
        "scope": ["source_crs", "camera_centers", "camera_to_world_pose"],
        "reviewed_by": os.getenv("USER") or "operator",
        "absolute_accuracy": "unknown",
        "ground_control_points_used": manifest.get("source", {}).get("ground_control_points_used"),
        "coverage_accuracy": "unavailable_without_surface_model",
    }
    image_root = (data_dir / "oblique-images").resolve()
    store.ensure_schema()
    with store.connect() as db:
        db.execute(SCHEMA)
        db.execute("""INSERT INTO oblique_resource
            (id,name,source_srs,assumed_agl_m,ground_window_m,buffer_m,max_horizontal_factor,location_review,metadata)
            VALUES (%s,%s,%s,NULL,NULL,NULL,NULL,%s,%s)
            ON CONFLICT (id) DO UPDATE SET name=excluded.name,source_srs=excluded.source_srs,
                location_review=excluded.location_review,metadata=excluded.metadata,indexed_at=now()""",
            (resource_id, resource_name, source_srs, Jsonb(location_review), Jsonb(manifest)))
        for group_id, group in groups.items():
            intrinsics = group["intrinsics"]
            focal = intrinsics["focal_length_px"]
            principal = intrinsics["principal_point_px_from_upper_left"]
            dimensions = group["image_dimensions_px"]
            distortion = group.get("distortion", {}).get("coefficients", {})
            db.execute("""INSERT INTO oblique_group
                (resource_id,group_id,width,height,focal_mm,sensor_mm,focal_px,principal_x,principal_y,distortion,
                 focal_x_px,focal_y_px,principal_x_px,principal_y_px,source_model)
                VALUES (%s,%s,%s,%s,NULL,NULL,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (resource_id,group_id) DO UPDATE SET width=excluded.width,height=excluded.height,
                    focal_mm=NULL,sensor_mm=NULL,focal_px=excluded.focal_px,principal_x=excluded.principal_x,
                    principal_y=excluded.principal_y,distortion=excluded.distortion,
                    focal_x_px=excluded.focal_x_px,focal_y_px=excluded.focal_y_px,
                    principal_x_px=excluded.principal_x_px,principal_y_px=excluded.principal_y_px,
                    source_model=excluded.source_model""",
                (resource_id, int(group_id.removeprefix("camera_group_")), dimensions["width"], dimensions["height"],
                 (float(focal["fx"])+float(focal["fy"]))/2, float(principal["x"]), float(principal["y"]),
                 Jsonb(distortion), float(focal["fx"]), float(focal["fy"]), float(principal["x"]),
                 float(principal["y"]), Jsonb(group)))
        for source_photo_id, record in enumerate(records, 1):
            image = record["image"]
            group_id = int(record["camera_group_id"].removeprefix("camera_group_"))
            image_id = uuid.uuid5(resource_id, image["filename"])
            center = record["camera_center"]["coordinates"]
            wgs84 = record["camera_center_wgs84"]
            rotation = [row[:3] for row in record["camera_to_world_matrix"]["matrix"][:3]]
            capture = record.get("capture_time", {}).get("value")
            capture_time = datetime.fromisoformat(capture) if capture else None
            storage_key = f"{resource_id}/{image['filename']}"
            assumptions = {
                "coverage_status": "unavailable",
                "coverage_method": "not_computed_no_dem_or_depth",
                "footprint_is_null_by_design": True,
                "camera_center_is_not_image_coverage": True,
                "absolute_geolocation_accuracy": "unknown",
                "vertical_datum": wgs84.get("vertical_datum"),
            }
            db.execute("""INSERT INTO oblique_image
                (id,resource_id,group_id,source_photo_id,source_xml_id,file_key,storage_backend,storage_key,
                 file_status,size_bytes,sha256,center_x,center_y,center_z,rotation,camera_center,candidate_footprint,
                 coverage_status,coverage_method,coverage_assumptions,capture_time_local,source_pose)
                VALUES (%s,%s,%s,%s,NULL,%s,'data-volume',%s,'present',%s,%s,%s,%s,%s,%s,
                        ST_SetSRID(ST_MakePoint(%s,%s),4326),NULL,'unavailable_no_surface_model',
                        'not_computed_no_dem_or_depth',%s,%s,%s)
                ON CONFLICT (id) DO UPDATE SET group_id=excluded.group_id,source_photo_id=excluded.source_photo_id,
                    file_key=excluded.file_key,storage_backend=excluded.storage_backend,storage_key=excluded.storage_key,
                    file_status=excluded.file_status,size_bytes=excluded.size_bytes,sha256=excluded.sha256,
                    center_x=excluded.center_x,center_y=excluded.center_y,center_z=excluded.center_z,
                    rotation=excluded.rotation,camera_center=excluded.camera_center,candidate_footprint=NULL,
                    coverage_status=excluded.coverage_status,coverage_method=excluded.coverage_method,
                    coverage_assumptions=excluded.coverage_assumptions,capture_time_local=excluded.capture_time_local,
                    source_pose=excluded.source_pose,indexed_at=now()""",
                (image_id, resource_id, group_id, source_photo_id, f"{resource_id}/{image['filename']}", storage_key,
                 image["size_bytes"], image["sha256"], center[0], center[1], center[2], Jsonb(rotation),
                 wgs84["longitude"], wgs84["latitude"], Jsonb(assumptions), capture_time, Jsonb(record["source_pose"])))
    return {"resource_id": str(resource_id), "name": resource_name, "images": len(records),
            "bytes": sum(int(record["image"]["size_bytes"]) for record in records),
            "stored_at": image_root.as_posix(), "coverage": "not computed; camera centers and poses are registered"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="path to a provider package (no manifest.json required)")
    parser.add_argument("--name", help="resource name; defaults to the package directory name")
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DEQING_DATA_DIR", "/workspace/data")))
    parser.add_argument("--resource-id", type=uuid.UUID, help="stable ID; defaults to a deterministic package ID")
    parser.add_argument("--validate-only", action="store_true", help="validate package and print generated metadata without importing")
    parser.add_argument("--confirm-location-review", action="store_true",
                        help="confirm the source CRS, axis order and camera centers were reviewed")
    parser.add_argument("--location-review-source",
                        help="review evidence such as the source CRS/pose file or independent GIS inspection")
    parser.add_argument("--manifest-output", type=Path, help="optional path for a generated manifest copy; never writes into the input package by default")
    args = parser.parse_args()
    root = args.package.resolve()
    resource_name = args.name or root.name
    manifest, groups, records, package_digest = load_package(root, resource_name)
    resource_id = args.resource_id or uuid.uuid5(NAMESPACE, "deqing-oblique-package:" + package_digest)
    manifest["resource_id"] = str(resource_id)
    if args.manifest_output:
        output = args.manifest_output.resolve()
        if output.is_relative_to(root):
            raise ValueError("--manifest-output must be outside the provider package")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.validate_only:
        print(json.dumps({"valid": True, "resource_id": str(resource_id), "manifest": manifest},
                         ensure_ascii=False, indent=2))
        return
    if not args.confirm_location_review:
        parser.error("--confirm-location-review is required before importing map locations")
    if not args.location_review_source or len(args.location_review_source.strip()) < 5:
        parser.error("--location-review-source must identify the reviewed source")
    store = SpatialStore(args.data_dir.resolve())
    store.ensure_schema()
    with store.connect() as db:
        db.execute(SCHEMA)
        if db.execute("SELECT 1 FROM oblique_resource WHERE id=%s",(resource_id,)).fetchone():
            raise ValueError("published oblique resource ID is immutable; use a new resource ID")
    copy_images(root, records, args.data_dir.resolve(), resource_id)
    result = import_package(root, manifest, groups, records, store,
                            args.data_dir.resolve(), resource_id, args.location_review_source)
    from dataset_service.resource_index import index_native_assets
    result["file_index"] = index_native_assets(store, "oblique_resource", str(resource_id))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
