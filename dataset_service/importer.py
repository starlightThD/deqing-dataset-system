import hashlib
import json
import os
import shutil
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .colmap import validate_model
from .config import Settings
from .db import audit, connect


SPARSE_FILES = tuple(f"{stem}.{ext}" for stem in ("cameras", "images", "points3D") for ext in ("bin", "txt"))
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _regular_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"symbolic link is not allowed: {path.name}")
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode) or path.stat().st_size == 0:
        raise ValueError(f"missing, empty or nonregular file: {path.name}")


def _directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"missing or linked directory: {path}")


def source_files(root: Path, expected_images: int) -> list[tuple[Path, str, str]]:
    _directory(root)
    image_dir, sparse_dir = root / "images", root / "sparse" / "0"
    _directory(root / "sparse")
    _directory(image_dir)
    _directory(sparse_dir)
    images = sorted(image_dir.iterdir(), key=lambda path: path.name)
    if len(images) != expected_images:
        raise ValueError(f"expected {expected_images} images, found {len(images)}")
    if len({p.name.casefold() for p in images}) != len(images):
        raise ValueError("duplicate image names ignoring case")
    result = []
    for image in images:
        if image.suffix.lower() not in IMAGE_SUFFIXES or image.name in {".", ".."}:
            raise ValueError(f"unsupported image: {image.name}")
        _regular_file(image)
        result.append((image, f"images/{image.name}", "image"))
    for name in SPARSE_FILES:
        path = sparse_dir / name
        _regular_file(path)
        result.append((path, f"sparse/0/{name}", "colmap"))
    return result


def copy_and_hash(source: Path, destination: Path) -> tuple[int, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    try:
        with os.fdopen(descriptor, "rb") as input_stream, destination.open("xb") as output_stream:
            if not stat.S_ISREG(os.fstat(input_stream.fileno()).st_mode):
                raise ValueError("source changed to a nonregular file")
            while chunk := input_stream.read(1024 * 1024):
                output_stream.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if size == 0:
        raise ValueError(f"empty file: {source.name}")
    return size, digest.hexdigest()


def hash_source(source: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("source changed to a nonregular file")
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    if size == 0:
        raise ValueError(f"empty file: {source.name}")
    return size, digest.hexdigest()


def content_digest(files: list[dict]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(f"{item['relative_path']}\0{item['size']}\0{item['sha256']}\n".encode())
    return digest.hexdigest()


def run_import(settings: Settings, import_id: str, db_path: Path) -> None:
    stage = settings.data_dir / "staging" / import_id
    try:
        with connect(db_path) as db:
            job = db.execute("SELECT source_alias FROM import_job WHERE id=?", (import_id,)).fetchone()
            if job is None:
                return
            alias = job["source_alias"]
            db.execute("UPDATE import_job SET status='copying' WHERE id=?", (import_id,))
        entries = source_files(settings.sources[alias], settings.expected_images)
        with connect(db_path) as db:
            has_versions = db.execute("SELECT 1 FROM dataset_version WHERE dataset_id='garden' LIMIT 1").fetchone() is not None
        if has_versions:
            source_manifest = []
            for original, relative, kind in entries:
                size, digest = hash_source(original)
                source_manifest.append({"relative_path": relative, "size": size, "sha256": digest, "kind": kind})
            with connect(db_path) as db:
                duplicate = db.execute(
                    "SELECT version FROM dataset_version WHERE dataset_id='garden' AND content_sha256=? AND status IN ('validated','published')",
                    (content_digest(source_manifest),),
                ).fetchone()
                if duplicate:
                    db.execute("UPDATE import_job SET status='duplicate',version=?,finished_at=? WHERE id=?", (duplicate["version"], now(), import_id))
                    audit(db, "admin", "import", import_id, "duplicate", duplicate["version"])
                    return
        stage.mkdir(parents=True, exist_ok=False)
        manifest_files = []
        for original, relative, kind in entries:
            size, digest = copy_and_hash(original, stage / relative)
            manifest_files.append({"relative_path": relative, "size": size, "sha256": digest, "kind": kind})
            with connect(db_path) as db:
                db.execute("UPDATE import_job SET copied_files=copied_files+1 WHERE id=?", (import_id,))
        with connect(db_path) as db:
            db.execute("UPDATE import_job SET status='validating' WHERE id=?", (import_id,))
        model = validate_model(stage / "sparse" / "0", {p.name for p in (stage / "images").iterdir()})
        content_sha = content_digest(manifest_files)
        total_bytes = sum(item["size"] for item in manifest_files)
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + content_sha[:12]
        manifest = {
            "dataset_id": "garden", "version": version, "source_alias": alias,
            "imported_at": now(), "content_sha256": content_sha,
            "file_count": len(manifest_files), "image_count": settings.expected_images,
            "total_bytes": total_bytes, "validation": {"result": "passed", **model},
            "files": manifest_files,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        with connect(db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT version FROM dataset_version WHERE dataset_id='garden' AND content_sha256=? AND status IN ('validated','published')",
                (content_sha,),
            ).fetchone()
            if duplicate:
                db.execute("UPDATE import_job SET status='duplicate',version=?,finished_at=? WHERE id=?", (duplicate["version"], now(), import_id))
                audit(db, "admin", "import", import_id, "duplicate", duplicate["version"])
                shutil.rmtree(stage)
                return
            db.execute(
                "INSERT INTO dataset_version(version,dataset_id,status,source_alias,content_sha256,file_count,image_count,total_bytes,created_at) VALUES(?,'garden','validated',?,?,?,?,?,?)",
                (version, alias, content_sha, len(manifest_files), settings.expected_images, total_bytes, now()),
            )
            db.executemany(
                "INSERT INTO dataset_file(version,relative_path,size,sha256,kind) VALUES(?,?,?,?,?)",
                [(version, f["relative_path"], f["size"], f["sha256"], f["kind"]) for f in manifest_files],
            )
            db.execute("UPDATE import_job SET status='validated',version=?,finished_at=? WHERE id=?", (version, now(), import_id))
            audit(db, "admin", "import", import_id, "validated", version)
    except Exception as exc:
        if stage.exists():
            shutil.rmtree(stage)
        with connect(db_path) as db:
            db.execute("UPDATE import_job SET status='failed',error=?,finished_at=? WHERE id=?", (str(exc)[:1000], now(), import_id))
            audit(db, "admin", "import", import_id, "failed", str(exc)[:1000])


def start_import(settings: Settings, db_path: Path, alias: str, idempotency_key: str | None) -> dict:
    import_id = uuid.uuid4().hex
    with connect(db_path) as db:
        db.execute("BEGIN IMMEDIATE")
        if idempotency_key:
            previous = db.execute("SELECT * FROM import_job WHERE dataset_id='garden' AND idempotency_key=?", (idempotency_key,)).fetchone()
            if previous:
                if previous["source_alias"] != alias:
                    raise ValueError("idempotency key already used for another source")
                return dict(previous)
        db.execute(
            "INSERT INTO import_job(id,dataset_id,source_alias,idempotency_key,status,created_at) VALUES(?,'garden',?,?,'queued',?)",
            (import_id, alias, idempotency_key, now()),
        )
        audit(db, "admin", "import", import_id, "queued", alias)
    threading.Thread(target=run_import, args=(settings, import_id, db_path), daemon=True).start()
    with connect(db_path) as db:
        return dict(db.execute("SELECT * FROM import_job WHERE id=?", (import_id,)).fetchone())
