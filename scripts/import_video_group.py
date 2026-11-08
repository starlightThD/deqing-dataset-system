#!/usr/bin/env python3
"""Normalize, describe, upload, and publish one georeferenced video group."""

import argparse
import hashlib
import json
import mimetypes
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


# Source paths from the received deliverables map to stable package paths.
# Video files may have different source names; every package uses video.mp4.
SOURCE_ASSETS = {
    "evidence/coarse-search-match.png": ("01_粗搜索匹配图.png", "evidence"),
    "evidence/fine-search-match.png": ("02_精搜索匹配图.png", "evidence"),
    "evidence/coverage-sketch.png": ("03_范围示意图.png", "evidence"),
    "evidence/rough-calibration.txt": ("04_粗标定结果.txt", "document"),
    "evidence/fine-calibration-source.txt": ("05_精标定结果.txt", "document"),
    "model/region.obj": ("06_裁剪OBJ/region.obj", "model"),
    "model/region.mtl": ("06_裁剪OBJ/region.mtl", "model"),
    "evidence/projection.png": ("07_投影图.png", "evidence"),
    "evidence/fine-match.png": ("08_精匹配图.png", "evidence"),
    "raster/orthophoto.tif": ("09_正射叠加图.tif", "raster"),
}


def parse_calibration(path: Path) -> dict[str, float]:
    text = path.read_text(encoding="utf-8", errors="replace")
    patterns = {
        "fx": r"\bfx\s*=\s*([-+\d.eE]+)",
        "fy": r"\bfy\s*=\s*([-+\d.eE]+)",
        "cx": r"\bcx\s*=\s*([-+\d.eE]+)",
        "cy": r"\bcy\s*=\s*([-+\d.eE]+)",
        "x": r"\bXs\s*=\s*([-+\d.eE]+)",
        "y": r"\bYs\s*=\s*([-+\d.eE]+)",
        "z": r"\bZs\s*=\s*([-+\d.eE]+)",
        "phi": r"\bphi\s*=\s*([-+\d.eE]+)",
        "omega": r"\bomega\s*=\s*([-+\d.eE]+)",
        "kappa": r"\bkappa\s*=\s*([-+\d.eE]+)",
        "k1": r"\bk1\s*=\s*([-+\d.eE]+)",
        "k2": r"\bk2\s*=\s*([-+\d.eE]+)",
        "p1": r"\bp1\s*=\s*([-+\d.eE]+)",
        "p2": r"\bp2\s*=\s*([-+\d.eE]+)",
        "mean_reprojection_error": r"平均重投影误差\s*[:：]\s*([-+\d.eE]+)",
    }
    values = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, text)
        if not match:
            raise ValueError(f"could not parse {name} from {path.name}")
        values[name] = float(match.group(1))
    return values


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def geotiff_coverage(path: Path) -> tuple[str, list[tuple[float, float]], tuple[float, float, float, float]]:
    try:
        import rasterio
        from pyproj import Transformer
    except ImportError as error:
        raise RuntimeError("preparing this group requires rasterio and pyproj on the import workstation") from error
    with rasterio.open(path) as dataset:
        if dataset.crs is None:
            raise ValueError("orthophoto GeoTIFF has no CRS; cannot create a reliable geographic query extent")
        epsg = dataset.crs.to_epsg()
        source_crs = f"EPSG:{epsg}" if epsg else dataset.crs.to_string()
        transformer = Transformer.from_crs(dataset.crs, "EPSG:4326", always_xy=True)
        bounds = dataset.bounds
        corners = [
            (bounds.left, bounds.bottom),
            (bounds.right, bounds.bottom),
            (bounds.right, bounds.top),
            (bounds.left, bounds.top),
            (bounds.left, bounds.bottom),
        ]
        # Densify projected edges before transforming so the WGS84 polygon
        # follows the actual projected rectangle instead of just four corners.
        dense = []
        steps = 16
        for (x1, y1), (x2, y2) in zip(corners, corners[1:]):
            for index in range(steps):
                t = index / steps
                dense.append(transformer.transform(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t))
        dense.append(dense[0])
        return source_crs, dense, (float(bounds.left), float(bounds.bottom), float(bounds.right), float(bounds.top))


def video_metadata(path: Path) -> dict[str, str]:
    try:
        output = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_name,width,height,duration,avg_frame_rate", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
        streams = json.loads(output.stdout).get("streams", [])
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    if not streams:
        return {}
    stream = streams[0]
    keys = ("codec_name", "width", "height", "duration", "avg_frame_rate")
    return {key: str(stream[key]) for key in keys if stream.get(key) is not None}


def make_xml(source_dir: Path, group_name: str | None = None,
             location_review_source: str | None = None) -> tuple[bytes, dict[str, Path]]:
    fine = source_dir / "05_精标定结果.txt"
    orthophoto = source_dir / "09_正射叠加图.tif"
    if not fine.is_file() or not orthophoto.is_file():
        raise FileNotFoundError("the fine calibration text and georeferenced orthophoto are required")
    if not location_review_source or len(location_review_source.strip()) < 5:
        raise ValueError("a traceable location review source is required before generating an upload package")
    calibration = parse_calibration(fine)
    source_crs, polygon, projected_bounds = geotiff_coverage(orthophoto)
    pose_inside_orthophoto_bbox = (
        projected_bounds[0] <= calibration["x"] <= projected_bounds[2]
        and projected_bounds[1] <= calibration["y"] <= projected_bounds[3]
    )
    video_candidates = sorted(source_dir.glob("*.mp4"))
    if len(video_candidates) != 1:
        raise ValueError(f"expected exactly one MP4 video in {source_dir}, found {len(video_candidates)}")
    mapped: dict[str, Path] = {"video.mp4": video_candidates[0]}
    for target, (source, _) in SOURCE_ASSETS.items():
        path = source_dir / source
        if not path.is_file():
            raise FileNotFoundError(f"required source asset is missing: {source}")
        mapped[target] = path

    root = ET.Element("VideoGroup", {
        "contractVersion": "1.0", "name": group_name or source_dir.name,
    })
    ET.SubElement(root, "SpatialReference", {
        "horizontal": source_crs,
        "axisOrder": "easting,northing",
        "source": "raster/orthophoto.tif GeoTIFF metadata",
    })
    coverage = ET.SubElement(root, "Coverage", {
        "geometryCRS": "EPSG:4326",
        "sourceCRS": source_crs,
        "source": "raster/orthophoto.tif",
        "semantics": "orthophoto_product_footprint",
    })
    polygon_element = ET.SubElement(coverage, "Polygon", {"coordinateOrder": "longitude,latitude"})
    for lon, lat in polygon:
        ET.SubElement(polygon_element, "Point", {"lon": f"{lon:.12f}", "lat": f"{lat:.12f}"})
    ET.SubElement(root, "LocationReview", {
        "status": "confirmed",
        "source": location_review_source.strip(),
        "coordinateOrder": "longitude,latitude",
        "fields": "source_crs,axis_order,camera_center_wgs84,coverage_wgs84",
    })

    calibration_node = ET.SubElement(root, "Calibration", {
        "source": "05_精标定结果.txt",
        "coordinateReference": source_crs,
        "coordinateReferenceStatus": "inferred",
        "method": "self_calibration",
    })
    ET.SubElement(calibration_node, "Intrinsics", {
        "fx": f"{calibration['fx']:.8f}", "fy": f"{calibration['fy']:.8f}",
        "cx": f"{calibration['cx']:.8f}", "cy": f"{calibration['cy']:.8f}",
        "unit": "pixel",
    })
    ET.SubElement(calibration_node, "Pose", {
        "x": f"{calibration['x']:.8f}", "y": f"{calibration['y']:.8f}",
        "z": f"{calibration['z']:.8f}", "phi": f"{calibration['phi']:.8f}",
        "omega": f"{calibration['omega']:.8f}", "kappa": f"{calibration['kappa']:.8f}",
        "positionUnit": "metre", "verticalUnit": "metre", "angleUnit": "degree",
        "insideOrthophotoBBox": str(pose_inside_orthophoto_bbox).lower(),
    })
    ET.SubElement(calibration_node, "Distortion", {
        "model": "Brown-Conrady",
        **{key: f"{calibration[key]:.8f}" for key in ("k1", "k2", "p1", "p2")},
    })
    ET.SubElement(calibration_node, "Accuracy", {
        "metric": "mean_reprojection_error",
        "value": f"{calibration['mean_reprojection_error']:.4f}",
        "unit": "pixel",
    })
    notes = ET.SubElement(root, "QualityNotes")
    ET.SubElement(notes, "Note", {"severity": "warning", "code": "POSE_CRS_INFERRED"}).text = (
        "Fine calibration text does not name its CRS; the pose CRS is inferred from the georeferenced orthophoto and coordinate values."
    )
    if not pose_inside_orthophoto_bbox:
        ET.SubElement(notes, "Note", {"severity": "warning", "code": "POSE_OUTSIDE_PRODUCT_FOOTPRINT"}).text = (
            "The calibrated camera center lies outside the orthophoto product bounds. The query geometry represents the orthophoto footprint, not the full video field of view."
        )
    material_file = source_dir / "06_裁剪OBJ" / "region.mtl"
    if material_file.is_file() and any(
        line.lstrip().lower().startswith(("map_kd ", "map_ka ", "map_ks ", "map_bump ", "bump "))
        for line in material_file.read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        ET.SubElement(notes, "Note", {
            "severity": "warning", "code": "MODEL_TEXTURE_DEPENDENCIES_NOT_BUNDLED",
        }).text = (
            "The OBJ material file references texture dependencies that are not included in this source directory; "
            "the OBJ/MTL files are preserved, but the model may not render with complete textures."
        )

    video_info = video_metadata(mapped["video.mp4"])
    if video_info:
        ET.SubElement(root, "VideoMetadata", video_info)
    assets_node = ET.SubElement(root, "Assets")
    for target, path in mapped.items():
        role = "video" if target == "video.mp4" else SOURCE_ASSETS[target][1]
        ET.SubElement(assets_node, "Asset", {
            "path": target,
            "role": role,
            "sizeBytes": str(path.stat().st_size),
            "sha256": sha256(path),
            "mediaType": mimetypes.guess_type(target)[0] or "application/octet-stream",
        })
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    return body, mapped


def read_token(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8")) if path.suffix.lower() == ".json" else path.read_text(encoding="utf-8").strip()
    if isinstance(value, dict):
        token = value.get("admin")
    else:
        token = value
    if not isinstance(token, str) or not token:
        raise ValueError("token file must contain an admin string or JSON object with an admin field")
    return token


def request_json(base_url: str, path: str, token: str, method: str = "GET", body: bytes | None = None,
                 content_type: str = "application/json", extra_headers: dict[str, str] | None = None,
                 timeout: int = 180):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(base_url.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} for {method} {path}: {detail}") from error


def upload_file(base_url: str, token: str, source: Path, upload: dict, dry_run: bool = False) -> None:
    upload_id = upload["upload_id"]
    status = request_json(base_url, f"/videos/uploads/{upload_id}", token)
    chunk_size = status["chunk_size"]
    total = status["size_bytes"]
    received = set(status["received_chunks"])
    count = (total + chunk_size - 1) // chunk_size
    for index in range(count):
        if index in received:
            continue
        start = index * chunk_size
        end = min(total - 1, start + chunk_size - 1)
        with source.open("rb") as stream:
            stream.seek(start)
            block = stream.read(end - start + 1)
        if len(block) != end - start + 1:
            raise RuntimeError(f"source file changed while uploading: {source}")
        if dry_run:
            continue
        for attempt in range(5):
            try:
                request_json(
                    base_url, f"/videos/uploads/{upload_id}/chunks/{index}", token,
                    method="PUT", body=block, content_type="application/octet-stream",
                    extra_headers={"Content-Range": f"bytes {start}-{end}/{total}"}, timeout=240,
                )
                break
            except (RuntimeError, urllib.error.URLError, TimeoutError):
                if attempt == 4:
                    raise
                time.sleep(min(2 ** attempt, 20))
        if (index + 1) % max(1, count // 20) == 0 or index + 1 == count:
            print(f"  chunks {index + 1}/{count}", flush=True)
    if not dry_run:
        request_json(base_url, f"/videos/uploads/{upload_id}/complete", token, method="POST", body=b"{}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--base-url", default="http://192.168.150.41:7110")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--group-name", help="display name stored in the service; defaults to the source directory name")
    parser.add_argument("--location-review-source", required=True,
                        help="review evidence, such as the checked CRS/control-point document and orthophoto metadata")
    parser.add_argument("--resume-group", help="continue an existing uploading group")
    parser.add_argument("--dry-run", action="store_true", help="generate calibration.xml and inspect upload plan only")
    args = parser.parse_args()
    source_dir = args.source_dir.resolve()
    if not source_dir.is_dir():
        parser.error(f"source directory does not exist: {source_dir}")
    token = read_token(args.token_file)
    calibration_xml, mapped = make_xml(source_dir, args.group_name, args.location_review_source)
    xml_path = source_dir / "calibration.xml"
    xml_path.write_bytes(calibration_xml)
    print(f"Generated {xml_path} ({len(calibration_xml)} bytes)")
    print(f"Package files: {len(mapped) + 1}; payload bytes: {sum(p.stat().st_size for p in mapped.values()) + len(calibration_xml)}")

    if args.dry_run:
        print("Dry run: no data was sent to 7110.")
        return 0

    state_path = source_dir / ".7110-video-upload.json"
    if args.resume_group:
        group_id = args.resume_group
        uploads = request_json(args.base_url, f"/videos/groups/{group_id}/uploads", token)
        by_path = {row["relative_path"]: row["id"] for row in uploads}
        upload_plan = [{"path": path, "upload_id": by_path[path]} for path in mapped]
    elif state_path.is_file():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("base_url") != args.base_url:
            raise RuntimeError("upload state belongs to a different base URL; pass --resume-group explicitly")
        group_id = state["group_id"]
        uploads = request_json(args.base_url, f"/videos/groups/{group_id}/uploads", token)
        by_path = {row["relative_path"]: row["id"] for row in uploads}
        upload_plan = [{"path": path, "upload_id": by_path[path]} for path in mapped]
    else:
        result = request_json(args.base_url, "/videos/groups", token, method="POST", body=calibration_xml,
                              content_type="application/xml")
        group_id = result["id"]
        upload_plan = result["uploads"]
        state_path.write_text(json.dumps({"base_url": args.base_url, "group_id": group_id}, indent=2) + "\n", encoding="utf-8")
        print(f"Created group {group_id}")

    for upload in upload_plan:
        target = upload["path"]
        source = mapped[target]
        print(f"Uploading {target} ({source.stat().st_size} bytes)", flush=True)
        upload_file(args.base_url, token, source, upload)

    published = request_json(args.base_url, f"/videos/groups/{group_id}/finalize", token, method="POST", body=b"{}")
    detail = request_json(args.base_url, f"/videos/groups/{group_id}", token)
    points = detail["spatial_extent"]["coordinates"][0]
    coverage_bbox = [
        min(point[0] for point in points), min(point[1] for point in points),
        max(point[0] for point in points), max(point[1] for point in points),
    ]
    print(json.dumps({"published": published, "readback": {
        "name": detail["name"], "status": detail["status"], "file_count": len(detail["files"]),
        "total_bytes": detail["total_bytes"], "coverage_bbox_wgs84": coverage_bbox,
        "files": [item["path"] for item in detail["files"]],
    }}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)
