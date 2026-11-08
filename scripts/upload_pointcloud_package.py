#!/usr/bin/env python3
"""Upload a deqing.pointcloud-package.v1 PLY or 3D Tiles directory to 7110."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


CHUNK_SIZE = 8 * 1024 * 1024
MAX_FILE_BYTES = 24 * 1024**3
MAX_VERSION_BYTES = 100 * 1024**3
def read_token(path: Path | None) -> str:
    raw = path.read_text(encoding="utf-8").strip() if path else os.getenv("DEQING_ADMIN_TOKEN", "").strip()
    if not raw:
        raise ValueError("provide --token-file or DEQING_ADMIN_TOKEN")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        value = raw
    token = value.get("admin") if isinstance(value, dict) else value
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token file must be a plain token or JSON object with an admin field")
    return token.strip()


def request_json(base_url: str, path: str, token: str, method: str = "GET",
                 body: dict | None = None, raw: bytes | None = None,
                 headers: dict[str, str] | None = None) -> dict:
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = raw
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    req = Request(base_url.rstrip("/") + path, data=data, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=300) as response:
            payload = response.read()
            return json.loads(payload.decode("utf-8")) if payload else {}
    except HTTPError as error:
        detail = error.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} {method} {path}: {detail}") from error


def package_files(package_root: Path, manifest: dict) -> tuple[list[tuple[str, Path, int, str]], Path]:
    payload_name = manifest.get("files_dir", "files")
    payload_root = (package_root / payload_name).resolve()
    if payload_root == package_root or not payload_root.is_relative_to(package_root) or not payload_root.is_dir():
        raise ValueError("files_dir must name a directory inside the package")
    entries = []
    total = 0
    for path in sorted(payload_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed in the data package: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(payload_root).as_posix()
        if not relative or any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError(f"unsafe relative file path: {relative}")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"empty files are not accepted: {relative}")
        if size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds the 24 GiB per-file limit: {relative}")
        total += size
        entries.append((relative, path, size, ""))
    if not entries:
        raise ValueError("files_dir contains no files")
    if total > MAX_VERSION_BYTES:
        raise ValueError("package exceeds the 100 GiB version limit")
    fmt = manifest.get("format")
    entrypoint = manifest.get("entrypoint") or ("tileset.json" if fmt == "3dtiles" else "cloud.ply")
    if (entrypoint, payload_root / entrypoint) not in [(item[0], item[1]) for item in entries]:
        raise ValueError(f"entrypoint is missing from files_dir: {entrypoint}")
    if fmt == "ply":
        if len(entries) != 1 or Path(entrypoint).suffix.lower() != ".ply":
            raise ValueError("PLY package must contain exactly one .ply file as its entrypoint")
    elif fmt == "3dtiles":
        if entrypoint != "tileset.json":
            raise ValueError("3D Tiles package entrypoint must be tileset.json")
    else:
        raise ValueError("format must be ply or 3dtiles")
    hashed = []
    for relative, path, size, _ in entries:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        hashed.append((relative, path, size, digest.hexdigest()))
    return hashed, payload_root


def validate_manifest(manifest: dict) -> None:
    required = ("name", "srs", "format", "label", "spatial_extent", "location_review")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise ValueError("package.json is missing required fields: " + ", ".join(missing))
    if manifest.get("schema") != "deqing.pointcloud-package.v1":
        raise ValueError("unsupported package schema; expected deqing.pointcloud-package.v1")
    if not isinstance(manifest["name"], str) or not manifest["name"].strip():
        raise ValueError("name must be a non-empty string")
    extent = manifest["spatial_extent"]
    if not isinstance(extent, dict) or extent.get("type") not in {"Polygon", "MultiPolygon"}:
        raise ValueError("spatial_extent must be a WGS84 GeoJSON Polygon or MultiPolygon")
    review = manifest["location_review"]
    if not isinstance(review, dict) or review.get("confirmed") is not True:
        raise ValueError("location_review.confirmed must be true after manual CRS/range review")
    for key in ("source_crs", "extent_method", "extent_coordinate_order", "evidence"):
        if not isinstance(review.get(key), str) or not review[key].strip():
            raise ValueError(f"location_review.{key} is required")
    if review["extent_coordinate_order"] != "longitude,latitude":
        raise ValueError("location_review.extent_coordinate_order must be longitude,latitude")
    if review["source_crs"].strip().upper() != str(manifest["srs"]).strip().upper():
        raise ValueError("location_review.source_crs must match srs")
    if len(review["evidence"].strip()) < 5 or len(review["extent_method"].strip()) < 3:
        raise ValueError("location review evidence and method must be descriptive")
    if manifest.get("purpose", "source") != "source":
        raise ValueError("package importer accepts only source versions; segmentation output requires a source version and taxonomy")


def upload_file(base_url: str, token: str, version_id: str, path: str,
                source: Path, size: int, digest: str) -> None:
    created = request_json(base_url, f"/pointcloud/versions/{version_id}/files", token, "POST", {
        "path": path, "size_bytes": size, "sha256": digest, "chunk_size": CHUNK_SIZE,
    })
    upload_id = created["upload_id"]
    total_chunks = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
    with source.open("rb") as stream:
        for index in range(total_chunks):
            start = index * CHUNK_SIZE
            block = stream.read(CHUNK_SIZE)
            end = start + len(block) - 1
            request_json(base_url, f"/pointcloud/uploads/{upload_id}/chunks/{index}", token, "PUT",
                         raw=block, headers={"Content-Range": f"bytes {start}-{end}/{size}"})
            print(f"  {path}: chunk {index + 1}/{total_chunks}", flush=True)
    request_json(base_url, f"/pointcloud/uploads/{upload_id}/complete", token, "POST", {})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="directory with package.json and files/")
    parser.add_argument("--base-url", default=os.getenv("DEQING_DATA_URL", "http://192.168.150.41:7110"))
    parser.add_argument("--token-file", type=Path, help="admin token file (plain token or {\"admin\":...})")
    parser.add_argument("--validate-only", action="store_true", help="validate and hash the package without modifying 7110")
    args = parser.parse_args()
    try:
        parsed = urlsplit(args.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("--base-url must be a plain http(s) origin")
        root = args.package.resolve()
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8"))
        validate_manifest(manifest)
        if not isinstance(manifest.get("metadata", {}), dict) or not isinstance(manifest.get("version_metadata", {}), dict):
            raise ValueError("metadata and version_metadata must be JSON objects")
        files, _ = package_files(root, manifest)
        total_bytes = sum(item[2] for item in files)
        print(json.dumps({"name": manifest["name"], "format": manifest["format"],
                          "files": len(files), "bytes": total_bytes,
                          "entrypoint": manifest.get("entrypoint") or ("tileset.json" if manifest["format"] == "3dtiles" else "cloud.ply")},
                         ensure_ascii=False, indent=2))
        if args.validate_only:
            return 0
        token = read_token(args.token_file)
        metadata = {**manifest.get("metadata", {}), "content_kind": manifest.get("content_kind", "pointcloud")}
        resource = request_json(args.base_url, "/pointcloud/resources", token, "POST", {
            "name": manifest["name"], "description": manifest.get("description", ""),
            "srs": manifest["srs"], "metadata": metadata,
            "spatial_extent": manifest["spatial_extent"], "location_review": manifest["location_review"],
        })
        resource_id = resource["id"]
        entrypoint = manifest.get("entrypoint") or ("tileset.json" if manifest["format"] == "3dtiles" else "cloud.ply")
        version = request_json(args.base_url, f"/pointcloud/resources/{resource_id}/versions", token, "POST", {
            "label": manifest["label"], "format": manifest["format"], "purpose": "source",
            "entrypoint": entrypoint, "metadata": manifest.get("version_metadata", {}),
            "location_review": manifest["location_review"],
        })
        version_id = version["id"]
        print(f"resource_id={resource_id}\nversion_id={version_id}", flush=True)
        for relative, path, size, digest in files:
            upload_file(args.base_url, token, version_id, relative, path, size, digest)
        published = request_json(args.base_url, f"/pointcloud/versions/{version_id}/finalize", token, "POST", {})
        print(json.dumps({"published": published, "resource_id": resource_id}, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, json.JSONDecodeError, HTTPError, URLError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
