"""Unified, resumable package upload protocol and type-specific import adapters."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field


CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILES = 10000
MAX_FILE_BYTES = 24 * 1024**3
MAX_PACKAGE_BYTES = 100 * 1024**3
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ZIP_RATIO = 200
ALLOWED_TYPES = {"oblique", "pointcloud", "raster", "vector", "video"}
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


class PackageFile(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(gt=0, le=MAX_PACKAGE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")


class PackageStart(BaseModel):
    transport: Literal["folder", "zip"]
    expected_type: str | None = None
    files: list[PackageFile] = Field(min_length=1, max_length=MAX_FILES)


def _lock(upload_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(upload_id, threading.RLock())


def _safe_relative(value: str) -> str:
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("package paths must use safe POSIX relative paths")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"unsafe package path: {value}")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError(f"drive-qualified package path is not allowed: {value}")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError(f"package path must be canonical POSIX form: {value}")
    if len(normalized.encode("utf-8")) > 1024:
        raise ValueError("package path is too long")
    return normalized


def _read_state(root: Path, upload_id: str) -> dict:
    try:
        state = json.loads((root / upload_id / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise HTTPException(404, "package upload session not found") from None
    if state.get("id") != upload_id:
        raise HTTPException(404, "package upload session not found")
    return state


def _write_state(root: Path, upload_id: str, state: dict) -> None:
    session = root / upload_id
    temporary = session / "state.json.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, session / "state.json")


def _validate_start(body: PackageStart) -> list[dict]:
    if body.expected_type is not None and body.expected_type not in ALLOWED_TYPES:
        raise HTTPException(422, "expected_type must be oblique, pointcloud, raster, vector or video")
    if body.transport == "zip":
        if len(body.files) != 1 or not body.files[0].path.lower().endswith(".zip"):
            raise HTTPException(422, "ZIP upload must contain exactly one .zip file")
    else:
        paths = [item.path for item in body.files]
        if "package.json" not in paths:
            raise HTTPException(422, "folder package must contain package.json at its root")
    seen: set[str] = set()
    total = 0
    files = []
    for item in body.files:
        try:
            path = _safe_relative(item.path)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        folded = path.casefold()
        if folded in seen:
            raise HTTPException(422, f"duplicate package path: {path}")
        seen.add(folded)
        if item.size_bytes > MAX_FILE_BYTES and not (body.transport == "zip" and path.lower().endswith(".zip")):
            raise HTTPException(413, f"package file exceeds 24 GiB: {path}")
        total += item.size_bytes
        files.append({"path": path, "size_bytes": item.size_bytes, "sha256": item.sha256.lower(), "chunks": {}})
    if total > MAX_PACKAGE_BYTES:
        raise HTTPException(413, "package exceeds 100 GiB")
    return files


def _validate_manifest(package_root: Path) -> dict:
    manifest_path = package_root / "package.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("package root must contain a package.json no larger than 1 MiB")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"package.json is not valid UTF-8 JSON: {error}") from error
    if not isinstance(manifest, dict) or manifest.get("schema") != "deqing.data-package.v1":
        raise ValueError("package.json schema must be deqing.data-package.v1")
    kind = manifest.get("type")
    if kind not in ALLOWED_TYPES:
        raise ValueError("package.json type must be oblique, pointcloud, raster, vector or video")
    name = manifest.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValueError("package.json name must contain 1 to 200 characters")
    description = manifest.get("description", "")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("package.json description must be a string no longer than 4000 characters")
    metadata = manifest.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("package.json metadata must be an object")
    review = manifest.get("location_review")
    if not isinstance(review, dict) or review.get("confirmed") is not True:
        raise ValueError("location_review.confirmed must be true after review")
    for key in ("source_crs", "axis_order", "method", "evidence"):
        if not isinstance(review.get(key), str) or len(review[key].strip()) < 3:
            raise ValueError(f"location_review.{key} is required")
    if len(review["evidence"].strip()) < 5:
        raise ValueError("location_review.evidence must cite a reviewable source")
    data_dir = package_root / "data"
    if not data_dir.is_dir():
        raise ValueError("package must contain a data/ directory")
    payload_files = [path for path in data_dir.rglob("*") if path.is_file()]
    if not payload_files:
        raise ValueError("data/ contains no payload files")
    if any(path.is_symlink() for path in data_dir.rglob("*")):
        raise ValueError("symbolic links are not allowed in package data")
    if kind == "vector":
        shapes = [path for path in data_dir.iterdir() if path.is_file() and path.suffix.lower() == ".shp"]
        if len(shapes) != 1:
            raise ValueError("vector package data/ must contain exactly one Shapefile group at its root")
        stem = shapes[0].stem.casefold()
        for ext in (".shx", ".dbf"):
            if not any(path.is_file() and path.stem.casefold() == stem and path.suffix.lower() == ext for path in data_dir.iterdir()):
                raise ValueError(f"vector package is missing {shapes[0].stem}{ext}")
        if not any(path.is_file() and path.stem.casefold() == stem and path.suffix.lower() == ".prj" for path in data_dir.iterdir()) and not metadata.get("source_srid"):
            raise ValueError("vector package requires a matching .prj or metadata.source_srid")
    elif kind == "pointcloud":
        if metadata.get("format") not in {"ply", "3dtiles"}:
            raise ValueError("pointcloud metadata.format must be ply or 3dtiles")
        if metadata.get("content_kind", "pointcloud") not in {"pointcloud", "mesh", "model", "terrain"}:
            raise ValueError("pointcloud metadata.content_kind is not supported")
        if not isinstance(metadata.get("srs"), str) or not metadata["srs"].strip():
            raise ValueError("pointcloud metadata.srs is required")
        if not isinstance(metadata.get("label"), str) or not metadata["label"].strip():
            raise ValueError("pointcloud metadata.label is required")
        if not isinstance(metadata.get("spatial_extent"), dict):
            raise ValueError("pointcloud metadata.spatial_extent must be a WGS84 GeoJSON geometry")
        if metadata["format"] == "ply":
            ply = [path for path in payload_files if path.suffix.lower() == ".ply"]
            if len(payload_files) != 1 or len(ply) != 1:
                raise ValueError("PLY package data/ must contain exactly one .ply file")
        elif not (data_dir / "tileset.json").is_file():
            raise ValueError("3D Tiles package data/ must contain tileset.json at its root")
        if review.get("source_crs", "").casefold() != metadata["srs"].strip().casefold():
            raise ValueError("location_review.source_crs must match pointcloud metadata.srs")
    elif kind == "raster":
        if not any(path.is_dir() and re.fullmatch(r"imagery_\d{8}", path.name) for path in data_dir.iterdir()):
            raise ValueError("raster package data/ must contain imagery_YYYYMMDD period directories")
    elif kind == "oblique":
        for required in ("camera_groups.json", "poses.jsonl", "images"):
            if not (data_dir / required).exists():
                raise ValueError(f"oblique package data/ is missing {required}")
    elif kind == "video":
        data_bytes = sum(path.stat().st_size for path in payload_files)
        if data_bytes > MAX_PACKAGE_BYTES - MAX_MANIFEST_BYTES:
            raise ValueError("video data leaves no room for the generated calibration.xml within the 100 GiB group limit")
        videos = list(data_dir.glob("*.mp4"))
        if len(videos) != 1:
            raise ValueError("video package data/ must contain exactly one MP4 at its root")
        for required in ("05_精标定结果.txt", "09_正射叠加图.tif", "01_粗搜索匹配图.png",
                         "02_精搜索匹配图.png", "03_范围示意图.png", "04_粗标定结果.txt",
                         "06_裁剪OBJ/region.obj", "06_裁剪OBJ/region.mtl", "07_投影图.png", "08_精匹配图.png"):
            if not (data_dir / required).is_file():
                raise ValueError(f"video package data/ is missing {required}")
    return manifest


def _assemble_files(root: Path, upload_id: str, state: dict) -> Path:
    session = root / upload_id
    package_root = session / "package"
    package_root.mkdir(mode=0o700)
    if state["transport"] == "folder":
        for index, item in enumerate(state["files"]):
            destination = package_root / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with destination.open("wb") as output:
                count = (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
                for chunk_index in range(count):
                    path = session / "chunks" / str(index) / f"{chunk_index:08d}.chunk"
                    with path.open("rb") as source:
                        while block := source.read(1024 * 1024):
                            output.write(block)
                            digest.update(block)
                            size += len(block)
            if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
                raise ValueError(f"size or SHA-256 mismatch for {item['path']}")
        return package_root

    item = state["files"][0]
    archive_path = session / "package.zip"
    digest = hashlib.sha256()
    size = 0
    with archive_path.open("wb") as output:
        count = (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
        for chunk_index in range(count):
            path = session / "chunks" / "0" / f"{chunk_index:08d}.chunk"
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                    size += len(block)
    if size != item["size_bytes"] or digest.hexdigest() != item["sha256"]:
        raise ValueError("ZIP size or SHA-256 mismatch")
    _extract_zip(archive_path, package_root)
    archive_path.unlink(missing_ok=True)
    return package_root


def _extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > MAX_FILES:
            raise ValueError("ZIP contains more than 10,000 entries")
        normalized: list[tuple[zipfile.ZipInfo, str]] = []
        total = 0
        seen: set[str] = set()
        for info in members:
            if info.is_dir():
                continue
            name = _safe_relative(info.filename)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"ZIP symbolic links are not allowed: {name}")
            folded = name.casefold()
            if folded in seen:
                raise ValueError(f"ZIP contains duplicate paths: {name}")
            seen.add(folded)
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"ZIP member exceeds 24 GiB: {name}")
            total += info.file_size
            if total > MAX_PACKAGE_BYTES:
                raise ValueError("ZIP expanded size exceeds 100 GiB")
            if info.file_size > 0 and info.compress_size > 0 and info.file_size / info.compress_size > MAX_ZIP_RATIO:
                raise ValueError(f"ZIP compression ratio exceeds 200:1: {name}")
            normalized.append((info, name))
        root_manifest = "package.json" in seen
        if not root_manifest:
            roots = {name.split("/", 1)[0] for _, name in normalized if "/" in name}
            if len(roots) == 1 and all(name.startswith(next(iter(roots)) + "/") for _, name in normalized):
                prefix = next(iter(roots)) + "/"
                normalized = [(info, name[len(prefix):]) for info, name in normalized]
            if not any(name == "package.json" for _, name in normalized):
                raise ValueError("ZIP must contain package.json at its root (an optional single enclosing folder is allowed)")
        for info, name in normalized:
            output = destination / name
            output.parent.mkdir(parents=True, exist_ok=True)
            with source.open(info, "r") as src, output.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def _run_script(script: Path, args: list[str], *, cwd: Path, timeout: int = 24 * 60 * 60) -> str:
    env = os.environ.copy()
    project_root = str(script.parent.parent)
    env["PYTHONPATH"] = project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    result = subprocess.run([sys.executable, str(script), *args], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=timeout, check=False)
    output = (result.stdout or "") + ("\n" + result.stderr if result.stderr else "")
    if result.returncode:
        raise RuntimeError(output[-8000:] or f"import script exited with {result.returncode}")
    return output


def _run_adapter(package_root: Path, manifest: dict, data_dir: Path,
                 authorization: str | None) -> dict:
    kind = manifest["type"]
    name = manifest["name"].strip()
    metadata = manifest["metadata"]
    review = manifest["location_review"]
    evidence = review["evidence"].strip()
    data = package_root / "data"
    scripts = Path(__file__).resolve().parent.parent / "scripts"
    token_path = package_root.parent / "admin-token.tmp"
    if authorization:
        token_path.write_text(authorization.removeprefix("Bearer ").strip(), encoding="utf-8")
        token_path.chmod(0o600)
    try:
        if kind == "vector":
            if not authorization:
                raise ValueError("administrator Bearer token is required for vector import")
            command = [str(data), "--base-url", "http://127.0.0.1:7110", "--token-file", str(token_path),
                       "--name", name, "--confirm-location-review", "--location-review-source", evidence]
            for option, key in (("--source-srid", "source_srid"), ("--encoding", "encoding"),
                                ("--vertical-crs", "vertical_crs"), ("--vertical-unit", "vertical_unit")):
                if metadata.get(key) is not None:
                    command.extend([option, str(metadata[key])])
            output = _run_script(scripts / "upload_spatial.py", command, cwd=package_root)
            match = re.search(r"OK .+?: id=([0-9a-f-]{36}) features=(\d+)", output)
            return {"kind": kind, "id": match.group(1) if match else None,
                    "feature_count": int(match.group(2)) if match else None}

        if kind == "pointcloud":
            if not authorization:
                raise ValueError("administrator Bearer token is required for pointcloud import")
            srs = str(metadata["srs"]).strip()
            point_manifest = {
                "schema": "deqing.pointcloud-package.v1", "name": name,
                "description": manifest.get("description", ""), "content_kind": metadata.get("content_kind", "pointcloud"),
                "srs": srs, "format": metadata["format"], "purpose": "source",
                "label": metadata["label"], "files_dir": "data",
                "entrypoint": metadata.get("entrypoint") or ("tileset.json" if metadata["format"] == "3dtiles" else "cloud.ply"),
                "spatial_extent": metadata["spatial_extent"],
                "location_review": {"confirmed": True, "source_crs": srs,
                    "extent_coordinate_order": "longitude,latitude",
                    "extent_method": review.get("method", "operator_reviewed_wgs84_extent"), "evidence": evidence},
                "metadata": metadata.get("resource_metadata", {}),
                "version_metadata": metadata.get("version_metadata", {}),
            }
            common_manifest = package_root / "package.json"
            common_text = common_manifest.read_text(encoding="utf-8")
            common_manifest.write_text(json.dumps(point_manifest, ensure_ascii=False), encoding="utf-8")
            try:
                output = _run_script(scripts / "upload_pointcloud_package.py",
                    [str(package_root), "--base-url", "http://127.0.0.1:7110", "--token-file", str(token_path)], cwd=package_root)
            finally:
                common_manifest.write_text(common_text, encoding="utf-8")
            resource_id = re.search(r"resource_id=([0-9a-f-]{36})", output)
            version_id = re.search(r"version_id=([0-9a-f-]{36})", output)
            return {"kind": kind, "id": resource_id.group(1) if resource_id else None,
                    "version_id": version_id.group(1) if version_id else None}

        if kind == "video":
            if not authorization:
                raise ValueError("administrator Bearer token is required for video import")
            _run_script(scripts / "import_video_group.py",
                [str(data), "--base-url", "http://127.0.0.1:7110", "--token-file", str(token_path),
                 "--group-name", name, "--location-review-source", evidence], cwd=package_root)
            state_file = data / ".7110-video-upload.json"
            state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
            group_id = state.get("group_id")
            if not group_id:
                raise RuntimeError("video importer completed but did not return a group id")
            return {"kind": kind, "id": group_id}

        if kind == "raster":
            collection_id = str(uuid.uuid4())
            raster_root = (data_dir / "rasters").resolve()
            raster_root.mkdir(parents=True, exist_ok=True)
            destination = raster_root / collection_id
            if destination.exists():
                raise RuntimeError("generated raster collection directory already exists")
            shutil.move(str(data), str(destination))
            try:
                output = _run_script(scripts / "register_temporal_raster.py",
                    ["--root", str(destination), "--collection-id", collection_id, "--name", name,
                     "--confirm-location-review", "--location-review-source", evidence], cwd=package_root)
            except Exception:
                if destination.exists() and not data.exists():
                    data.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(data))
                raise
            return {"kind": kind, "id": collection_id, "details": output[-2000:]}

        if kind == "oblique":
            resource_id = str(uuid.uuid4())
            output = _run_script(scripts / "import_oblique_resource_package.py",
                [str(data), "--name", name, "--data-dir", str(data_dir), "--resource-id", resource_id,
                 "--confirm-location-review", "--location-review-source", evidence], cwd=package_root)
            return {"kind": kind, "id": resource_id, "details": output[-2000:]}
    finally:
        token_path.unlink(missing_ok=True)
    raise ValueError(f"unsupported package type: {kind}")


def _finalize(root: Path, data_dir: Path, upload_id: str, authorization: str | None) -> None:
    with _lock(upload_id):
        session = root / upload_id
        try:
            state = _read_state(root, upload_id)
            state["status"] = "assembling"
            _write_state(root, upload_id, state)
            for file_index, item in enumerate(state["files"]):
                expected = (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
                received = item.get("chunks", {})
                if sorted(int(index) for index in received) != list(range(expected)):
                    raise ValueError(f"upload is missing chunks for {item['path']}")
            package_root = _assemble_files(root, upload_id, state)
            manifest = _validate_manifest(package_root)
            if state.get("expected_type") and state["expected_type"] != manifest["type"]:
                raise ValueError(f"selected type is {state['expected_type']}, but package.json declares {manifest['type']}")
            state["status"] = "importing"
            state["package_type"] = manifest["type"]
            _write_state(root, upload_id, state)
            result = _run_adapter(package_root, manifest, data_dir, authorization)
            state["status"] = "complete"
            state["result"] = result
            state["completed_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(root, upload_id, state)
            shutil.rmtree(session / "chunks", ignore_errors=True)
            shutil.rmtree(session / "package", ignore_errors=True)
        except Exception as error:
            try:
                state = _read_state(root, upload_id)
                state["status"] = "failed"
                state["error"] = str(error)[-8000:]
                _write_state(root, upload_id, state)
            except Exception:
                pass


def router(data_dir: Path, administrator: Callable) -> APIRouter:
    api = APIRouter(
        prefix="/admin/data-packages",
        tags=["数据包上传（统一入口）"],
    )
    root = (data_dir / "package-staging").resolve()
    root.mkdir(parents=True, exist_ok=True)

    @api.post(
        "/uploads", status_code=201,
        summary="创建统一数据包上传",
        description=(
            "五类资源（oblique、pointcloud、raster、vector、video）统一使用此入口。"
            "上传目录或 ZIP 清单后，按返回的分片信息上传文件并调用 finalize；"
            "服务端校验标准 package.json、文件清单、位置审核信息并分发到对应存储适配器。"
        ),
    )
    def create_upload(body: PackageStart, _: str = Depends(administrator)):
        files = _validate_start(body)
        upload_id = str(uuid.uuid4())
        session = root / upload_id
        session.mkdir(mode=0o700, parents=True)
        state = {"id": upload_id, "transport": body.transport, "files": files,
                 "expected_type": body.expected_type,
                 "status": "uploading", "created_at": datetime.now(timezone.utc).isoformat()}
        _write_state(root, upload_id, state)
        return {"upload_id": upload_id, "chunk_size": CHUNK_BYTES,
                "files": [{"index": index, "path": item["path"], "chunk_count": (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES}
                          for index, item in enumerate(files)]}

    @api.put(
        "/uploads/{upload_id}/files/{file_index}/chunks/{chunk_index}",
        summary="上传数据包文件分片",
    )
    async def upload_chunk(upload_id: str, file_index: int, chunk_index: int, request: Request,
                           _: str = Depends(administrator)):
        try:
            upload_id = str(uuid.UUID(upload_id))
        except ValueError:
            raise HTTPException(404, "package upload session not found") from None
        with _lock(upload_id):
            state = _read_state(root, upload_id)
            if state["status"] != "uploading":
                raise HTTPException(409, "package upload is no longer writable")
            if not 0 <= file_index < len(state["files"]):
                raise HTTPException(404, "package file index not found")
            item = state["files"][file_index]
            chunk_count = (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
            if not 0 <= chunk_index < chunk_count:
                raise HTTPException(416, "chunk index is outside the package file")
            start = chunk_index * CHUNK_BYTES
            end = min(item["size_bytes"] - 1, start + CHUNK_BYTES - 1)
            expected_length = end - start + 1
            declared_length = request.headers.get("content-length")
            if declared_length is not None and declared_length.isdigit() and int(declared_length) != expected_length:
                raise HTTPException(413, "Content-Length does not match the registered package chunk")
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers.get("content-range", ""))
            if not match or tuple(map(int, match.groups())) != (start, end, item["size_bytes"]):
                raise HTTPException(416, "Content-Range does not match the registered package chunk")
            body_buffer = bytearray()
            async for block in request.stream():
                if len(body_buffer) + len(block) > min(CHUNK_BYTES, expected_length):
                    raise HTTPException(413, "chunk body exceeds the registered byte range")
                body_buffer.extend(block)
            body = bytes(body_buffer)
            if len(body) != end - start + 1 or len(body) > CHUNK_BYTES:
                raise HTTPException(413, "chunk byte length does not match the registered range")
            digest = hashlib.sha256(body).hexdigest()
            existing = item["chunks"].get(str(chunk_index))
            if existing:
                if existing["sha256"] != digest:
                    raise HTTPException(409, "chunk already exists with different content")
                return {"upload_id": upload_id, "file_index": file_index, "chunk_index": chunk_index,
                        "received": True, "idempotent": True}
            chunk_dir = root / upload_id / "chunks" / str(file_index)
            chunk_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            chunk = chunk_dir / f"{chunk_index:08d}.chunk"
            temporary = chunk.with_suffix(".tmp")
            temporary.write_bytes(body)
            os.replace(temporary, chunk)
            item["chunks"][str(chunk_index)] = {"size_bytes": len(body), "sha256": digest}
            _write_state(root, upload_id, state)
        return {"upload_id": upload_id, "file_index": file_index, "chunk_index": chunk_index,
                "received": True, "idempotent": False}

    @api.get("/uploads/{upload_id}", summary="查询统一数据包上传状态")
    def upload_status(upload_id: str, _: str = Depends(administrator)):
        try:
            upload_id = str(uuid.UUID(upload_id))
        except ValueError:
            raise HTTPException(404, "package upload session not found") from None
        state = _read_state(root, upload_id)
        return {"upload_id": upload_id, "transport": state["transport"], "expected_type": state.get("expected_type"), "status": state["status"],
                "package_type": state.get("package_type"), "result": state.get("result"), "error": state.get("error"),
                "files": [{"index": index, "path": item["path"], "size_bytes": item["size_bytes"],
                           "chunk_count": (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES,
                           "received_chunks": sorted(int(chunk) for chunk in item.get("chunks", {}))}
                          for index, item in enumerate(state["files"])]}

    @api.post(
        "/uploads/{upload_id}/finalize", status_code=202,
        summary="校验并导入统一数据包",
        description="所有分片上传完成后调用；服务端校验包内容并通过类型适配器导入。",
    )
    def finalize_upload(upload_id: str, request: Request, background: BackgroundTasks,
                        _: str = Depends(administrator)):
        try:
            upload_id = str(uuid.UUID(upload_id))
        except ValueError:
            raise HTTPException(404, "package upload session not found") from None
        with _lock(upload_id):
            state = _read_state(root, upload_id)
            if state["status"] == "complete":
                return {"upload_id": upload_id, "status": "complete", "result": state.get("result")}
            if state["status"] in {"assembling", "importing"}:
                return {"upload_id": upload_id, "status": state["status"]}
            if state["status"] != "uploading":
                raise HTTPException(409, f"package upload status is {state['status']}")
            for item in state["files"]:
                expected = (item["size_bytes"] + CHUNK_BYTES - 1) // CHUNK_BYTES
                if sorted(int(index) for index in item.get("chunks", {})) != list(range(expected)):
                    raise HTTPException(409, f"package upload is missing chunks: {item['path']}")
            authorization = request.headers.get("authorization")
            state["status"] = "assembling"
            _write_state(root, upload_id, state)
            background.add_task(_finalize, root, data_dir, upload_id, authorization)
            return {"upload_id": upload_id, "status": "assembling"}

    @api.delete("/uploads/{upload_id}", status_code=204, summary="取消统一数据包上传")
    def cancel_upload(upload_id: str, _: str = Depends(administrator)):
        try:
            upload_id = str(uuid.UUID(upload_id))
        except ValueError:
            raise HTTPException(404, "package upload session not found") from None
        session_lock = _lock(upload_id)
        if not session_lock.acquire(blocking=False):
            raise HTTPException(409, "package import is currently running")
        try:
            state = _read_state(root, upload_id)
            if state["status"] in {"assembling", "importing"}:
                raise HTTPException(409, "package import is currently running")
            shutil.rmtree(root / upload_id, ignore_errors=True)
        finally:
            session_lock.release()
        return None

    return api
