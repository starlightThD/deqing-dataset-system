import json
import mimetypes
import os
import re
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .credentials import ensure_credentials, token_hash
from .admin_management import apply_legacy_name_overrides, router as admin_management_router
from .data_packages import router as data_packages_router
from .db import audit, connect, initialize
from .legacy import LegacyStore, authenticated_user, router as legacy_router
from .legacy_auth import ensure_legacy_auth, ensure_legacy_users
from .legacy_cache import LegacyCache
from .legacy_pointcloud_bridge import LegacyPointCloudBridge
from .legacy_scenes import LegacySceneStore
from .legacy_subsets import LegacySubsetStore
from .legacy_oblique_subsets import LegacyObliqueSubsetStore
from .oblique import router as oblique_router
from .pointcloud import router as pointcloud_router
from .poi_catalog import router as poi_catalog_router
from .raster import router as raster_router
from .temporal_raster import router as temporal_raster_router
from .spatial import SpatialStore, SpatialUnavailable, router as spatial_router
from .resource_query import router as resource_query_router
from .video import router as video_router


class ImportRequest(BaseModel):
    source_alias: str


class MetadataPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


def create_app(settings: Settings | None = None, legacy_root: Path | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if legacy_root is None and os.getenv("DEQING_LEGACY_OUT"):
        legacy_root = Path(os.environ["DEQING_LEGACY_OUT"])
    db_path = settings.data_dir / "catalog.sqlite3"
    publish_lock = threading.Lock()
    credentials: dict[str, str] = {}
    legacy_auth: dict = {}
    spatial_store = SpatialStore(settings.data_dir)
    legacy_store = LegacyStore(legacy_root) if legacy_root is not None else None
    legacy_pointcloud_bridge = LegacyPointCloudBridge(spatial_store) if legacy_store is not None else None
    legacy_cache = (
        LegacyCache(Path(os.getenv("DEQING_LEGACY_CACHE_DIR", str(settings.data_dir / "legacy-mirror"))))
        if legacy_store is not None else None
    )
    legacy_scenes = LegacySceneStore(settings.data_dir) if legacy_store is not None else None
    legacy_subsets = (
        LegacySubsetStore(settings.data_dir, legacy_store, legacy_cache, legacy_pointcloud_bridge)
        if legacy_store is not None and legacy_cache is not None else None
    )
    if legacy_subsets is not None:
        legacy_store.resources.extend(legacy_subsets.rows())
    legacy_oblique_subsets = (
        LegacyObliqueSubsetStore(settings.data_dir, legacy_store, spatial_store)
        if legacy_store is not None and legacy_store.arlayer_db is not None else None
    )
    if legacy_oblique_subsets is not None:
        legacy_store.resources.extend(legacy_oblique_subsets.rows())
    apply_legacy_name_overrides(settings.data_dir, legacy_store)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        (settings.data_dir / "staging").mkdir(exist_ok=True)
        (settings.data_dir / "datasets" / "garden" / "versions").mkdir(parents=True, exist_ok=True)
        credentials.update({role: token_hash(value) for role, value in ensure_credentials(settings.data_dir).items()})
        if legacy_store is not None:
            legacy_auth.update(ensure_legacy_auth(settings.data_dir))
            legacy_auth["user_db_path"] = ensure_legacy_users(settings.data_dir, legacy_auth)
        initialize(db_path)
        yield

    app = FastAPI(
        title="Deqing Dataset Service",
        version="0.1.0",
        lifespan=lifespan,
        openapi_tags=[{
            "name": "数据包上传（统一入口）",
            "description": (
                "五类资源 oblique、pointcloud、raster、vector、video 的唯一公开入库流程。"
                "上传标准数据包目录或 ZIP；旧分类型上传路由仍为内部兼容适配器保留，"
                "不再作为 Swagger 中的独立入库入口展示。"
            ),
        }],
    )

    management_assets = Path(__file__).with_name("static") / "management"
    app.mount("/admin/console/assets", StaticFiles(directory=management_assets), name="management-assets")

    @app.get("/admin/console", include_in_schema=False)
    @app.get("/admin/console/", include_in_schema=False)
    def management_console():
        return FileResponse(management_assets / "index.html")

    @app.exception_handler(SpatialUnavailable)
    async def spatial_unavailable(_: Request, error: SpatialUnavailable):
        return JSONResponse(status_code=503, content={"detail": str(error)})
    origins = json.loads(os.getenv("DEQING_CORS_ORIGINS", '["*"]'))
    if not isinstance(origins, list) or not all(isinstance(origin, str) for origin in origins):
        raise ValueError("DEQING_CORS_ORIGINS must be a JSON string array")
    app.add_middleware(
        CORSMiddleware, allow_origins=origins, allow_credentials=False,
        allow_methods=["*"], allow_headers=["*"],
        expose_headers=["Content-Length", "Content-Range", "X-Legacy-Cache"],
    )
    if legacy_store is not None:
        app.include_router(legacy_router(
            legacy_store, legacy_auth, legacy_cache, spatial_store, legacy_scenes, legacy_subsets,
            legacy_oblique_subsets, legacy_pointcloud_bridge,
        ))

    def identity(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "Bearer credential required", headers={"WWW-Authenticate": "Bearer"})
        token = authorization[7:].strip()
        presented = token_hash(token)
        for role, digest in credentials.items():
            if secrets.compare_digest(presented, digest):
                return role
        if legacy_auth:
            user = authenticated_user(legacy_auth, token)
            if user is not None:
                return user["role"]
        raise HTTPException(401, "invalid credential", headers={"WWW-Authenticate": "Bearer"})

    def account_identity(authorization: Annotated[str | None, Header()] = None) -> dict:
        if not authorization or not authorization.startswith("Bearer ") or not legacy_auth:
            raise HTTPException(401, "account JWT required", headers={"WWW-Authenticate": "Bearer"})
        user = authenticated_user(legacy_auth, authorization[7:].strip())
        if user is None:
            raise HTTPException(401, "invalid account session", headers={"WWW-Authenticate": "Bearer"})
        return user

    def administrator(role: Annotated[str, Depends(identity)]) -> str:
        if role != "admin":
            raise HTTPException(403, "administrator role required")
        return role

    def spatial_reader(authorization: Annotated[str | None, Header()] = None) -> str:
        return identity(authorization)

    app.include_router(spatial_router(spatial_store, spatial_reader, administrator, account_identity))
    app.include_router(poi_catalog_router(spatial_store, spatial_reader))
    app.include_router(resource_query_router(spatial_store, account_identity, legacy_scenes))
    app.include_router(temporal_raster_router(spatial_store, settings.data_dir, spatial_reader))
    app.include_router(pointcloud_router(spatial_store, spatial_reader, administrator))
    app.include_router(video_router(spatial_store, spatial_reader, administrator, settings.data_dir / "videos"))
    app.include_router(oblique_router(
        spatial_store, spatial_reader,
        Path(os.getenv("DEQING_LEGACY_CACHE_DIR", str(settings.data_dir / "legacy-mirror"))),
        settings.data_dir,
    ))
    app.include_router(admin_management_router(
        spatial_store, settings.data_dir, administrator,
        legacy_scenes=legacy_scenes, legacy_subsets=legacy_subsets, legacy_store=legacy_store,
    ))
    app.include_router(data_packages_router(settings.data_dir, administrator))
    if legacy_store is not None and legacy_cache is not None:
        app.include_router(raster_router(legacy_store, legacy_cache, spatial_reader))

    if legacy_cache is not None:
        @app.get("/admin/legacy-cache")
        def list_legacy_cache(_: Annotated[str, Depends(administrator)], limit: int = 100):
            if limit < 1 or limit > 1000:
                raise HTTPException(400, "limit must be between 1 and 1000")
            return legacy_cache.list_entries(limit)

    def dataset_row():
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM dataset WHERE id='garden'").fetchone()
            return dict(row) if row else None

    def published_version(version: str):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM dataset_version WHERE dataset_id='garden' AND version=? AND status='published'", (version,)).fetchone()
            if not row:
                raise HTTPException(404, "published version not found")
            return dict(row)

    @app.get("/healthz")
    def healthz():
        with connect(db_path) as db:
            db.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    @app.post("/admin/datasets/import", status_code=202)
    def import_dataset(body: ImportRequest, _: Annotated[str, Depends(administrator)]):
        # The legacy garden package contains images and a COLMAP reconstruction in
        # local coordinates, but has no reviewed WGS84 camera positions or extent.
        # Keep it out of the map catalog until it has a map-ready registration flow.
        raise HTTPException(
            422,
            "garden import is disabled until camera positions and coverage are georeferenced, reviewed, and exposed as a map-ready resource",
        )

    @app.get("/admin/imports/{import_id}")
    def get_import(import_id: str, _: Annotated[str, Depends(administrator)]):
        with connect(db_path) as db:
            row = db.execute("SELECT * FROM import_job WHERE id=?", (import_id,)).fetchone()
            if not row:
                raise HTTPException(404, "import not found")
            return dict(row)

    @app.post("/admin/imports/{import_id}/publish")
    def publish(import_id: str, _: Annotated[str, Depends(administrator)]):
        with publish_lock:
            with connect(db_path) as db:
                job = db.execute("SELECT * FROM import_job WHERE id=?", (import_id,)).fetchone()
                if not job:
                    raise HTTPException(404, "import not found")
                if job["status"] == "published":
                    return {"dataset_id": "garden", "version": job["version"], "status": "published"}
                raise HTTPException(
                    422,
                    "garden import cannot be published until its camera positions and coverage have a reviewed WGS84 registration",
                )

    @app.patch("/admin/datasets/garden")
    def update_metadata(body: MetadataPatch, _: Annotated[str, Depends(administrator)]):
        changes = body.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(400, "no changes supplied")
        with connect(db_path) as db:
            for key, value in changes.items():
                if value is None:
                    raise HTTPException(400, f"{key} cannot be null")
                db.execute(f"UPDATE dataset SET {key}=? WHERE id='garden'", (value,))
            audit(db, "admin", "update_metadata", "garden", "success", json.dumps(changes, ensure_ascii=False))
        return dataset_row()

    @app.post("/admin/datasets/garden/versions/{version}/activate")
    def activate(version: str, _: Annotated[str, Depends(administrator)]):
        published_version(version)
        with connect(db_path) as db:
            db.execute("UPDATE dataset SET current_version=? WHERE id='garden'", (version,))
            audit(db, "admin", "activate", version, "success")
        return dataset_row()

    @app.get("/admin/audit")
    def get_audit(_: Annotated[str, Depends(administrator)], limit: int = 100):
        if limit < 1 or limit > 1000:
            raise HTTPException(400, "limit must be between 1 and 1000")
        with connect(db_path) as db:
            return [dict(row) for row in db.execute("SELECT * FROM audit_event ORDER BY id DESC LIMIT ?", (limit,))]

    @app.get("/datasets")
    def list_datasets(_: Annotated[str, Depends(identity)]):
        row = dataset_row()
        return [row] if row and row["current_version"] else []

    @app.get("/datasets/garden")
    def get_dataset(_: Annotated[str, Depends(identity)]):
        row = dataset_row()
        if not row or not row["current_version"]:
            raise HTTPException(404, "dataset not published")
        return row

    @app.get("/datasets/garden/versions")
    def get_versions(_: Annotated[str, Depends(identity)]):
        with connect(db_path) as db:
            return [dict(row) for row in db.execute("SELECT * FROM dataset_version WHERE dataset_id='garden' AND status='published' ORDER BY published_at DESC")]

    @app.get("/datasets/garden/versions/{version}/manifest")
    def get_manifest(version: str, _: Annotated[str, Depends(identity)]):
        published_version(version)
        path = settings.data_dir / "datasets" / "garden" / "versions" / version / "manifest.json"
        if not path.is_file():
            raise HTTPException(503, "published manifest unavailable")
        return json.loads(path.read_text())

    @app.get("/datasets/garden/versions/{version}/files/{relative_path:path}")
    def get_file(version: str, relative_path: str, request: Request, _: Annotated[str, Depends(identity)]):
        published_version(version)
        if not relative_path or relative_path.startswith("/") or ".." in Path(relative_path).parts or "\\" in relative_path:
            raise HTTPException(400, "invalid relative path")
        with connect(db_path) as db:
            entry = db.execute("SELECT size FROM dataset_file WHERE version=? AND relative_path=?", (version, relative_path)).fetchone()
        if not entry:
            raise HTTPException(404, "file not in manifest")
        path = settings.data_dir / "datasets" / "garden" / "versions" / version / relative_path
        if not path.is_file() or path.is_symlink():
            raise HTTPException(503, "published file unavailable")
        size = entry["size"]
        start, end, status = 0, size - 1, 200
        range_header = request.headers.get("range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                raise HTTPException(416, "invalid range", headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
            else:
                start = max(size - int(last), 0)
            if start >= size or end < start:
                raise HTTPException(416, "range not satisfiable", headers={"Content-Range": f"bytes */{size}"})
            status = 206

        def stream():
            remaining = end - start + 1
            with path.open("rb") as file:
                file.seek(start)
                while remaining:
                    chunk = file.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {"Accept-Ranges": "bytes", "Content-Length": str(end - start + 1), "Content-Disposition": f'attachment; filename="{Path(relative_path).name}"'}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(stream(), status_code=status, media_type=mimetypes.guess_type(relative_path)[0] or "application/octet-stream", headers=headers)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    uvicorn.run("dataset_service.main:app", host="0.0.0.0", port=7110, workers=1)
