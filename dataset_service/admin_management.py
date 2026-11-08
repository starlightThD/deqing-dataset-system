"""Admin-only catalog metadata changes and resource removal."""

import json
import sqlite3
import shutil
import uuid
from pathlib import Path
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from .oblique import SCHEMA as OBLIQUE_SCHEMA
from .pointcloud import SCHEMA as POINTCLOUD_SCHEMA
from .spatial import SCHEMA as SPATIAL_SCHEMA, SpatialStore
from .video import SCHEMA as VIDEO_SCHEMA


class ResourceRename(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str


def apply_legacy_name_overrides(data_dir: Path, legacy_store) -> None:
    """Restore local display names without changing the read-only mirror snapshot."""
    path = data_dir / "legacy_resource_names.sqlite3"
    if legacy_store is None or not path.is_file():
        return
    with sqlite3.connect(path) as db:
        names = dict(db.execute("SELECT resource_id,name FROM resource_name_override"))
    for rows in (legacy_store.resources, legacy_store.lite_snapshot or []):
        for row in rows:
            name = names.get(str(row.get("Id")))
            if name is not None:
                row["Label"] = name
                if "Name" in row:
                    row["Name"] = name


def router(store: SpatialStore, data_dir: Path, administrator: Callable,
           legacy_scenes=None, legacy_subsets=None, legacy_store=None) -> APIRouter:
    api = APIRouter(prefix="/admin", tags=["admin-management"])
    data_root = data_dir.resolve()

    def ready() -> None:
        try:
            store.ensure_schema()
            with store.connect() as db:
                db.execute(SPATIAL_SCHEMA)
                db.execute(POINTCLOUD_SCHEMA)
                db.execute(VIDEO_SCHEMA)
                db.execute(OBLIQUE_SCHEMA)
        except Exception as error:
            if isinstance(error, HTTPException):
                raise
            raise HTTPException(503, "management database is unavailable") from error

    def resource_uuid(raw: str) -> uuid.UUID:
        try:
            return uuid.UUID(raw)
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(422, "resource id must be a UUID") from None

    def quarantine(paths: list[Path], boundary: Path) -> tuple[Path | None, list[tuple[Path, Path]]]:
        """Atomically move owned paths aside before the database transaction."""
        boundary = boundary.resolve()
        candidates: list[Path] = []
        for path in paths:
            if not path.exists() and not path.is_symlink():
                continue
            try:
                lexical = path.absolute().relative_to(boundary)
            except ValueError:
                raise HTTPException(409, "stored path is outside its managed data directory") from None
            current = boundary
            for part in lexical.parts:
                current = current / part
                if current.is_symlink():
                    raise HTTPException(409, "refusing to delete a symlinked storage path")
            resolved = path.resolve()
            if resolved == boundary or not resolved.is_relative_to(boundary):
                raise HTTPException(409, "stored path is outside its managed data directory")
            candidates.append(resolved)
        # If one candidate contains another, moving the parent is sufficient.
        candidates = [p for p in candidates if not any(p != q and p.is_relative_to(q) for q in candidates)]
        if not candidates:
            return None, []
        trash_root = boundary / ".trash"
        if trash_root.is_symlink() or not trash_root.resolve().is_relative_to(boundary):
            raise HTTPException(409, "managed trash directory is unsafe")
        trash = trash_root / str(uuid.uuid4())
        trash.mkdir(parents=True, mode=0o700)
        moves: list[tuple[Path, Path]] = []
        try:
            for index, source in enumerate(candidates):
                target = trash / f"{index}-{source.name}"
                source.rename(target)
                moves.append((source, target))
        except OSError as error:
            restore(moves, trash)
            raise HTTPException(503, "could not stage stored files for deletion") from error
        return trash, moves

    def restore(moves: list[tuple[Path, Path]], trash: Path | None) -> None:
        for original, staged in reversed(moves):
            if staged.exists() or staged.is_symlink():
                original.parent.mkdir(parents=True, exist_ok=True)
                staged.rename(original)
        if trash is not None:
            shutil.rmtree(trash, ignore_errors=True)

    def finish(trash: Path | None) -> bool:
        if trash is None:
            return True
        try:
            shutil.rmtree(trash)
            parent = trash.parent
            if parent.exists() and not any(parent.iterdir()):
                parent.rmdir()
            return True
        except OSError:
            return False

    def unlink_scene_references(resource_id: str, *, dataset: bool = False,
                                video_group: bool = False) -> int:
        if legacy_scenes is None:
            return 0
        return legacy_scenes.remove_resource_everywhere(
            resource_id, dataset=dataset, video_group=video_group,
        )

    def update_legacy_label(resource_id: str, name: str, *, persist: bool = False) -> None:
        if legacy_store is None:
            return
        if persist:
            path = data_root / "legacy_resource_names.sqlite3"
            with sqlite3.connect(path, timeout=30) as db:
                db.execute("""CREATE TABLE IF NOT EXISTS resource_name_override (
                    resource_id TEXT PRIMARY KEY, name TEXT NOT NULL)""")
                db.execute("""INSERT INTO resource_name_override(resource_id,name) VALUES(?,?)
                    ON CONFLICT(resource_id) DO UPDATE SET name=excluded.name""", (resource_id, name))
            path.chmod(0o600)
        for rows in (legacy_store.resources, legacy_store.lite_snapshot or []):
            for row in rows:
                if str(row.get("Id")) == resource_id:
                    row["Label"] = name
                    if "Name" in row:
                        row["Name"] = name

    @api.patch("/resources/{kind}/{resource_id}")
    def rename_resource(kind: str, resource_id: str, body: ResourceRename,
                        _: Annotated[str, Depends(administrator)]):
        if kind not in {"vector", "raster", "imagery", "video", "pointcloud", "subset", "legacy"}:
            raise HTTPException(422, "unsupported resource kind")
        identifier = resource_uuid(resource_id)
        name = body.name.strip()
        if not 1 <= len(name) <= 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise HTTPException(422, "name must contain 1..200 printable characters")
        ready()
        resource_id = str(identifier)

        if kind == "subset":
            if legacy_subsets is None:
                raise HTTPException(404, "legacy subset service is not configured")
            subset = legacy_subsets.get(resource_id)
            if subset is None:
                raise HTTPException(404, "subset resource not found")
            previous = subset["resource"].get("Label") or ""
            if previous != name:
                legacy_subsets.rename(resource_id, name)
                update_legacy_label(resource_id, name)
            return {"id": resource_id, "kind": kind, "name": name, "previous_name": previous,
                    "renamed": previous != name}

        if kind == "legacy":
            if legacy_store is None:
                raise HTTPException(404, "legacy catalog is not configured")
            source = next((row for row in legacy_store.resources
                           if str(row.get("Id")) == resource_id and not row.get("IsDeleted")), None)
            if source is None:
                raise HTTPException(404, "legacy resource not found")
            if legacy_subsets is not None and legacy_subsets.get(resource_id) is not None:
                raise HTTPException(409, "use subset kind for local subset resources")
            with store.connect() as db:
                if db.execute("SELECT 1 FROM pointcloud_resource WHERE id=%s", (identifier,)).fetchone():
                    raise HTTPException(409, "use pointcloud kind for versioned resources")
            previous = source.get("Label") or source.get("Name") or ""
            if previous != name:
                update_legacy_label(resource_id, name, persist=True)
            return {"id": resource_id, "kind": kind, "name": name, "previous_name": previous,
                    "renamed": previous != name, "display_override": True}

        tables = {"vector": "spatial_dataset", "raster": "raster_collection",
                  "imagery": "oblique_resource", "video": "video_group",
                  "pointcloud": "pointcloud_resource"}
        table = tables[kind]
        with store.connect() as db:
            row = db.execute(f"SELECT name FROM {table} WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, "resource not found")
            previous = row["name"]
            if previous != name:
                suffix = ",updated_at=now()" if kind in {"raster", "pointcloud"} else ""
                db.execute(f"UPDATE {table} SET name=%s{suffix} WHERE id=%s", (name, identifier))
        if previous != name and legacy_store is not None and any(
            str(row.get("Id")) == resource_id for row in legacy_store.resources
        ):
            update_legacy_label(resource_id, name, persist=True)
        return {"id": resource_id, "kind": kind, "name": name, "previous_name": previous,
                "renamed": previous != name}

    @api.delete("/resources/{kind}/{resource_id}")
    def delete_resource(kind: str, resource_id: str,
                        _: Annotated[str, Depends(administrator)]):
        ready()
        if kind not in {"vector", "raster", "imagery", "video", "pointcloud", "subset"}:
            raise HTTPException(422, "unsupported or read-only resource kind")
        identifier = resource_uuid(resource_id)
        staged: Path | None = None
        moves: list[tuple[Path, Path]] = []
        details: dict = {}
        scene_ref_type: str | None = None
        subsets_to_remove: list[str] = []

        if kind == "subset":
            if legacy_subsets is None:
                raise HTTPException(404, "legacy subset service is not configured")
            subset = legacy_subsets.get(str(identifier))
            if subset is None:
                raise HTTPException(404, "subset resource not found")
            if not legacy_subsets.delete(str(identifier)):
                raise HTTPException(404, "subset resource not found")
            try:
                removed_refs = unlink_scene_references(str(identifier))
                scene_cleanup = True
            except Exception:
                removed_refs, scene_cleanup = 0, False
            if legacy_store is not None:
                legacy_store.resources[:] = [row for row in legacy_store.resources if str(row.get("Id")) != str(identifier)]
            return {"id": str(identifier), "kind": kind, "deleted": True,
                    "scene_references_removed": removed_refs, "scene_reference_cleanup_complete": scene_cleanup,
                    "source_files_deleted": False}

        boundaries = {
            "vector": (data_root / "spatial").resolve(),
            "raster": (data_root / "rasters").resolve(),
            "imagery": (data_root / "oblique-images").resolve(),
            "video": (data_root / "videos").resolve(),
            "pointcloud": (data_root / "pointcloud").resolve(),
        }

        try:
            with store.connect() as db:
                if kind == "vector":
                    row = db.execute("SELECT id FROM spatial_dataset WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
                    if not row:
                        raise HTTPException(404, "spatial dataset not found")
                    boundary = boundaries[kind]
                    staged, moves = quarantine([boundary / "sources" / str(identifier)], boundary)
                    # Supplemental exports are stored inside canonical POI rows.
                    # Remove their provenance when the source dataset is deleted.
                    db.execute("""UPDATE poi_feature p SET sources=(
                        SELECT COALESCE(jsonb_agg(item.value ORDER BY item.ordinality),'[]'::jsonb)
                        FROM jsonb_array_elements(p.sources) WITH ORDINALITY AS item(value,ordinality)
                        WHERE item.value->>'dataset_id'<>%s)
                        WHERE p.sources @> %s::jsonb""",
                        (str(identifier), json.dumps([{"dataset_id": str(identifier)}])))
                    db.execute("DELETE FROM spatial_dataset WHERE id=%s", (identifier,))
                    scene_ref_type = "dataset"

                elif kind == "raster":
                    row = db.execute("SELECT id FROM raster_collection WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
                    if not row:
                        raise HTTPException(404, "raster collection not found")
                    epochs = db.execute("SELECT storage_path FROM raster_epoch WHERE collection_id=%s", (identifier,)).fetchall()
                    boundary = boundaries[kind]
                    paths = []
                    for epoch in epochs:
                        path = (boundary / epoch["storage_path"]).resolve()
                        if path == boundary or not path.is_relative_to(boundary):
                            raise HTTPException(409, "raster storage path is outside the raster volume")
                        overlaps = db.execute("SELECT storage_path FROM raster_epoch WHERE collection_id<>%s", (identifier,)).fetchall()
                        for other in overlaps:
                            other_path = (boundary / other["storage_path"]).resolve()
                            if path == other_path or path.is_relative_to(other_path) or other_path.is_relative_to(path):
                                raise HTTPException(409, "raster epoch storage is shared with another collection")
                        paths.append(path)
                    staged, moves = quarantine(paths, boundary)
                    db.execute("DELETE FROM raster_collection WHERE id=%s", (identifier,))

                elif kind == "imagery":
                    row = db.execute("SELECT id FROM oblique_resource WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
                    if not row:
                        raise HTTPException(404, "oblique image resource not found")
                    boundary = boundaries[kind]
                    owned = db.execute("SELECT storage_backend,storage_key FROM oblique_image WHERE resource_id=%s", (identifier,)).fetchall()
                    # Only package-imported data-volume files are owned by this resource.
                    owned_prefix = f"{identifier}/"
                    has_owned_files = any(item["storage_backend"] == "data-volume"
                                          and str(item["storage_key"] or "").startswith(owned_prefix) for item in owned)
                    staged, moves = quarantine([boundary / str(identifier)] if has_owned_files else [], boundary)
                    db.execute("DELETE FROM oblique_image WHERE resource_id=%s", (identifier,))
                    db.execute("DELETE FROM oblique_group WHERE resource_id=%s", (identifier,))
                    db.execute("DELETE FROM oblique_resource WHERE id=%s", (identifier,))
                    details["shared_legacy_files_preserved"] = True

                elif kind == "video":
                    row = db.execute("SELECT id FROM video_group WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
                    if not row:
                        raise HTTPException(404, "video group not found")
                    boundary = boundaries[kind]
                    upload_ids = db.execute("SELECT id FROM video_upload WHERE group_id=%s", (identifier,)).fetchall()
                    paths = [boundary / "groups" / str(identifier), boundary / ".staging" / str(identifier)]
                    paths.extend(boundary / ".uploads" / str(item["id"]) for item in upload_ids)
                    staged, moves = quarantine(paths, boundary)
                    db.execute("DELETE FROM video_group WHERE id=%s", (identifier,))
                    scene_ref_type = "video"

                else:  # versioned PLY / 3D Tiles resource
                    row = db.execute("SELECT id FROM pointcloud_resource WHERE id=%s FOR UPDATE", (identifier,)).fetchone()
                    if not row:
                        raise HTTPException(404, "point-cloud resource not found")
                    boundary = boundaries[kind]
                    upload_ids = db.execute("""SELECT u.id FROM pointcloud_upload u JOIN pointcloud_version v
                        ON v.id=u.version_id WHERE v.resource_id=%s""", (identifier,)).fetchall()
                    external_references = db.execute("""SELECT
                        (SELECT count(*) FROM pointcloud_version WHERE resource_id<>%s
                            AND source_version_id IN (SELECT id FROM pointcloud_version WHERE resource_id=%s))
                        + (SELECT count(*) FROM pointcloud_version WHERE resource_id<>%s
                            AND taxonomy_id IN (SELECT id FROM pointcloud_taxonomy WHERE resource_id=%s))
                        + (SELECT count(*) FROM pointcloud_job WHERE resource_id<>%s
                            AND (source_version_id IN (SELECT id FROM pointcloud_version WHERE resource_id=%s)
                              OR output_version_id IN (SELECT id FROM pointcloud_version WHERE resource_id=%s)
                              OR taxonomy_id IN (SELECT id FROM pointcloud_taxonomy WHERE resource_id=%s))) AS n""",
                        (identifier, identifier, identifier, identifier,
                         identifier, identifier, identifier, identifier)).fetchone()["n"]
                    if external_references:
                        raise HTTPException(409, "resource is referenced by another point-cloud resource or job")
                    paths = [boundary / "files" / str(identifier)]
                    paths.extend(boundary / ".uploads" / str(item["id"]) for item in upload_ids)
                    staged, moves = quarantine(paths, boundary)
                    subsets_to_remove = legacy_subsets.ids_for_source(str(identifier)) if legacy_subsets is not None else []
                    db.execute("DELETE FROM pointcloud_job WHERE resource_id=%s", (identifier,))
                    db.execute("UPDATE pointcloud_version SET source_version_id=NULL,job_id=NULL WHERE resource_id=%s", (identifier,))
                    db.execute("DELETE FROM pointcloud_version WHERE resource_id=%s", (identifier,))
                    db.execute("DELETE FROM pointcloud_taxonomy WHERE resource_id=%s", (identifier,))
                    db.execute("DELETE FROM pointcloud_resource WHERE id=%s", (identifier,))
        except HTTPException:
            restore(moves, staged)
            raise
        except Exception as error:
            restore(moves, staged)
            raise HTTPException(503, "resource deletion failed; staged files were restored") from error

        if kind == "pointcloud" and subsets_to_remove and legacy_subsets is not None:
            try:
                legacy_subsets.delete_for_source(str(identifier))
                for subset_id in subsets_to_remove:
                    unlink_scene_references(subset_id)
                if legacy_store is not None:
                    deleted = set(subsets_to_remove)
                    legacy_store.resources[:] = [row for row in legacy_store.resources if str(row.get("Id")) not in deleted]
                details["derived_subsets_removed"] = len(subsets_to_remove)
                details["derived_subsets_cleanup_complete"] = True
            except Exception:
                details["derived_subsets_removed"] = 0
                details["derived_subsets_cleanup_complete"] = False
        try:
            details["scene_references_removed"] = unlink_scene_references(
                str(identifier), dataset=scene_ref_type == "dataset", video_group=scene_ref_type == "video",
            )
            details["scene_reference_cleanup_complete"] = True
        except Exception:
            details["scene_references_removed"] = 0
            details["scene_reference_cleanup_complete"] = False

        details.update({"id": str(identifier), "kind": kind, "deleted": True,
                        "storage_cleanup_complete": finish(staged)})
        return details

    return api
