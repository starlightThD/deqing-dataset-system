#!/usr/bin/env python3
"""Upload a standard 7110 data package directory or ZIP using the unified API."""

import argparse
import hashlib
import json
import os
import sys
import time
import zipfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


CHUNK_BYTES = 8 * 1024 * 1024
MAX_FILES = 10_000
MAX_FILE_BYTES = 24 * 1024**3
MAX_PACKAGE_BYTES = 100 * 1024**3


def token_from_file(path: Path | None) -> str:
    raw = path.read_text(encoding="utf-8").strip() if path else os.getenv("DEQING_ADMIN_TOKEN", "").strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    token = parsed.get("admin") if isinstance(parsed, dict) else parsed
    if not isinstance(token, str) or not token.strip():
        raise ValueError("provide --token-file or DEQING_ADMIN_TOKEN")
    return token.strip()


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def package_files(source: Path) -> tuple[str, list[dict]]:
    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise ValueError("package file must be a .zip archive")
        size = source.stat().st_size
        if not 0 < size <= MAX_PACKAGE_BYTES:
            raise ValueError("ZIP size must be greater than 0 and no more than 100 GiB")
        return "zip", [{"path": source.name, "source": source,
                         "size_bytes": size, "sha256": hash_file(source)}]
    if not source.is_dir() or not (source / "package.json").is_file():
        raise ValueError("package directory must contain package.json at its root")
    paths = []
    total = 0
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symbolic links are not allowed: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        if any(part in {"", ".", ".."} for part in relative.split("/")):
            raise ValueError(f"unsafe package path: {relative}")
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"empty package file: {relative}")
        if size > MAX_FILE_BYTES:
            raise ValueError(f"package file exceeds 24 GiB: {relative}")
        total += size
        if total > MAX_PACKAGE_BYTES:
            raise ValueError("package exceeds 100 GiB")
        paths.append((relative, path, size))
    if not paths:
        raise ValueError("package contains no files")
    if len(paths) > MAX_FILES:
        raise ValueError("package contains more than 10,000 files")
    output = [{"path": relative, "source": path, "size_bytes": size, "sha256": hash_file(path)}
              for relative, path, size in paths]
    return "folder", output


def package_type(source: Path, transport: str) -> str:
    if transport == "folder":
        manifest = json.loads((source / "package.json").read_text(encoding="utf-8"))
    else:
        with zipfile.ZipFile(source) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            target = "package.json" if "package.json" in names else None
            if target is None:
                roots = {name.split("/", 1)[0] for name in names if "/" in name}
                if len(roots) == 1:
                    candidate = next(iter(roots)) + "/package.json"
                    if candidate in names:
                        target = candidate
            if target is None:
                raise ValueError("ZIP must contain package.json at its root")
            manifest = json.loads(archive.read(target).decode("utf-8"))
    kind = manifest.get("type") if isinstance(manifest, dict) else None
    if kind not in {"oblique", "pointcloud", "raster", "vector", "video"}:
        raise ValueError("package.json type must be oblique, pointcloud, raster, vector or video")
    if manifest.get("schema") != "deqing.data-package.v1":
        raise ValueError("package.json schema must be deqing.data-package.v1")
    return kind


def request(base_url: str, token: str, path: str, method: str = "GET", body: bytes | None = None,
            content_type: str | None = None, headers: dict | None = None, timeout: int = 300):
    request_headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if content_type:
        request_headers["Content-Type"] = content_type
    if headers:
        request_headers.update(headers)
    req = Request(base_url.rstrip("/") + path, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except HTTPError as error:
        detail = error.read(2000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code} {method} {path}: {detail}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="standard package directory or .zip archive")
    parser.add_argument("--base-url", default=os.getenv("DEQING_DATA_URL", "http://192.168.150.41:7110"))
    parser.add_argument("--token-file", type=Path, help="admin token file (plain token or {\"admin\": ...})")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()
    state_path = args.package.parent / f".{args.package.name}.7110-upload.json"
    try:
        parsed = urlsplit(args.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("--base-url must be an http(s) origin")
        token = token_from_file(args.token_file)
        transport, files = package_files(args.package.resolve())
        expected_type = package_type(args.package.resolve(), transport)
        descriptors = [{key: item[key] for key in ("path", "size_bytes", "sha256")} for item in files]
        if state_path.is_file():
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if saved.get("base_url") != args.base_url or saved.get("transport") != transport or saved.get("expected_type") != expected_type or saved.get("files") != descriptors:
                raise ValueError(f"upload state does not match this package; inspect or remove {state_path}")
            upload_id = saved["upload_id"]
            status = request(args.base_url, token, f"/admin/data-packages/uploads/{upload_id}")
            if status["status"] == "failed":
                raise RuntimeError(status.get("error") or "previous package import failed; remove the failed session before retrying")
        else:
            created = request(args.base_url, token, "/admin/data-packages/uploads", "POST",
                              json.dumps({"transport": transport, "expected_type": expected_type, "files": descriptors}).encode("utf-8"),
                              "application/json")
            upload_id = created["upload_id"]
            state_path.write_text(json.dumps({"base_url": args.base_url, "transport": transport,
                                              "expected_type": expected_type, "files": descriptors,
                                              "upload_id": upload_id}, indent=2) + "\n",
                                  encoding="utf-8")
            status = request(args.base_url, token, f"/admin/data-packages/uploads/{upload_id}")
        if status["status"] == "complete":
            print(json.dumps(status["result"], ensure_ascii=False, indent=2))
            state_path.unlink(missing_ok=True)
            return 0
        uploaded = 0
        total = sum(item["size_bytes"] for item in files)
        for file_row in status["files"]:
            index = file_row["index"]
            source = files[index]["source"]
            received = set(file_row["received_chunks"])
            with source.open("rb") as stream:
                for chunk_index in range(file_row["chunk_count"]):
                    start = chunk_index * CHUNK_BYTES
                    block = stream.read(CHUNK_BYTES)
                    if chunk_index in received:
                        uploaded += len(block)
                        continue
                    end = start + len(block) - 1
                    request(args.base_url, token,
                        f"/admin/data-packages/uploads/{upload_id}/files/{index}/chunks/{chunk_index}",
                        "PUT", block, "application/octet-stream",
                        {"Content-Range": f"bytes {start}-{end}/{files[index]['size_bytes']}"}, timeout=7200)
                    uploaded += len(block)
                    print(f"{files[index]['path']}: {bytes(uploaded)} / {bytes(total)}", flush=True)
        request(args.base_url, token, f"/admin/data-packages/uploads/{upload_id}/finalize", "POST", b"{}", "application/json")
        while True:
            status = request(args.base_url, token, f"/admin/data-packages/uploads/{upload_id}")
            if status["status"] == "complete":
                print(json.dumps(status["result"], ensure_ascii=False, indent=2))
                state_path.unlink(missing_ok=True)
                return 0
            if status["status"] == "failed":
                raise RuntimeError(status.get("error") or "package import failed")
            print(f"导入状态：{status['status']}", flush=True)
            time.sleep(max(1.0, args.poll_seconds))
    except (OSError, ValueError, KeyError, HTTPError, URLError, RuntimeError, json.JSONDecodeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


def bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    amount = float(value)
    for unit in units[:-1]:
        if amount < 1024:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


if __name__ == "__main__":
    raise SystemExit(main())
