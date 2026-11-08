"""Compatibility API for the former .NET data service."""
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import math
import mimetypes
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from .legacy_auth import authenticate_user, create_session, get_session_user, get_user_by_id, register_user, revoke_session
from .legacy_cache import LegacyCache
from .legacy_scenes import LegacySceneStore
from .legacy_subsets import (SPATIAL_DATASET_QUERY_MAX_AREA_KM2, LegacySubsetStore,
                             SubsetError, canonical_geometry)
from .legacy_oblique_subsets import LegacyObliqueSubsetStore
from .poi_identity import UNIFIED_POI_ID
from .resource_query import ResourceQuery, run_query, _version as resolve_source_version


ISSUER = "http://localhost:8031/"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def make_jwt(secret: str, user_id: str = "00000000-0000-0000-0000-0000000000aa") -> str:
    issued = int(time.time())
    auth_info = json.dumps({
        "UserId": user_id,
        "LoginRecordId": _b64(os.urandom(16)),
        "TenantId": "00000000-0000-0000-0000-000000000001",
        "ExpireDays": 3,
        "Type": 1,
        "CurrentToken": None,
        "Param": None,
        "IsSuperAdmin": False,
    }, separators=(",", ":"))
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64(json.dumps({
        "nbf": issued, "exp": issued + 259200, "AuthInfo": auth_info,
        "iss": ISSUER, "aud": ISSUER,
    }, separators=(",", ":")).encode())
    signature = _b64(hmac.new(bytes.fromhex(secret), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def valid_jwt(token: str, secret: str) -> bool:
    if len(token) > 8192:
        return False
    try:
        header, payload, signature = token.split(".")
        expected = _b64(hmac.new(bytes.fromhex(secret), f"{header}.{payload}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        def decode(part: str):
            return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        head, claims = decode(header), decode(payload)
        current = time.time()
        return (
            head.get("alg") == "HS256" and head.get("typ") == "JWT"
            and claims.get("nbf", current + 1) <= current < claims.get("exp", 0)
            and claims.get("iss") == ISSUER and claims.get("aud") == ISSUER
            and isinstance(claims.get("AuthInfo"), str)
        )
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error, OverflowError):
        return False


def jwt_user_id(token: str, secret: str) -> str | None:
    if not valid_jwt(token, secret):
        return None
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        auth_info = json.loads(claims["AuthInfo"])
        user_id = auth_info.get("UserId")
        return user_id if isinstance(user_id, str) and user_id else None
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        return None


def authenticated_user(auth: dict, token: str) -> dict | None:
    user_id = jwt_user_id(token, auth["jwt_secret"])
    if user_id is None:
        return None
    return get_session_user(auth["user_db_path"], user_id, token)


class LegacyStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        db_path = self.root / "catalog.db"
        json_path = self.root / "resources" / "resources_all.json"
        if not db_path.is_file() or not json_path.is_file():
            raise RuntimeError(f"legacy catalog or resource snapshot missing in {self.root}")
        snapshot = json.loads(json_path.read_text(encoding="utf-8"))
        self.resources = [self._clean_resource(row) for row in snapshot["Rows"]]
        self.resources.sort(key=lambda row: row.get("num") or 0)
        lite_path = self.root / "resources" / "lite_all.json"
        self.lite_snapshot = json.loads(lite_path.read_text(encoding="utf-8"))["Rows"] if lite_path.is_file() else None
        self.big_types = self._optional_json("big_types.json")
        self.resource_types = self._optional_json("resource_types.json")
        self.dataset_dropdown = self._optional_json("datasets_dropdown.json")
        arlayer_path = self.root / "arlayer_index.sqlite3"
        self.arlayer_db = arlayer_path if arlayer_path.is_file() else None
        self.keys: dict[str, Path] = {}
        self.datasets: list[dict] = []
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            self.datasets = [
                {"Key": dataset_id, "Value": name}
                for dataset_id, name in db.execute("SELECT id,name FROM dataset ORDER BY name,id")
            ]
            for key, local_path in db.execute("SELECT fastdfs_id,local_path FROM resource_asset WHERE fastdfs_id IS NOT NULL AND local_path IS NOT NULL"):
                self._add(key, local_path)
            for resource_id, key in db.execute("SELECT id,original_root_file_id FROM resource WHERE original_root_file_id IS NOT NULL"):
                extension = "index.db3" if key.lower().endswith(".db3") else "root.json"
                self._add(key, f"indexes/{resource_id}/{extension}")
            for key, local_path in db.execute("SELECT fastdfs_id,local_path FROM photo WHERE fastdfs_id IS NOT NULL AND local_path IS NOT NULL"):
                self._add(key, local_path)
        finally:
            db.close()

    @staticmethod
    def _clean_resource(row: dict) -> dict:
        result = dict(row)
        result.pop("_meta", None)
        return result

    def _optional_json(self, filename: str):
        path = self.root / "resources" / filename
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def _add(self, key: str, local_path: str) -> None:
        if not key or not local_path:
            return
        candidate = (self.root / local_path).resolve()
        if candidate.is_relative_to(self.root) and candidate.is_file():
            self.keys[key] = candidate

    def path_of(self, key: str) -> Path | None:
        path = self.keys.get(key)
        return path if path is not None and path.is_file() else None

    def lite_resources(self) -> list[dict]:
        if self.lite_snapshot is not None:
            return self.lite_snapshot
        rows = []
        for row in self.resources:
            rows.append({
                "TypeName": row.get("TypeName"), "Id": row.get("Id"),
                "Label": row.get("Label"), "Description": row.get("Description"),
                "Type": row.get("Type"), "UploadTime": row.get("UploadTime"),
                "MetaData": None, "BusinessTag": row.get("BusinessTag"),
                "EpsgId": row.get("EpsgId"),
                "TypeKey": "RasterTMS" if row.get("Type") == 12000008 else None,
                "Url": None, "Attribute": None, "IsLook": True, "IsSetting": True,
                "Proj4text": None,
            })
        return rows


def _json(data, status: int = 200) -> Response:
    return Response(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode(), status_code=status, media_type="application/json; charset=utf-8")


async def _body(request: Request) -> tuple[dict | None, int]:
    raw = await request.body()
    if raw and "json" not in request.headers.get("content-type", "").lower():
        return None, 415
    try:
        data = json.loads(raw.decode("utf-8-sig")) if raw else {}
    except (UnicodeError, json.JSONDecodeError):
        return None, 400
    return (data, 200) if isinstance(data, dict) else (None, 400)


def _plot_geometry(payload: dict) -> tuple[str, str] | None:
    """Return a static SQL geometry expression name and its parameter value."""
    value = payload.get("geometry", payload.get("RangeJson", payload.get("rangeJson")))
    if isinstance(value, dict):
        return "geojson", json.dumps(value, ensure_ascii=False, allow_nan=False)
    if isinstance(value, list):
        coordinates = value
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ("wkt", value) if value.strip() else None
        if isinstance(parsed, dict) and "type" in parsed and "coordinates" in parsed:
            return "geojson", json.dumps(parsed, ensure_ascii=False, allow_nan=False)
        if isinstance(parsed, list):
            coordinates = parsed
        else:
            return None
    else:
        return None

    normalized = []
    for item in coordinates:
        if isinstance(item, dict):
            point = [item.get("x", item.get("X")), item.get("y", item.get("Y"))]
            z = item.get("z", item.get("Z"))
            if z is not None:
                point.append(z)
        elif isinstance(item, (list, tuple)):
            point = list(item)
        else:
            return None
        if len(point) < 2 or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in point):
            return None
        normalized.append(point)
    if not normalized:
        return None

    kind = str(payload.get("Type", payload.get("type", ""))).strip().lower()
    instruction = payload.get("InstructionJson")
    if isinstance(instruction, str):
        try:
            instruction = json.loads(instruction)
        except json.JSONDecodeError:
            instruction = None
    if isinstance(instruction, dict):
        kind = str(instruction.get("type", kind)).lower()
    try:
        numeric_kind = int(payload.get("Type", -1))
    except (TypeError, ValueError):
        numeric_kind = -1
    if numeric_kind == 0 or kind in {"draw_text", "draw_point", "point"}:
        geometry = {"type": "Point", "coordinates": normalized[0]}
    elif numeric_kind == 1 or kind in {"draw_line", "linestring", "line"}:
        geometry = {"type": "LineString", "coordinates": normalized}
    else:
        ring = normalized
        if ring[0] != ring[-1]:
            ring = [*ring, ring[0]]
        geometry = {"type": "Polygon", "coordinates": [ring]}
    return "geojson", json.dumps(geometry, ensure_ascii=False, allow_nan=False)


def _old_plot_record(row: dict, files: list[dict] | None = None) -> dict:
    properties = row["properties"] or {}
    if isinstance(properties, str):
        try:
            properties = json.loads(properties)
        except json.JSONDecodeError:
            properties = {}
    coordinates = (row.get("geometry") or {}).get("coordinates", [])
    if (row.get("geometry") or {}).get("type") == "Polygon" and coordinates:
        coordinates = coordinates[0]
    points = []

    def collect(value):
        if isinstance(value, (list, tuple)) and value and isinstance(value[0], (int, float)):
            points.append({"x": value[0], "y": value[1], "z": value[2] if len(value) > 2 else 0})
        elif isinstance(value, (list, tuple)):
            for child in value:
                collect(child)

    collect(coordinates)
    annotation_id = str(row["id"])
    name = row["name"]
    attributes = dict(properties)
    attributes.update({
        "Id": annotation_id,
        "Label": name,
        "RangeJson": json.dumps(points, ensure_ascii=False, separators=(",", ":")),
        "IsChecked": bool(properties.get("IsChecked", False)),
        "ArLabelFiles": files or [],
    })
    if row.get("range_wkt"):
        instruction = properties.get("InstructionJson")
        if not isinstance(instruction, str) or not instruction.strip():
            instruction_data = {str(key).lower(): value for key, value in properties.items()}
            instruction_data.update({"label": name, "RangeJson": row["range_wkt"]})
            instruction = json.dumps(instruction_data, ensure_ascii=False, separators=(",", ":"))
        attributes["InstructionJson"] = instruction
    kind = str(attributes.get("Type", "0"))
    icon = {"0": "biaohuidian", "1": "biaohuixian", "2": "biaohuimian", "3": "biaohuiti"}.get(kind, "biaohui")
    return {
        "id": annotation_id,
        "label": name,
        "type": "Plotting",
        "attribute": attributes,
        "isAdd": True,
        "isEdit": True,
        "isDelete": True,
        "isSetResource": False,
        "iconUrl": icon,
        "children": [],
    }


def router(store: LegacyStore, auth: dict, cache: LegacyCache | None = None, spatial_store=None,
           scenes: LegacySceneStore | None = None, subsets: LegacySubsetStore | None = None,
           oblique_subsets: LegacyObliqueSubsetStore | None = None,
           pointcloud_bridge=None) -> APIRouter:
    def sync_pointcloud_resource_catalog() -> None:
        if pointcloud_bridge is not None:
            pointcloud_bridge.sync_resources(store.resources)

    api = APIRouter(dependencies=[Depends(sync_pointcloud_resource_catalog)] if pointcloud_bridge else [])

    def authorized(request: Request) -> bool:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return False
        return authenticated_user(auth, header[7:].strip()) is not None

    def unauthorized() -> Response:
        return _json({"State": 401, "Message": "请重新登录"}, 401)

    def account_user(request: Request) -> dict | None:
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer "):
            return None
        return authenticated_user(auth, header[7:].strip())

    def admin_required(request: Request) -> tuple[dict | None, Response | None]:
        user = account_user(request)
        if user is None:
            return None, unauthorized()
        if user.get("role") != "admin":
            return user, _json({"State": 403, "Message": "仅管理员可以修改场景及其资源"}, 403)
        return user, None

    resource_node_types = {
        1000008: "DemCTB", 1000012: "Terrain3dtiles", 1000015: "Terrain3dtiles",
        1000016: "DemCTB", 2000004: "VectorWfsLayer", 2000027: "VectorMvtLayer",
        2000033: "Vector3DTiles", 4000001: "ImageAirImage",
        4000002: "ImageStreetPano", 4000003: "ImageIndoorPano",
        4000004: "ImageAirPano", 4000005: "ImageLand",
        5000012: "Model3dtiles", 10000001: "MediaDirectionalVideo",
        10000004: "MediaDirectionalVideoDynamic", 10000005: "MediaUndirectionalVideo",
        10000006: "MediaHttpFlv", 10000007: "MediaHls",
        12000004: "RasterWMS", 12000005: "RasterWMTS", 12000008: "RasterTMS",
        13000006: "PointCloud3Dtiles", 14000003: "BIM3Dtiles",
        21000001: "PointCloudGaussian3DTiles",
    }

    def get_scene_tree_templates() -> tuple[dict, dict]:
        # A new scene starts with its own root; resource groups are added only
        # for relationships stored in 7110. No cached remote scene is a template.
        return {}, {"tree": [{
            "id": "00000000-0000-0000-0000-000000000000",
            "label": "场景", "type": "Group", "attribute": None,
            "isAdd": False, "isEdit": False, "isDelete": False,
            "iconUrl": "changjing", "isSetResource": False, "sort": 1,
            "children": [],
        }]}

    def local_scene_tree(scene_id: str) -> list[dict]:
        if scenes is None:
            return []
        scenes.migrate_legacy_tree(scene_id, store.resources)
        leaves, template = get_scene_tree_templates()
        scene = scenes.get(scene_id) or {}
        custom_tree = int(scene.get("SceneTreeType") or 1) == 2
        if custom_tree:
            tree = scenes.folders(scene_id)
        else:
            tree = json.loads(json.dumps(template["tree"], ensure_ascii=False))
            def rekey_groups(nodes) -> None:
                for group in nodes:
                    if not isinstance(group, dict):
                        continue
                    group["id"] = str(uuid.uuid5(
                        uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:group:{group.get('id')}"
                    ))
                    rekey_groups(group.get("children") or [])
            rekey_groups(tree)
        associations = scenes.resource_rows(scene_id)
        resource_index = {str(row.get("Id")): row for row in store.resources}
        def existing_ids(nodes: list[dict]) -> set[str]:
            ids: set[str] = set()
            pending = list(nodes)
            while pending:
                candidate = pending.pop()
                if isinstance(candidate, dict):
                    ids.add(str(candidate.get("id")))
                    pending.extend(candidate.get("children") or [])
            return ids
        present_ids = existing_ids(tree)
        for association in associations:
            if str(association["resource_id"]) in present_ids:
                continue
            resource = resource_index.get(str(association["resource_id"]))
            if resource is None:
                continue
            resource_type = int(association["resource_type"])
            source = leaves.get(resource_type, {})
            node_type = source.get("type") or resource_node_types.get(resource_type) or str(resource.get("TypeName") or "Resource")
            attr = source.get("attribute") if isinstance(source.get("attribute"), dict) else {}
            attribute = dict(attr)
            label = association.get("label") or resource.get("Label") or resource.get("Name")
            attribute.update({
                "Id": str(association["resource_id"]), "Label": label,
                "Type": resource_type,
                "MetaData": association.get("metadata") if association.get("metadata") is not None else resource.get("MetaData"),
                "BusinessTag": resource.get("BusinessTag") or "", "IsChecked": False,
                "EpsgId": resource.get("EpsgId"),
                "Url": resource.get("WfsDataUrl") or resource.get("DataUrl"),
                "TypeName": resource.get("TypeName"),
                "Proj4text": resource.get("SrsDefinition"),
                "ExtendedAttributes": association.get("extended_attributes"),
                "Sort": association.get("sort", 0),
            })
            node = json.loads(json.dumps(source, ensure_ascii=False)) if source else {
                "isAdd": False, "isEdit": True, "isDelete": True, "iconUrl": None,
                "isSetResource": False, "children": [],
            }
            node.update({"id": str(association["resource_id"]), "label": label,
                         "type": node_type, "attribute": attribute, "children": []})
            parent = None
            if custom_tree and association.get("custom_tree_id"):
                pending = [node for node in tree if isinstance(node, dict)]
                while pending:
                    candidate = pending.pop()
                    if candidate.get("id") == association["custom_tree_id"]:
                        parent = candidate
                        break
                    pending.extend(candidate.get("children") or [])
            elif not custom_tree:
                for group in (n for n in tree if isinstance(n, dict)):
                    pending = [group]
                    while pending:
                        candidate = pending.pop()
                        group_types = candidate.get("attribute")
                        if isinstance(group_types, str):
                            try:
                                group_types = json.loads(group_types)
                            except json.JSONDecodeError:
                                group_types = []
                        if isinstance(group_types, list) and resource_type in group_types:
                            parent = candidate
                            break
                        pending.extend(candidate.get("children") or [])
                    if parent is not None:
                        break
            if custom_tree and parent is None:
                # Preserve associations created before a custom folder was
                # selected. Never hide a saved resource when the tree is empty.
                unassigned_id = str(uuid.uuid5(
                    uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:unassigned-resources"
                ))
                parent = next((group for group in tree if isinstance(group, dict)
                               and group.get("id") == unassigned_id), None)
                if parent is None:
                    parent = {
                        "id": unassigned_id, "label": "未分类资源", "type": "Group",
                        "attribute": None, "isAdd": False, "isEdit": False,
                        "isDelete": False, "isSetResource": False,
                        "children": [],
                    }
                    tree.append(parent)
            if parent is None and not custom_tree:
                if not tree:
                    tree.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:root")),
                                 "label": "场景", "type": "Group", "attribute": None,
                                 "isAdd": False, "isEdit": False, "isDelete": False,
                                 "iconUrl": "changjing", "isSetResource": False, "sort": 1,
                                 "children": []})
                big_type = resource.get("BigResourceType")
                group_names = {
                    1: "地形图层", 2: "矢量图层", 4: "影像图层", 5: "模型图层",
                    10: "视频图层", 12: "栅格图层", 13: "点云图层", 14: "BIM图层",
                    21: "高斯点云图层",
                }
                group_label = group_names.get(big_type)
                root = tree[0]
                root_children = root.setdefault("children", [])
                parent = next((child for child in root_children
                               if isinstance(child, dict) and child.get("type") == "Group"
                               and child.get("label") == group_label), None) if group_label else None
                if parent is None and group_label:
                    applicable_types = sorted({int(row["Type"]) for row in store.resources
                                               if row.get("BigResourceType") == big_type
                                               and row.get("Type") is not None})
                    parent = {
                        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:type:{big_type}")),
                        "label": group_label, "type": "Group",
                        "attribute": json.dumps(applicable_types, separators=(",", ":")),
                        "isAdd": False, "isEdit": False, "isDelete": False,
                        "iconUrl": None, "isSetResource": False,
                        "sort": len(root_children) + 1, "children": [],
                    }
                    root_children.append(parent)
                if parent is None:
                    parent = root
            if parent is None and custom_tree:
                continue
            parent.setdefault("children", []).append(node)
            present_ids.add(str(association["resource_id"]))
        return tree

    def append_persisted_layers(tree: list[dict], scene_id: str) -> list[dict]:
        """Overlay scene-owned folders and layer instances without changing legacy IDs."""
        if scenes is None:
            return tree
        folders = scenes.layer_folders(scene_id)
        layers = scenes.layers(scene_id)
        if not folders and not layers:
            return tree
        nodes: dict[str, dict] = {}
        for row in folders:
            nodes[row["id"]] = {
                "id": row["id"], "label": row["label"], "type": "Group",
                "layerCategory": row["layer_category"], "persistedFolderId": row["id"],
                "attribute": None, "isAdd": True, "isEdit": True, "isDelete": True,
                "isSetResource": True, "sort": row["sort"], "children": [],
            }
        # Video/POI groups can be inserted before scene-owned folders. Never
        # treat one of those layer groups as the scene root: doing so nests
        # unrelated folders under the video/POI checkbox.
        root_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-scene:{scene_id}:root"))
        root = next((item for item in tree if isinstance(item, dict)
                     and (item.get("id") == root_id or
                          (item.get("type") == "Group" and item.get("label") == "场景"))), None)
        if root is None:
            root = {"id": root_id,
                    "label": "场景", "type": "Group", "attribute": None, "children": []}
            tree.insert(0, root)
        for row in folders:
            parent = nodes.get(row["parent_id"]) if row["parent_id"] else root
            (parent or root).setdefault("children", []).append(nodes[row["id"]])

        resource_index = {str(row.get("Id")): row for row in store.resources}
        dataset_index: dict[str, dict] = {}
        video_index: dict[str, dict] = {}
        pointcloud_index: dict[str, dict] = {}
        oblique_index: dict[str, dict] = {}
        raster_index: dict[str, dict] = {}
        if spatial_store is not None:
            try:
                with spatial_store.connect() as connection:
                    ids = [row["source_id"] for row in layers if row["source_kind"] == "spatial_dataset"]
                    if ids:
                        dataset_index = {str(row["id"]): dict(row) for row in connection.execute(
                            "SELECT id,name,shape_type,feature_count,bounds FROM spatial_dataset WHERE id=ANY(%s::uuid[])",
                            (ids,)).fetchall()}
                    ids = [row["source_id"] for row in layers if row["source_kind"] == "video_group"]
                    if ids:
                        video_index = {str(row["id"]): dict(row) for row in connection.execute(
                            "SELECT id,name,ST_AsGeoJSON(spatial_extent) AS coverage FROM video_group WHERE id=ANY(%s::uuid[]) AND status='published'",
                            (ids,)).fetchall()}
                    ids = [row["source_id"] for row in layers if row["source_kind"] == "pointcloud_version"]
                    if ids:
                        pointcloud_index = {str(row["id"]): dict(row) for row in connection.execute(
                            "SELECT id,label,format,entrypoint FROM pointcloud_version WHERE id=ANY(%s::uuid[]) AND status='published'",
                            (ids,)).fetchall()}
                    ids = [row["source_id"] for row in layers if row["source_kind"] == "oblique_resource"]
                    if ids:
                        oblique_index = {str(row["id"]): dict(row) for row in connection.execute(
                            "SELECT r.id,r.name,r.source_srs,r.location_review,count(i.id) AS image_count,"
                            "count(i.id) FILTER (WHERE i.file_status='present') AS available_count "
                            "FROM oblique_resource r LEFT JOIN oblique_image i ON i.resource_id=r.id "
                            "WHERE r.id=ANY(%s::uuid[]) GROUP BY r.id", (ids,)).fetchall()}
                    ids = [row["source_id"] for row in layers if row["source_kind"] == "raster_collection"]
                    if ids:
                        raster_index = {str(row["id"]): dict(row) for row in connection.execute(
                            "SELECT c.id,c.name,ST_AsGeoJSON(c.bounds)::json AS bounds,c.crs_epsg,"
                            "c.tile_scheme,c.tile_format,c.min_zoom,c.max_zoom,"
                            "min(e.acquisition_date)::text AS acquisition_date,"
                            "count(e.acquisition_date) AS published_epoch_count "
                            "FROM raster_collection c LEFT JOIN raster_epoch e ON e.collection_id=c.id "
                            "AND e.status='published' WHERE c.id=ANY(%s::uuid[]) GROUP BY c.id", (ids,)).fetchall()}
            except Exception:
                pass
        leaves, _ = get_scene_tree_templates()
        for row in layers:
            source_id = row["source_id"]
            effective_id = row["subset_resource_id"] or source_id
            category = row["layer_category"]
            node = {"id": row["layer_id"], "layerId": row["layer_id"],
                    "sourceKind": row["source_kind"], "sourceId": source_id,
                    "sourceVersionId": row.get("source_version_id"),
                    "selectionMode": row.get("selection_mode"),
                    "renderDescriptor": row.get("render_descriptor"),
                    "subsetResourceId": row["subset_resource_id"],
                    "scopeGeometry": row["scope_geometry"], "scopeCrs": row["scope_crs"],
                    "filterProperties": row["filter_properties"],
                    "layerCategory": category, "parentFolderId": row["parent_folder_id"],
                    "label": row["display_name"], "sort": row["sort"], "children": [],
                    "isAdd": False, "isEdit": True, "isDelete": True, "isSetResource": False,
                    "attribute": {"IsChecked": row["default_visible"]}}
            diagnostic = None
            if row["source_kind"] == "legacy_resource":
                resource = resource_index.get(effective_id)
                if resource is None:
                    node["type"] = "UnavailableResource"
                    diagnostic = "资源目录或区域子资源不存在"
                else:
                    resource_type = int(resource.get("Type") or 0)
                    template = leaves.get(resource_type, {})
                    node["type"] = template.get("type") or resource_node_types.get(resource_type, resource.get("TypeName") or "Resource")
                    attribute = dict(template.get("attribute")) if isinstance(template.get("attribute"), dict) else {}
                    attribute.update({"Id": effective_id, "Label": row["display_name"], "Type": resource_type,
                                      "MetaData": resource.get("MetaData"), "BusinessTag": resource.get("BusinessTag") or "",
                                      "IsChecked": row["default_visible"], "EpsgId": resource.get("EpsgId"),
                                      "Url": resource.get("WfsDataUrl") or resource.get("DataUrl"),
                                      "TypeName": resource.get("TypeName"), "Proj4text": resource.get("SrsDefinition"),
                                      "Sort": row["sort"]})
                    legacy_payload = row.get("legacy_payload") or {}
                    if legacy_payload.get("metadata") is not None:
                        attribute["MetaData"] = legacy_payload["metadata"]
                    if legacy_payload.get("extended_attributes") is not None:
                        attribute["ExtendedAttributes"] = legacy_payload["extended_attributes"]
                    if legacy_payload.get("relate_resource_id") is not None:
                        attribute["RelateResourceId"] = legacy_payload["relate_resource_id"]
                    if legacy_payload.get("osgb_id") is not None:
                        attribute["OsgbId"] = legacy_payload["osgb_id"]
                    node["attribute"] = attribute
            elif row["source_kind"] == "spatial_dataset":
                dataset = dataset_index.get(source_id)
                node["type"] = "SpatialPoi" if dataset and int(dataset["shape_type"]) == 1 else "SpatialVector"
                if dataset is None:
                    diagnostic = "空间数据集不存在或暂不可查询"
                elif int(dataset["shape_type"]) not in {1, 5, 15}:
                    diagnostic = "当前场景渲染器尚不支持此几何类型"
                else:
                    dataset["id"] = source_id
                    node["dataset"] = dataset
                    if row["scope_geometry"]:
                        coordinates = row["scope_geometry"]["coordinates"]
                        polygons = coordinates if row["scope_geometry"]["type"] == "MultiPolygon" else [coordinates]
                        points = [point for polygon in polygons for point in polygon[0]]
                        node["scopeBbox"] = [min(point[0] for point in points), min(point[1] for point in points),
                                             max(point[0] for point in points), max(point[1] for point in points)]
                    if node["type"] == "SpatialPoi":
                        node["agentScopedPoi"] = True
            elif row["source_kind"] == "video_group":
                node["type"] = "FixedVideo"
                node["videoGroupId"] = source_id
                video = video_index.get(source_id)
                if video is None:
                    diagnostic = "视频资料组不存在或尚未发布"
                else:
                    if row["display_name"] == source_id:
                        node["label"] = video["name"]
                    node["coverage"] = json.loads(video["coverage"]) if video.get("coverage") else None
            elif row["source_kind"] == "oblique_resource":
                node["type"] = "OdmImage"
                resource = oblique_index.get(source_id)
                if resource is None:
                    diagnostic = "ODM 影像资源包不存在或暂不可查询"
                else:
                    node["imageCount"] = resource["image_count"]
                    node["availableCount"] = resource["available_count"]
                    node["sourceSrs"] = resource["source_srs"]
                    node["locationReview"] = resource["location_review"]
                    if not resource["available_count"]:
                        diagnostic = "影像文件尚未存入本地"
            elif row["source_kind"] == "raster_collection":
                node["type"] = "TemporalRaster"
                resource = raster_index.get(source_id)
                if resource is None or resource["published_epoch_count"] != 1:
                    diagnostic = "时序影像集合不存在或已发布期次数量不是 1"
                else:
                    node["collectionId"] = source_id
                    node["acquisitionDate"] = resource["acquisition_date"]
                    node["bounds"] = resource["bounds"]
                    node["rasterFormat"] = {"crs_epsg": resource["crs_epsg"],
                                            "tile_scheme": resource["tile_scheme"],
                                            "tile_format": resource["tile_format"],
                                            "min_zoom": resource["min_zoom"], "max_zoom": resource["max_zoom"]}
            elif row["source_kind"] == "pointcloud_resource":
                node["type"] = "Scoped3DTiles" if row["scope_geometry"] else "PointCloudVersion"
                node["pointcloudResourceId"] = source_id
                node["pointcloudVersionId"] = row.get("source_version_id")
                if not row.get("source_version_id"):
                    diagnostic = "点云资源未固定已发布版本"
            elif row["source_kind"] == "pointcloud_version":
                node["type"] = "PointCloudVersion"
                node["pointcloudVersionId"] = source_id
                version = pointcloud_index.get(source_id)
                if version is None:
                    diagnostic = "点云版本不存在或尚未发布"
                else:
                    node["entrypoint"] = version["entrypoint"]
                    node["format"] = version["format"]
            if diagnostic:
                node["availability"] = {"status": "unavailable", "reason": diagnostic}
            else:
                node["availability"] = {"status": "indexed", "reason": "文件可用性需由加载器检查"}
            parent = nodes.get(row["parent_folder_id"])
            (parent or root).setdefault("children", []).append(node)
        return tree

    def append_scene_poi_nodes(tree: list[dict], scene_id: str) -> list[dict]:
        if scenes is None or spatial_store is None:
            return tree
        layers = scenes.poi_layers(scene_id)
        if not layers:
            return tree
        try:
            spatial_store.ensure_schema()
            with spatial_store.connect() as connection:
                rows = connection.execute(
                    "SELECT id,name,shape_type,feature_count,bounds FROM spatial_dataset WHERE id=ANY(%s::uuid[])",
                    ([layer["dataset_id"] for layer in layers],),
                ).fetchall()
        except Exception:
            return tree
        datasets = {str(row["id"]): dict(row) for row in rows}
        children = []
        for layer in layers:
            dataset = datasets.get(layer["dataset_id"])
            if dataset is None:
                continue
            children.append({
                "id": "spatial-poi-scope:" + layer["id"],
                "label": layer["name"] + " · " + dataset["name"],
                "type": "SpatialPoi", "attribute": {"IsChecked": False},
                "dataset": {**dataset, "id": str(dataset["id"])}, "scopeBbox": layer["bbox"],
                "agentScopedPoi": True, "persistedPoiLayerId": layer["id"],
                "children": [], "isAdd": False, "isEdit": False,
                "isDelete": False, "isSetResource": False,
            })
        if children:
            group = next((item for item in tree if isinstance(item, dict) and item.get("id") == "spatial-poi-group"), None)
            if group is not None:
                group.setdefault("children", []).extend(children)
            else:
                tree.insert(1 if tree else 0, {
                    "id": "spatial-poi-group", "label": "POI 图层", "type": "SpatialPoiGroup",
                    "attribute": {"IsChecked": False}, "children": children,
                    "isAdd": False, "isEdit": False, "isDelete": False, "isSetResource": False,
                })
        return tree

    def append_scene_video_nodes(tree: list[dict], scene_id: str) -> list[dict]:
        if scenes is None or spatial_store is None:
            return tree
        group_ids = scenes.video_group_ids(scene_id)
        if not group_ids:
            return tree
        try:
            with spatial_store.connect() as connection:
                rows = connection.execute(
                    "SELECT id,name,ST_AsGeoJSON(spatial_extent) AS coverage "
                    "FROM video_group WHERE status='published' AND id=ANY(%s::uuid[])",
                    (group_ids,),
                ).fetchall()
        except Exception:
            return tree
        by_id = {str(row["id"]): row for row in rows}
        children = [{
            "id": f"fixed-video:{group_id}", "label": by_id[group_id]["name"],
            "type": "FixedVideo", "videoGroupId": group_id,
            "coverage": json.loads(by_id[group_id]["coverage"]),
            "attribute": {"IsChecked": False}, "children": [],
            "isAdd": False, "isEdit": False, "isDelete": False, "isSetResource": False,
        } for group_id in group_ids if group_id in by_id]
        if children:
            tree.insert(1 if tree else 0, {
                "id": "fixed-video-group", "label": "定点视频", "type": "FixedVideoGroup",
                "attribute": {"IsChecked": False}, "children": children,
                "isAdd": False, "isEdit": False, "isDelete": False, "isSetResource": False,
            })
        return tree

    def append_scene_data_nodes(tree: list[dict], scene_id: str) -> list[dict]:
        return append_persisted_layers(append_scene_video_nodes(append_scene_poi_nodes(tree, scene_id), scene_id), scene_id)

    def ensure_annotations() -> Response | None:
        if spatial_store is None:
            return _json({"State": 503, "Message": "标绘存储未配置"}, 503)
        try:
            spatial_store.ensure_schema()
        except Exception:
            return _json({"State": 503, "Message": "标绘存储暂不可用"}, 503)
        return None

    def annotation_files(user_id: str, annotation_id: str) -> list[dict]:
        with spatial_store.connect() as connection:
            rows = connection.execute(
                """SELECT id,original_name,content_type,size_bytes,created_at
                   FROM user_annotation_file
                   WHERE owner_user_id=%s AND annotation_id=%s
                   ORDER BY created_at,id""",
                (user_id, annotation_id),
            ).fetchall()
        return [{
            "Id": str(row["id"]), "FileId": str(row["id"]),
            "ArLabelId": annotation_id, "FileName": row["original_name"],
            "ContentType": row["content_type"], "Size": row["size_bytes"],
            "Url": f"/api/ArLabel/GetFile/{row['id']}",
        } for row in rows]

    def annotation_query(user_id: str, annotation_id: str | None = None, scene_id: str | None = None,
                         label: str | None = None) -> list[dict]:
        conditions = ["owner_user_id=%s"]
        params: list = [user_id]
        if annotation_id is not None:
            conditions.append("id=%s")
            params.append(annotation_id)
        if scene_id:
            conditions.append("properties->>'ArSceneId'=%s")
            params.append(scene_id)
        if label is not None:
            conditions.append("name=%s")
            params.append(label)
        with spatial_store.connect() as connection:
            return connection.execute(
                f"""SELECT id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,
                           ST_AsText(geom) AS range_wkt
                    FROM user_annotation WHERE {' AND '.join(conditions)}
                    ORDER BY created_at,id""",
                params,
            ).fetchall()

    def save_plot_uploads(user_id: str, annotation_id: str, uploads: list[StarletteUploadFile]) -> list[dict]:
        if not uploads:
            return []
        root = spatial_store.data_dir.resolve()
        directory = root / "spatial" / "annotation-files" / user_id / annotation_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        saved = []
        total = 0
        limit = int(os.getenv("DEQING_MAX_ANNOTATION_FILE_BYTES", str(100 * 1024 * 1024)))
        try:
            for upload in uploads:
                original_name = Path((upload.filename or "attachment").replace("\\", "/")).name[:255]
                extension = Path(original_name).suffix[:20]
                file_id = str(uuid.uuid4())
                target = directory / f"{file_id}{extension}"
                size = 0
                saved.append({
                    "id": file_id,
                    "original_name": original_name,
                    "content_type": upload.content_type or "application/octet-stream",
                    "relative_path": target.relative_to(root).as_posix(),
                    "size_bytes": 0,
                    "path": target,
                })
                with target.open("xb") as output:
                    while chunk := upload.file.read(1024 * 1024):
                        size += len(chunk)
                        total += len(chunk)
                        if total > limit:
                            raise ValueError("标绘附件超出大小限制")
                        output.write(chunk)
                saved[-1]["size_bytes"] = size
            return saved
        except Exception:
            for item in saved:
                item["path"].unlink(missing_ok=True)
            raise

    def public_file_path(relative_path: str) -> Path | None:
        if spatial_store is None:
            return None
        root = spatial_store.data_dir.resolve()
        candidate = (root / relative_path).resolve()
        return candidate if candidate.is_relative_to(root) and candidate.is_file() else None

    async def parse_plot_request(request: Request) -> tuple[dict | None, list[StarletteUploadFile], int]:
        content_type = request.headers.get("content-type", "").lower()
        if "multipart/form-data" in content_type:
            try:
                form = await request.form()
            except Exception:
                return None, [], 400
            payload: dict = {}
            uploads: list[StarletteUploadFile] = []
            for key, value in form.multi_items():
                if isinstance(value, StarletteUploadFile):
                    uploads.append(value)
                else:
                    payload[key] = str(value)
            return payload, uploads, 200
        payload, status = await _body(request)
        return payload, [], status

    async def write_annotation(request: Request, editing: bool) -> Response:
        user = account_user(request)
        if user is None:
            return unauthorized()
        unavailable = ensure_annotations()
        if unavailable is not None:
            return unavailable
        payload, uploads, status = await parse_plot_request(request)
        if payload is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        name = str(payload.get("Label") or payload.get("label") or "").strip()
        if not name:
            return _json({"State": 1, "Message": "标绘名称不能为空"})
        geometry = _plot_geometry(payload)
        if geometry is None:
            return _json({"State": 1, "Message": "标绘坐标无效"})
        if geometry[0] == "geojson":
            geometry_sql = "ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)"
        else:
            geometry_sql = "ST_SetSRID(ST_GeomFromText(%s),4326)"

        raw_id = payload.get("Id") or payload.get("id")
        try:
            annotation_id = str(uuid.UUID(str(raw_id))) if editing and raw_id else str(uuid.uuid4())
        except (ValueError, TypeError, AttributeError):
            return _json({"State": 1, "Message": "标绘 ID 无效"})
        if editing and not raw_id:
            return _json({"State": 1, "Message": "编辑请求缺少标绘 ID"})

        properties = {key: value for key, value in payload.items() if key not in {"Id", "id", "geometry"}}
        properties["Label"] = name
        if isinstance(properties.get("Type"), str) and properties["Type"].isdigit():
            properties["Type"] = int(properties["Type"])
        if "InstructionJson" in properties and isinstance(properties["InstructionJson"], str):
            try:
                json.loads(properties["InstructionJson"])
            except json.JSONDecodeError:
                return _json({"State": 1, "Message": "InstructionJson 格式无效"})
        try:
            files = save_plot_uploads(user["id"], annotation_id, uploads)
        except ValueError as error:
            return _json({"State": 1, "Message": str(error)}, 413)
        except OSError:
            return _json({"State": 503, "Message": "无法保存标绘附件"}, 503)

        root = spatial_store.data_dir.resolve()
        try:
            with spatial_store.connect() as connection:
                if editing:
                    result = connection.execute(
                        f"""UPDATE user_annotation
                            SET name=%s,properties=%s::jsonb,geom={geometry_sql},updated_at=now()
                            WHERE id=%s AND owner_user_id=%s
                            RETURNING id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,
                                      ST_AsText(geom) AS range_wkt""",
                        (name, json.dumps(properties, ensure_ascii=False), geometry[1], annotation_id, user["id"]),
                    ).fetchone()
                    if result is None:
                        raise LookupError("标绘不存在或无权修改")
                else:
                    result = connection.execute(
                        f"""INSERT INTO user_annotation(id,owner_user_id,name,properties,geom)
                            VALUES (%s,%s,%s,%s::jsonb,{geometry_sql})
                            RETURNING id,name,properties,ST_AsGeoJSON(geom)::jsonb AS geometry,
                                      ST_AsText(geom) AS range_wkt""",
                        (annotation_id, user["id"], name, json.dumps(properties, ensure_ascii=False), geometry[1]),
                    ).fetchone()
                for item in files:
                    connection.execute(
                        """INSERT INTO user_annotation_file
                           (id,annotation_id,owner_user_id,original_name,content_type,relative_path,size_bytes)
                           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (item["id"], annotation_id, user["id"], item["original_name"],
                         item["content_type"], item["relative_path"], item["size_bytes"]),
                    )
                delete_ids = payload.get("DeleteFileIds")
                if editing and delete_ids:
                    try:
                        parsed_ids = json.loads(delete_ids) if isinstance(delete_ids, str) else delete_ids
                    except json.JSONDecodeError:
                        parsed_ids = []
                    if isinstance(parsed_ids, list):
                        deleted_files = connection.execute(
                            """DELETE FROM user_annotation_file
                               WHERE annotation_id=%s AND owner_user_id=%s AND id=ANY(%s::uuid[])
                               RETURNING relative_path""",
                            (annotation_id, user["id"], [str(item) for item in parsed_ids]),
                        ).fetchall()
                    else:
                        deleted_files = []
                else:
                    deleted_files = []
            for item in deleted_files:
                candidate = public_file_path(item["relative_path"])
                if candidate is not None:
                    candidate.unlink(missing_ok=True)
        except LookupError:
            for item in files:
                item["path"].unlink(missing_ok=True)
            return _json({"State": 1, "Message": "标绘不存在或无权修改"}, 404)
        except Exception:
            for item in files:
                item["path"].unlink(missing_ok=True)
            return _json({"State": 503, "Message": "标绘存储暂不可用"}, 503)

        result["id"] = str(result["id"])
        file_rows = annotation_files(user["id"], result["id"])
        return _json({
            "State": 0,
            "Message": "保存成功",
            "Data": _old_plot_record(result, file_rows),
        })

    @api.post("/api/ArLabel/Add")
    @api.post("/api/ArLabel/AddByForm")
    async def add_legacy_annotation(request: Request):
        return await write_annotation(request, editing=False)

    @api.put("/api/ArLabel/Edit")
    @api.post("/api/ArLabel/EditByForm")
    async def edit_legacy_annotation(request: Request):
        return await write_annotation(request, editing=True)

    @api.delete("/api/ArLabel/Delete/{scene_id}/{annotation_id}")
    async def delete_legacy_annotation(scene_id: str, annotation_id: str, request: Request):
        user = account_user(request)
        if user is None:
            return unauthorized()
        unavailable = ensure_annotations()
        if unavailable is not None:
            return unavailable
        try:
            normalized_id = str(uuid.UUID(annotation_id))
        except ValueError:
            return _json({"State": 1, "Message": "标绘 ID 无效"})
        with spatial_store.connect() as connection:
            owned = connection.execute(
                """SELECT id FROM user_annotation
                   WHERE id=%s AND owner_user_id=%s AND properties->>'ArSceneId'=%s""",
                (normalized_id, user["id"], scene_id),
            ).fetchone()
            if owned is None:
                return _json({"State": 1, "Message": "标绘不存在或无权删除"}, 404)
            deleted_files = connection.execute(
                """SELECT relative_path FROM user_annotation_file
                   WHERE annotation_id=%s AND owner_user_id=%s""",
                (normalized_id, user["id"]),
            ).fetchall()
            connection.execute(
                "DELETE FROM user_annotation WHERE id=%s AND owner_user_id=%s",
                (normalized_id, user["id"]),
            )
        for row in deleted_files:
            path = public_file_path(row["relative_path"])
            if path is not None:
                path.unlink(missing_ok=True)
        return _json({"State": 0, "Message": "删除成功"})

    @api.get("/api/ArLabel/GetByLabelAndId/{scene_id}/{annotation_id}")
    async def get_legacy_annotation_by_name(scene_id: str, annotation_id: str, request: Request):
        user = account_user(request)
        if user is None:
            return unauthorized()
        unavailable = ensure_annotations()
        if unavailable is not None:
            return unavailable
        label = request.query_params.get("Label")
        annotation_id_filter = None
        if annotation_id not in {"", "0", "null", "undefined"}:
            try:
                annotation_id_filter = str(uuid.UUID(annotation_id))
            except ValueError:
                return _json({"State": 1, "Message": "标绘 ID 无效"})
        rows = annotation_query(user["id"], annotation_id_filter, scene_id, label)
        item = _old_plot_record(rows[0], annotation_files(user["id"], str(rows[0]["id"]))) if rows else None
        return _json({"State": 0, "Message": "获取成功", "Data": item})

    @api.get("/api/ArLabel/GetFilesByArLabelId/{annotation_id}")
    async def get_legacy_annotation_files(annotation_id: str, request: Request):
        user = account_user(request)
        if user is None:
            return unauthorized()
        unavailable = ensure_annotations()
        if unavailable is not None:
            return unavailable
        try:
            normalized_id = str(uuid.UUID(annotation_id))
        except ValueError:
            return _json({"State": 1, "Message": "标绘 ID 无效"})
        rows = annotation_files(user["id"], normalized_id)
        with spatial_store.connect() as connection:
            owned = connection.execute(
                "SELECT 1 FROM user_annotation WHERE id=%s AND owner_user_id=%s",
                (normalized_id, user["id"]),
            ).fetchone()
        return _json(rows if owned else [])

    @api.get("/api/ArLabel/GetFile/{file_id}")
    async def get_legacy_annotation_file(file_id: str, request: Request):
        user = account_user(request)
        if user is None:
            return unauthorized()
        unavailable = ensure_annotations()
        if unavailable is not None:
            return unavailable
        try:
            normalized_id = str(uuid.UUID(file_id))
        except ValueError:
            return Response(status_code=404)
        with spatial_store.connect() as connection:
            row = connection.execute(
                """SELECT original_name,content_type,relative_path
                   FROM user_annotation_file WHERE id=%s AND owner_user_id=%s""",
                (normalized_id, user["id"]),
            ).fetchone()
        path = public_file_path(row["relative_path"]) if row else None
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type=row["content_type"], filename=row["original_name"])

    @api.post("/api/ArScene/GetByPage")
    async def scene_page(request: Request):
        if account_user(request) is None:
            return unauthorized()
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        try:
            page_index = max(1, int(body.get("PageIndex") or 1))
            page_size = max(0, min(500, int(body.get("PageSize", 20))))
        except (TypeError, ValueError):
            return _json({"State": 1, "Message": "分页参数不合法"})

        local_rows = scenes.rows() if scenes is not None else []
        name = str(body.get("Name") or "").strip().casefold()
        start_time, end_time = body.get("StartTime"), body.get("EndTime")
        if name:
            local_rows = [row for row in local_rows if name in str(row.get("Name") or "").casefold()]
        if start_time:
            local_rows = [row for row in local_rows if str(row.get("CreateByTime") or "") >= str(start_time)]
        if end_time:
            bound = str(end_time) + ("T23:59:59" if len(str(end_time)) == 10 else "")
            local_rows = [row for row in local_rows if str(row.get("CreateByTime") or "") <= bound]
        start = (page_index - 1) * page_size
        return _json({"Total": len(local_rows), "Rows": local_rows[start:start + page_size]})

    @api.delete("/api/ArScene/Delete/{scene_id}")
    async def delete_scene(scene_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        try:
            normalized_id = str(uuid.UUID(scene_id))
        except (ValueError, TypeError, AttributeError):
            return _json({"State": 1, "Message": "场景 ID 无效"}, 404)
        deleted = await asyncio.to_thread(scenes.delete, normalized_id)
        if not deleted:
            return _json({"State": 404, "Message": "场景不存在或不支持删除"}, 404)
        return _json({"State": 0, "Message": "删除成功"})

    @api.get("/api/ArScene/GetByNameAndId/{scene_id}")
    async def scene_name_exists(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        name = str(request.query_params.get("Name") or "").strip()
        if not name:
            return _json({"State": 0, "Data": False})
        duplicate = scenes.name_exists(name, scene_id) if scenes is not None else False
        return _json({"State": 0, "Data": duplicate})

    @api.post("/api/ArScene/Add")
    async def add_scene(request: Request):
        user, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        name = str(body.get("Name") or "").strip()
        remark = str(body.get("Remark") or "").strip() or None
        if not 1 <= len(name) <= 20:
            return _json({"State": 1, "Message": "场景名称长度应为1至20个字符"})
        if remark is not None and len(remark) > 250:
            return _json({"State": 1, "Message": "场景描述不能超过250个字符"})
        if scenes.name_exists(name):
            return _json({"State": 1, "Message": "场景名称已存在"})
        try:
            engine_type = int(body.get("SceneEngineType", 2))
            tree_type = int(body.get("SceneTreeType", 1))
            if engine_type not in (1, 2, 3) or tree_type not in (1, 2):
                raise ValueError
        except (TypeError, ValueError):
            return _json({"State": 1, "Message": "场景类型参数不合法"})
        try:
            row = await asyncio.to_thread(scenes.create, user["id"], name, remark, engine_type, tree_type)
        except sqlite3.IntegrityError:
            return _json({"State": 1, "Message": "场景名称已存在"})
        return _json({"State": 0, "Message": "创建成功", "Data": row})

    @api.put("/api/ArScene/Edit")
    async def edit_scene(request: Request):
        user, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        try:
            scene_id = str(uuid.UUID(str(body.get("Id") or "")))
        except (ValueError, TypeError, AttributeError):
            return _json({"State": 1, "Message": "场景 ID 无效"}, 422)
        current = scenes.get(scene_id)
        if current is None:
            return _json({"State": 404, "Message": "场景不存在"}, 404)
        name = str(body.get("Name") or "").strip()
        remark = str(body.get("Remark") or "").strip() or None
        if not 1 <= len(name) <= 20 or remark is not None and len(remark) > 250:
            return _json({"State": 1, "Message": "场景名称或描述长度不合法"})
        for field in ("SceneTreeType", "SceneEngineType"):
            if field in body and str(body[field]) != str(current[field]):
                return _json({"State": 1, "Message": f"{field} 创建后不可修改"})
        try:
            updated = await asyncio.to_thread(scenes.update, scene_id, name, remark, user["id"])
        except (ValueError, sqlite3.IntegrityError):
            return _json({"State": 1, "Message": "场景名称已存在"})
        return _json({"State": 0, "Message": "修改成功", "Data": updated})

    def _scene_config(scene: dict, *, cim: bool) -> dict:
        resource_rows = scenes.resource_rows(scene["Id"]) if scenes is not None else []
        type_to_key = {
            1000008: "DemCTB", 1000012: "Terrain3dtiles", 1000015: "Terrain3dtiles",
            1000016: "DemCTB", 2000004: "VectorWfsLayer", 2000027: "VectorMvtLayer",
            2000033: "Vector3DTiles", 4000001: "ImageOblique", 4000002: "ImageStreetPano",
            4000003: "ImageIndoorPano", 4000004: "ImageAirPano", 4000005: "ImageLand",
            5000012: "Model3dtiles", 10000001: "MediaDirectionalVideo",
            10000004: "MediaDirectionalVideoDynamic", 10000005: "MediaUndirectionalVideo",
            10000006: "Http_Flv", 10000007: "Hls", 12000004: "RasterWMS",
            12000005: "RasterWMTS", 12000008: "RasterTMS", 13000006: "PointCloud3Dtiles",
            14000003: "BIM3Dtiles", 21000001: "PointCloudGaussian3DTiles",
        }
        default_type = type_to_key.get(int(resource_rows[0]["resource_type"])) if resource_rows else None
        common = {
            "MetaData": json.dumps({
                "ServerUrl": None, "Scale": None, "IsPublishWfs": True,
                "SRSOrigin": [0.0, 0.0, 0.0], "LocationPoint": [0.0, 0.0, 0.0],
                "OffsetOrigin": [0.0, 0.0, 0.0], "boundingsphere": [0.0, 0.0, 0.0, 0.0],
                "BoundingBox": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "IsSharePath": False, "ShareRootPath": None, "ShareUserName": None,
                "SharePassword": None, "IsRangeSelectedDownload": False,
                "IsMigrate": False, "FastTrackerAddress": None,
            }, separators=(",", ":")),
            "ConfigStr": "", "ManipulatorType": scene.get("ManipulatorType") or 1,
            "LightSource": scene.get("LightSource"), "DefaultResourceType": default_type,
            "EpsgId": 4326,
            "Proj4text": "+proj=longlat +datum=WGS84 +no_defs ",
            "Srstext": 'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],AUTHORITY["EPSG","4326"]]',
            "SceneFinenessType": scene.get("SceneFinenessType") or 1,
            "ModelFinenessType": scene.get("ModelFinenessType") or 1,
            "SceneBgColor": scene.get("SceneBgColor") or "#213D5E",
            "SceneTreeType": scene.get("SceneTreeType") or 1,
            "LogoFileId": scene.get("LogoFileId"),
            "ResourceCommonAttr": scene.get("ResourceCommonAttr"),
            "Name": scene.get("Name"),
        }
        if cim:
            common["EffectsConfig"] = scene.get("EffectsConfig") or "{}"
            common["MetaData"] = json.dumps({
                "BoundingBoxDb3FileId": None, "IsExistTopTile": False,
                "ServerUrl": None, "SRSOrigin": [0.0, 0.0, 0.0],
                "LocationPoint": [0.0, 0.0, 0.0], "OffsetOrigin": [0.0, 0.0, 0.0],
                "boundingsphere": [0.0, 0.0, 0.0, 0.0],
                "BoundingBox": [0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
                "IsSharePath": False, "ShareRootPath": None, "ShareUserName": None,
                "SharePassword": None, "IsRangeSelectedDownload": False,
                "IsMigrate": False, "FastTrackerAddress": None,
            }, separators=(",", ":"))
        return common

    @api.get("/api/ArScene/GetDetail/{scene_id}")
    async def get_scene_detail(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        scene = scenes.get(scene_id) if scenes is not None else None
        if scene is not None:
            return _json(scene)
        return Response(status_code=404)

    @api.get("/api/ArScene/GetSceneConfig/{scene_id}")
    async def get_scene_config(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        scene = scenes.get(scene_id) if scenes is not None else None
        if scene is not None:
            return _json(_scene_config(scene, cim=False))
        return Response(status_code=404)

    @api.get("/api/cim/CIMScene/GetCIMSceneConfig/{scene_id}")
    async def get_cim_scene_config(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        scene = scenes.get(scene_id) if scenes is not None else None
        if scene is not None:
            return _json(_scene_config(scene, cim=True))
        return Response(status_code=404)

    async def database_scene_tree(request: Request, scene_id: str) -> Response:
        user = account_user(request)
        if user is None:
            return unauthorized()
        if scenes is None or scenes.get(scene_id) is None:
            return Response(status_code=404)
        tree = append_scene_data_nodes(local_scene_tree(scene_id), scene_id)
        rows = annotation_query(user["id"], scene_id=scene_id) if ensure_annotations() is None else []
        plots = [_old_plot_record(row, annotation_files(user["id"], str(row["id"]))) for row in rows]

        def merge_plots(value) -> bool:
            if isinstance(value, list):
                return any(merge_plots(item) for item in value)
            if isinstance(value, dict):
                if value.get("type") == "PlottingGroup":
                    children = value.setdefault("children", [])
                    ids = {str(item.get("id")) for item in children if isinstance(item, dict)}
                    children.extend(item for item in plots if item["id"] not in ids)
                    return True
                return any(merge_plots(child) for child in value.get("children", []))
            return False

        if plots and not merge_plots(tree):
            tree.append({"id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"deqing-plotting:{scene_id}")),
                         "label": "标绘图层", "type": "PlottingGroup", "attribute": "",
                         "isAdd": False, "isEdit": False, "isDelete": False,
                         "iconUrl": "biaohui", "isSetResource": False, "sort": 0,
                         "children": plots})
        return _json(tree)

    @api.get("/api/ArScene/GetResourceTreeForScene/{scene_id}")
    async def scene_tree_with_user_plots(scene_id: str, request: Request):
        return await database_scene_tree(request, scene_id)

    @api.get("/api/cim/CIMScene/GetResourceTreeForCIMScene/{scene_id}")
    async def cim_scene_tree_with_user_plots(scene_id: str, request: Request):
        return await database_scene_tree(request, scene_id)

    category_labels = {
        "imagery": "区域影像图层", "terrain": "区域地形图层", "model": "区域模型图层",
        "pointcloud": "区域点云图层", "poi": "区域 POI 图层", "vector": "区域矢量图层",
        "raster": "区域栅格图层", "video": "视频图层",
    }

    def scene_exists(scene_id: str) -> bool:
        if scenes is None or scenes.get(scene_id) is None:
            return False
        scenes.migrate_legacy_tree(scene_id, store.resources)
        return True

    def uuid_text(value, label: str) -> str:
        try:
            return str(uuid.UUID(str(value)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError(f"{label} 必须是 UUID") from None

    @api.get("/api/scenes/{scene_id}/folders")
    async def list_scene_layer_folders(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        if not scene_exists(scene_id):
            return _json({"error": "场景不存在"}, 404)
        return _json({"folders": scenes.layer_folders(scene_id)})

    @api.post("/api/scenes/{scene_id}/folders")
    async def create_scene_layer_folder(scene_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if not scene_exists(scene_id):
            return _json({"error": "场景不存在"}, 404)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"error": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            category = str(body.get("layer_category") or "")
            label = str(body.get("label") or "").strip()
            if category not in {*category_labels, "mixed"} or not 1 <= len(label) <= 100:
                raise ValueError("类别或目录名称无效")
            parent_id = uuid_text(body["parent_id"], "parent_id") if body.get("parent_id") else None
            sort = body.get("sort")
            if sort is not None and (type(sort) is not int or sort < 0):
                raise ValueError("sort 必须是非负整数")
            if parent_id:
                parent = next((row for row in scenes.layer_folders(scene_id) if row["id"] == parent_id), None)
                if parent is None or parent["layer_category"] != category:
                    raise ValueError("父目录不存在或类别不匹配")
            return _json(scenes.create_layer_folder(scene_id, label, category, parent_id, sort), 201)
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)

    @api.patch("/api/scenes/{scene_id}/folders/{folder_id}")
    async def patch_scene_layer_folder(scene_id: str, folder_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"error": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            folder_id = uuid_text(folder_id, "folder_id")
            changes = {}
            if "label" in body:
                label = str(body["label"]).strip()
                if not 1 <= len(label) <= 100:
                    raise ValueError("目录名称长度应为 1 至 100")
                changes["label"] = label
            if "sort" in body:
                if type(body["sort"]) is not int or body["sort"] < 0:
                    raise ValueError("sort 必须是非负整数")
                changes["sort"] = body["sort"]
            if "parent_id" in body:
                changes["parent_id"] = uuid_text(body["parent_id"], "parent_id") if body["parent_id"] else None
            if not scenes.update_layer_folder(scene_id, folder_id, changes):
                return _json({"error": "目录不存在"}, 404)
            return _json(next(row for row in scenes.layer_folders(scene_id) if row["id"] == folder_id))
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)

    @api.delete("/api/scenes/{scene_id}/folders/{folder_id}")
    async def remove_scene_layer_folder(scene_id: str, folder_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        try:
            folder_id = uuid_text(folder_id, "folder_id")
            if not scenes.delete_layer_folder(scene_id, folder_id):
                return _json({"error": "目录不存在"}, 404)
            return _json({"removed": True})
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)

    @api.get("/api/scenes/{scene_id}/layers")
    async def list_scene_layers(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        if not scene_exists(scene_id):
            return _json({"error": "场景不存在"}, 404)
        return _json({"layers": scenes.layers(scene_id)})

    @api.post("/api/scenes/{scene_id}/layers")
    async def create_scene_layer(scene_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if not scene_exists(scene_id):
            return _json({"error": "场景不存在"}, 404)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"error": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            category = str(body.get("layer_category") or "")
            source_kind = str(body.get("source_kind") or "")
            if category not in category_labels or source_kind not in {"legacy_resource", "spatial_dataset", "pointcloud_version", "pointcloud_resource", "video_group", "oblique_resource", "raster_collection"}:
                raise ValueError("图层类别或来源类型不支持")
            source_id = uuid_text(body.get("source_id"), "source_id")
            display_name = str(body.get("display_name") or "").strip()
            if not 1 <= len(display_name) <= 100:
                raise ValueError("display_name 长度应为 1 至 100")
            if type(body.get("default_visible", False)) is not bool:
                raise ValueError("default_visible 必须是布尔值")
            sort = body.get("sort")
            if sort is not None and (type(sort) is not int or sort < 0):
                raise ValueError("sort 必须是非负整数")
            request_id = body.get("request_id")
            if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
                raise ValueError("request_id 长度应为 1 至 128")
            crs = str(body.get("scope_crs") or "EPSG:4326")
            scope = (canonical_geometry(body["scope_geometry"], crs, maximum_area_km2=(
                SPATIAL_DATASET_QUERY_MAX_AREA_KM2 if source_kind == "spatial_dataset" else None
            ), unlimited_area=(source_kind == "spatial_dataset" and source_id == str(UNIFIED_POI_ID)))
                     if body.get("scope_geometry") is not None else None)
            subset_id = uuid_text(body["subset_resource_id"], "subset_resource_id") if body.get("subset_resource_id") else None
            legacy_subset = None
            requested_version = str(body["source_version_id"]).strip() if body.get("source_version_id") else None
            mode = str(body.get("selection_mode") or "").strip() or None
            if requested_version and source_kind in {"pointcloud_version","pointcloud_resource","legacy_resource"}:
                requested_version = uuid_text(requested_version,"source_version_id")
            parent_id = uuid_text(body["parent_folder_id"], "parent_folder_id") if body.get("parent_folder_id") else None
            filter_properties = body.get("filter_properties")
            if filter_properties is not None:
                if source_kind != "spatial_dataset" or category != "poi" or not isinstance(filter_properties, dict):
                    raise ValueError("filter_properties 仅支持 POI 图层的 JSON 对象")
                try:
                    encoded_filter = json.dumps(filter_properties, ensure_ascii=False, allow_nan=False)
                except (TypeError, ValueError):
                    raise ValueError("filter_properties 必须包含合法 JSON 值") from None
                if len(encoded_filter.encode("utf-8")) > 8192:
                    raise ValueError("filter_properties 不能超过 8 KiB")
                if not filter_properties:
                    filter_properties = None
            if source_kind == "legacy_resource":
                source = next((row for row in store.resources if str(row.get("Id")) == source_id and not row.get("IsDeleted")), None)
                if source is None:
                    raise ValueError("源资源不存在")
                resource_type = int(source.get("Type") or 0)
                expected = {"terrain", "model"} if resource_type == 1000012 else {
                    LegacySceneStore.resource_category(resource_type, source.get("BigResourceType"))
                }
                if category not in expected:
                    raise ValueError("图层类别与源资源类型不匹配")
                if subset_id:
                    legacy_subset = (subsets.get(subset_id) if subsets else None) or (oblique_subsets.get(subset_id) if oblique_subsets else None)
                    if legacy_subset is None or legacy_subset["source_resource_id"] != source_id:
                        raise ValueError("区域子资源不存在或与源资源不匹配")
                    if scope is not None and scope != legacy_subset["geometry"]:
                        raise ValueError("范围与区域子资源的实际范围不一致")
                    scope = legacy_subset["geometry"]
                if scope is not None and resource_type not in {1000012,13000006}:
                    if legacy_subset is None:
                        raise ValueError("该旧目录资源尚无可用的逐文件空间索引")
            elif source_kind == "spatial_dataset":
                if category not in {"poi", "vector"} or subset_id:
                    raise ValueError("空间数据集仅可作为 POI 或矢量图层")
                if scope is None:
                    raise ValueError("空间数据集区域图层必须提供 scope_geometry")
                if spatial_store is None:
                    return _json({"error": "空间存储不可用"}, 503)
                with spatial_store.connect() as connection:
                    dataset = connection.execute("SELECT shape_type FROM spatial_dataset WHERE id=%s", (source_id,)).fetchone()
                if dataset is None or (category == "poi" and int(dataset["shape_type"]) != 1) or (
                    category == "vector" and int(dataset["shape_type"]) not in {5, 15}
                ):
                    raise ValueError("数据集不存在或几何类型与图层类别不匹配")
            elif source_kind == "video_group":
                if category != "video":
                    raise ValueError("视频资料组仅可作为视频图层")
                if spatial_store is None:
                    return _json({"error": "视频存储不可用"}, 503)
                with spatial_store.connect() as connection:
                    video = connection.execute("SELECT 1 FROM video_group WHERE id=%s AND status='published'", (source_id,)).fetchone()
                if video is None:
                    raise ValueError("视频资料组不存在或尚未发布")
            elif source_kind == "oblique_resource":
                if category != "imagery":
                    raise ValueError("ODM 影像资源包仅可作为影像图层关联")
                if spatial_store is None:
                    return _json({"error": "空间存储不可用"}, 503)
                with spatial_store.connect() as connection:
                    resource = connection.execute("SELECT 1 FROM oblique_resource WHERE id=%s", (source_id,)).fetchone()
                if resource is None:
                    raise ValueError("ODM 影像资源包不存在")
            elif source_kind == "raster_collection":
                if category != "raster":
                    raise ValueError("时序影像集合仅可作为栅格图层关联")
                if spatial_store is None:
                    return _json({"error": "空间存储不可用"}, 503)
                with spatial_store.connect() as connection:
                    resource = connection.execute(
                        "SELECT c.crs_epsg,c.tile_scheme,c.tile_size,c.tile_format,"
                        "count(e.acquisition_date) AS published_epoch_count "
                        "FROM raster_collection c LEFT JOIN raster_epoch e ON e.collection_id=c.id "
                        "AND e.status='published' WHERE c.id=%s GROUP BY c.id", (source_id,)).fetchone()
                if resource is None or resource["published_epoch_count"] < 1:
                    raise ValueError("时序影像集合不存在或尚无已发布期次")
                if (resource["crs_epsg"] != 3857 or str(resource["tile_scheme"]).lower() != "tms"
                        or resource["tile_size"] != 256 or str(resource["tile_format"]).lower() != "png"):
                    raise ValueError("当前加载器仅支持 EPSG:3857 TMS/PNG 256 像素瓦片")
            else:
                if spatial_store is None:
                    return _json({"error": "点云存储不可用"}, 503)
                with spatial_store.connect() as connection:
                    if source_kind == "pointcloud_version":
                        version = connection.execute("SELECT resource_id,format FROM pointcloud_version WHERE id=%s AND status='published'", (source_id,)).fetchone()
                        if version:
                            requested_version = source_id
                            source_id = str(version["resource_id"])
                            source_kind = "pointcloud_resource"
                    else:
                        version = connection.execute("""SELECT resource_id,format FROM pointcloud_version
                            WHERE resource_id=%s AND status='published' AND (%s::uuid IS NULL OR id=%s::uuid)
                            ORDER BY published_at DESC NULLS LAST LIMIT 1""",
                            (source_id,requested_version,requested_version)).fetchone()
                if version is None:
                    raise ValueError("点云版本不存在或尚未发布")
                if version["format"] != "3dtiles" or category not in {"pointcloud","terrain","model"}:
                    raise ValueError("当前场景图层仅支持已发布的 3D Tiles 版本")
            if spatial_store is None:
                return _json({"code":"index_unavailable","error":"空间存储不可用"},503)
            if legacy_subset is not None:
                actual_kind,canonical_id,pinned_version = source_kind,source_id,legacy_subset.get("source_version")
                mode = mode or "intersecting_tiles"
            else:
                try:
                    actual_kind, canonical_id, pinned_version = resolve_source_version(
                        spatial_store,source_kind,source_id,requested_version)
                except LookupError as exc:
                    if source_kind == "legacy_resource" and scope is None and requested_version is None:
                        actual_kind,canonical_id,pinned_version = source_kind,source_id,None
                    else:
                        return _json({"code":str(exc),"error":"源资源或指定版本不可用"},409)
            source_id = canonical_id
            source_kind = actual_kind
            if scope is not None and legacy_subset is None:
                mode = mode or {"pointcloud_resource":"intersecting_tiles",
                                "raster_collection":"intersecting_tiles",
                                "spatial_dataset":"feature_intersects",
                                "oblique_resource":"candidate_footprint_intersects",
                                "video_group":"footprint_intersects"}.get(source_kind)
                preview = run_query(spatial_store, ResourceQuery(
                    source_kind=source_kind,source_resource_id=source_id,
                    source_version_id=pinned_version,geometry=scope,crs="EPSG:4326",
                    selection_mode=mode,limit=1))
                if isinstance(preview, Response):
                    return preview
                if not preview["items"]:
                    return _json({"code":"empty","error":"范围内没有可用数据，未创建图层"},422)
            elif scope is None and mode is not None:
                raise ValueError("selection_mode 必须与 scope_geometry 同时提供")
            item = {"scene_id": scene_id, "parent_folder_id": parent_id, "layer_category": category,
                    "category_label": category_labels[category], "source_kind": source_kind, "source_id": source_id,
                    "subset_resource_id": subset_id, "scope_geometry": scope,
                    "source_version_id": pinned_version, "selection_mode": mode,
                    "filter_properties": filter_properties,
                    "scope_crs": "EPSG:4326" if scope else None, "display_name": display_name,
                    "default_visible": body.get("default_visible", False), "sort": sort, "request_id": request_id}
            layer, reused = scenes.add_layer(item)
            if reused and (layer["layer_category"] != category or layer["source_kind"] != source_kind or
                           layer["source_id"] != source_id or layer["subset_resource_id"] != subset_id or
                           layer.get("source_version_id") != pinned_version or layer.get("selection_mode") != mode or
                           layer["scope_geometry"] != scope or layer["display_name"] != display_name or
                           layer["filter_properties"] != filter_properties or
                           layer["default_visible"] != body.get("default_visible", False) or
                           (parent_id is not None and layer["parent_folder_id"] != parent_id) or
                           (sort is not None and layer["sort"] != sort)):
                return _json({"error": "request_id 已用于其他图层定义"}, 409)
            return _json({**layer, "reused_existing": reused}, 200 if reused else 201)
        except (ValueError, SubsetError) as exc:
            return _json({"error": str(exc)}, 422)
        except (sqlite3.Error, OSError) as exc:
            return _json({"error": "场景图层存储暂不可用", "detail": str(exc)}, 503)

    @api.patch("/api/scenes/{scene_id}/layers/{layer_id}")
    async def patch_scene_layer(scene_id: str, layer_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"error": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            layer_id = uuid_text(layer_id, "layer_id")
            changes = {}
            current_layer = next((row for row in scenes.layers(scene_id) if row["layer_id"] == layer_id), None)
            if current_layer is None:
                return _json({"error":"图层不存在"},404)
            if "display_name" in body:
                value = str(body["display_name"]).strip()
                if not 1 <= len(value) <= 100:
                    raise ValueError("display_name 长度应为 1 至 100")
                changes["display_name"] = value
            if "default_visible" in body:
                if type(body["default_visible"]) is not bool:
                    raise ValueError("default_visible 必须是布尔值")
                changes["default_visible"] = body["default_visible"]
            if "sort" in body:
                if type(body["sort"]) is not int or body["sort"] < 0:
                    raise ValueError("sort 必须是非负整数")
                changes["sort"] = body["sort"]
            if "parent_folder_id" in body:
                changes["parent_folder_id"] = uuid_text(body["parent_folder_id"], "parent_folder_id")
            if "filter_properties" in body:
                value = body["filter_properties"]
                if current_layer["layer_category"] != "poi":
                    raise ValueError("只有 POI 图层支持属性过滤")
                if value is not None and (not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 8192):
                    raise ValueError("filter_properties 必须是不超过 8 KiB 的 JSON 对象")
                changes["filter_properties"] = value
            if any(key in body for key in ("scope_geometry","scope_crs","selection_mode","source_version_id")):
                kind=current_layer["source_kind"]
                source_id=current_layer["source_id"]
                scope=(canonical_geometry(body["scope_geometry"],str(body.get("scope_crs") or "EPSG:4326"),
                                          maximum_area_km2=(SPATIAL_DATASET_QUERY_MAX_AREA_KM2
                                                            if kind == "spatial_dataset" else None),
                                          unlimited_area=(kind == "spatial_dataset" and source_id == str(UNIFIED_POI_ID)))
                       if body.get("scope_geometry") is not None else current_layer["scope_geometry"])
                if "scope_geometry" in body and body["scope_geometry"] is None:
                    scope=None
                requested=str(body["source_version_id"]).strip() if body.get("source_version_id") else current_layer.get("source_version_id")
                mode=str(body.get("selection_mode") or "").strip() if "selection_mode" in body else current_layer.get("selection_mode")
                if scope:
                    mode=mode or {"pointcloud_resource":"intersecting_tiles",
                                  "raster_collection":"intersecting_tiles",
                                  "spatial_dataset":"feature_intersects",
                                  "oblique_resource":"candidate_footprint_intersects",
                                  "video_group":"footprint_intersects"}.get(kind)
                    preview=run_query(spatial_store,ResourceQuery(source_kind=kind,
                        source_resource_id=source_id,source_version_id=requested,
                        geometry=scope,selection_mode=mode,limit=1))
                    if isinstance(preview,Response):
                        return preview
                    if not preview["items"]:
                        return _json({"code":"empty","error":"范围内没有可用数据，未修改图层"},422)
                    changes["source_version_id"]=preview["source_version_id"]
                elif requested:
                    try:
                        _,_,pinned=resolve_source_version(spatial_store,kind,source_id,requested)
                    except LookupError as exc:
                        return _json({"code":str(exc),"error":"指定版本不可用"},409)
                    changes["source_version_id"]=pinned
                changes["scope_geometry"]=scope
                changes["selection_mode"]=mode if scope else None
            if not scenes.update_layer(scene_id, layer_id, changes):
                return _json({"error": "图层不存在"}, 404)
            return _json(next(row for row in scenes.layers(scene_id) if row["layer_id"] == layer_id))
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)

    @api.delete("/api/scenes/{scene_id}/layers/{layer_id}")
    async def remove_scene_layer(scene_id: str, layer_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        try:
            layer_id = uuid_text(layer_id, "layer_id")
            if not scenes.delete_layer(scene_id, layer_id):
                return _json({"error": "图层不存在"}, 404)
            return _json({"removed": True})
        except ValueError as exc:
            return _json({"error": str(exc)}, 422)

    @api.post("/api/cim/CIMScene/UpdateTreeResourceSort")
    async def update_tree_resource_sort(request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        raw = await request.body()
        if len(raw) > 2 * 1024 * 1024:
            return _json({"State": 413, "Message": "资源树请求体过大"}, 413)
        try:
            body = json.loads(raw)
            scene_id = uuid_text(body.get("SceneId"), "SceneId")
            tree = json.loads(body.get("SceneTreeJson") or "[]")
            if not isinstance(tree, list):
                raise ValueError("SceneTreeJson 必须是数组")
            if not scene_exists(scene_id):
                return _json({"State": 404, "Message": "场景不存在"}, 404)
            result = scenes.apply_tree_order(scene_id, tree)
            return _json({"State": 0, "Message": "排序已保存", "Data": result})
        except (ValueError, TypeError, AttributeError, json.JSONDecodeError) as exc:
            return _json({"State": 1, "Message": str(exc)}, 422)

    @api.post("/api/scene-poi-layers")
    async def add_scene_poi_layer(request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None or spatial_store is None:
            return _json({"error": "场景或空间存储不可用"}, 503)
        try:
            body = await request.json()
            scene_id = str(uuid.UUID(str(body.get("scene_id"))))
            dataset_id = str(uuid.UUID(str(body.get("dataset_id"))))
            bbox = body.get("bbox")
            name = str(body.get("name") or "").strip()
            if not isinstance(bbox, list) or len(bbox) != 4 or any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in bbox):
                raise ValueError("bbox 必须为四个经纬度数值")
            west, south, east, north = (float(x) for x in bbox)
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("bbox 范围无效")
            if not name or len(name) > 40:
                raise ValueError("名称必须为 1 到 40 字")
            if type(body.get("default_visible", False)) is not bool:
                raise ValueError("default_visible 必须是布尔值")
            if not scene_exists(scene_id):
                return _json({"error": "场景不存在"}, 404)
            spatial_store.ensure_schema()
            with spatial_store.connect() as connection:
                dataset = connection.execute("SELECT shape_type,bounds FROM spatial_dataset WHERE id=%s", (dataset_id,)).fetchone()
            if dataset is None or int(dataset["shape_type"]) != 1:
                return _json({"error": "点数据集不存在"}, 404)
            geometry = {"type": "Polygon", "coordinates": [[[west, south], [east, south],
                         [east, north], [west, north], [west, south]]]}
            existing = next((item for item in scenes.layers(scene_id)
                             if item["source_kind"] == "spatial_dataset" and item["source_id"] == dataset_id
                             and item["layer_category"] == "poi"
                             and scenes.same_rectangle_scope(item["scope_geometry"], [west, south, east, north])
                             and item["filter_properties"] is None), None)
            if existing:
                return _json({"id": existing["layer_id"], "layer_id": existing["layer_id"],
                              "scene_id": scene_id, "dataset_id": dataset_id,
                              "bbox": [west, south, east, north], "name": existing["display_name"]})
            layer, _ = scenes.add_layer({"scene_id": scene_id, "layer_category": "poi",
                "category_label": category_labels["poi"], "source_kind": "spatial_dataset",
                "source_id": dataset_id, "scope_geometry": geometry, "scope_crs": "EPSG:4326",
                "display_name": name, "default_visible": bool(body.get("default_visible", False))})
            return _json({"id": layer["layer_id"], "layer_id": layer["layer_id"],
                          "scene_id": scene_id, "dataset_id": dataset_id,
                          "bbox": [west, south, east, north], "name": name}, 201)
        except (ValueError, TypeError, KeyError) as exc:
            return _json({"error": str(exc)}, 422)
        except (sqlite3.Error, OSError) as exc:
            return _json({"error": str(exc)}, 503)

    @api.delete("/api/scene-poi-layers/{layer_id}")
    async def delete_scene_poi_layer(layer_id: str, request: Request, scene_id: str):
        _, error = admin_required(request)
        if error is not None:
            return error
        try:
            layer_id = str(uuid.UUID(layer_id))
            scene_id = str(uuid.UUID(scene_id))
        except ValueError:
            return _json({"error": "图层或场景 ID 无效"}, 422)
        if scenes is None or not scenes.remove_poi_layer(scene_id, layer_id):
            return _json({"error": "图层不存在"}, 404)
        return _json({"removed": True})

    @api.post("/api/scene-video-groups")
    async def add_scene_video_group(request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None or spatial_store is None:
            return _json({"error": "场景或视频存储不可用"}, 503)
        try:
            body = await request.json()
            scene_id = str(uuid.UUID(str(body.get("scene_id"))))
            group_id = str(uuid.UUID(str(body.get("group_id"))))
            if type(body.get("default_visible", False)) is not bool:
                raise ValueError("default_visible 必须是布尔值")
            if not scene_exists(scene_id):
                return _json({"error": "场景不存在"}, 404)
            with spatial_store.connect() as connection:
                row = connection.execute(
                    "SELECT name FROM video_group WHERE id=%s AND status='published'",
                    (group_id,),
                ).fetchone()
            if row is None:
                return _json({"error": "已发布视频资料组不存在"}, 404)
            existing = next((item for item in scenes.layers(scene_id)
                             if item["source_kind"] == "video_group" and item["source_id"] == group_id), None)
            if existing:
                return _json({"scene_id": scene_id, "group_id": group_id,
                              "layer_id": existing["layer_id"], "name": existing["display_name"]})
            layer, _ = scenes.add_layer({"scene_id": scene_id, "layer_category": "video",
                "category_label": category_labels["video"], "source_kind": "video_group",
                "source_id": group_id, "display_name": row["name"],
                "default_visible": bool(body.get("default_visible", False))})
            return _json({"scene_id": scene_id, "group_id": group_id,
                          "layer_id": layer["layer_id"], "name": row["name"]}, 201)
        except (ValueError, TypeError, AttributeError) as exc:
            return _json({"error": str(exc)}, 422)
        except (sqlite3.Error, OSError) as exc:
            return _json({"error": str(exc)}, 503)

    @api.delete("/api/scene-video-groups/{group_id}")
    async def delete_scene_video_group(group_id: str, request: Request, scene_id: str):
        _, error = admin_required(request)
        if error is not None:
            return error
        try:
            scene_id = str(uuid.UUID(scene_id))
            group_id = str(uuid.UUID(group_id))
        except ValueError:
            return _json({"error": "场景或视频资料组 ID 无效"}, 422)
        if scenes is None or not scenes.remove_video_group(scene_id, group_id):
            return _json({"error": "场景视频关联不存在"}, 404)
        return _json({"removed": True})

    async def add_scene_resources(request: Request, *, custom: bool = False) -> Response:
        user, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        raw = await request.body()
        if len(raw) > 1024 * 1024:
            return _json({"State": 413, "Message": "请求体过大"}, 413)
        try:
            payload = json.loads(raw.decode("utf-8-sig")) if raw else None
        except (UnicodeError, json.JSONDecodeError):
            return _json({"State": 400, "Message": "请求体不合法"}, 400)
        if custom:
            if not isinstance(payload, dict) or not isinstance(payload.get("SceneResourceList"), list):
                return _json({"State": 1, "Message": "场景资源参数不合法"})
            items = payload["SceneResourceList"]
            custom_tree_id = str(payload.get("SceneTreesId") or payload.get("ArSceneTreesId") or "") or None
        else:
            if not isinstance(payload, list):
                return _json({"State": 1, "Message": "场景资源参数应为数组"})
            items = payload
            custom_tree_id = None
        if len(items) > 500:
            return _json({"State": 1, "Message": "单次最多关联500个资源"})
        resources_by_id = {str(row.get("Id")): row for row in store.resources}
        normalized = []
        scene_id = None
        for item in items:
            if not isinstance(item, dict):
                return _json({"State": 1, "Message": "场景资源参数不合法"})
            candidate_scene_id = str(item.get("ArSceneId") or "").strip()
            if not candidate_scene_id or (scene_id is not None and candidate_scene_id != scene_id):
                return _json({"State": 1, "Message": "每批资源必须关联到同一场景"})
            scene_id = candidate_scene_id
            resource_id = str(item.get("ResourceId") or "").strip()
            resource = resources_by_id.get(resource_id)
            if resource is None or resource.get("IsDeleted"):
                return _json({"State": 1, "Message": f"资源不存在或不可用：{resource_id}"})
            try:
                resource_type = int(resource.get("Type"))
            except (TypeError, ValueError):
                return _json({"State": 1, "Message": f"资源类型无效：{resource_id}"})
            if item.get("Type") is not None:
                try:
                    if int(item["Type"]) != resource_type:
                        return _json({"State": 1, "Message": f"资源类型与资源目录不匹配：{resource_id}"})
                except (TypeError, ValueError):
                    return _json({"State": 1, "Message": f"资源类型无效：{resource_id}"})
            normalized.append({
                "ResourceId": resource_id, "Type": resource_type,
                "Label": str(item.get("Label") or resource.get("Label") or resource.get("Name") or ""),
                "MetaData": item.get("MetaData") if item.get("MetaData") is not None else resource.get("MetaData"),
                "RelateResourceId": item.get("RelateResourceId"),
                "ExtendedAttributes": item.get("ExtendedAttributes"),
                "OsgbId": item.get("OsgbId"), "SceneTreesId": custom_tree_id,
            })
        if not normalized:
            return _json({"State": 1, "Message": "请选择要关联的资源"})
        if scenes.get(scene_id) is None:
            return _json({"State": 404, "Message": "场景不存在"}, 404)
        local_scene = scenes.get(scene_id)
        if custom_tree_id and not scenes.has_folder(scene_id, custom_tree_id):
            return _json({"State": 1, "Message": "目标资源目录不存在"})
        try:
            await asyncio.to_thread(scenes.add_resources, scene_id, normalized)
        except (sqlite3.Error, ValueError):
            return _json({"State": 503, "Message": "场景资源保存失败"}, 503)
        tree = append_scene_data_nodes(local_scene_tree(scene_id), scene_id)
        return _json({"State": 0, "Message": "关联成功", "Data": tree})

    @api.post("/api/cim/CIMScene/CIMSceneResourceRelevance")
    async def cim_scene_resource_relevance(request: Request):
        return await add_scene_resources(request)

    @api.post("/api/ArScene/ArSceneResourceRelevance")
    async def ar_scene_resource_relevance(request: Request):
        return await add_scene_resources(request)

    @api.post("/api/cim/CIMScene/AddCIMSceneResourceByCustomType")
    async def add_cim_scene_resource_custom(request: Request):
        return await add_scene_resources(request, custom=True)

    @api.post("/api/ArScene/AddArSceneResourceByCustomType")
    async def add_ar_scene_resource_custom(request: Request):
        return await add_scene_resources(request, custom=True)

    @api.post("/api/ArSceneTrees/Add")
    async def add_scene_tree_folder(request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        scene_id = str(body.get("ArSceneId") or "")
        label = str(body.get("Label") or "").strip()
        parent_id = str(body.get("ParentId") or "").strip() or None
        scene = scenes.get(scene_id)
        if scene is None:
            return _json({"State": 404, "Message": "场景不存在"}, 404)
        if not 1 <= len(label) <= 30:
            return _json({"State": 1, "Message": "目录名称长度应为1至30个字符"})
        try:
            folder = await asyncio.to_thread(scenes.create_folder, scene_id, label, parent_id)
        except ValueError:
            return _json({"State": 1, "Message": "父级目录不存在"})
        return _json({"State": 0, "Message": "创建成功", "Data": folder})

    @api.post("/api/ArSceneTrees/Edit")
    async def edit_scene_tree_folder(request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景存储未配置"}, 503)
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        folder_id = str(body.get("Id") or "")
        label = str(body.get("Label") or "").strip()
        if not 1 <= len(label) <= 30:
            return _json({"State": 1, "Message": "目录名称长度应为1至30个字符"})
        scene_id = ""
        for scene in scenes.rows():
            if scenes.has_folder(scene["Id"], folder_id):
                scene_id = scene["Id"]
                break
        if not scene_id or not await asyncio.to_thread(scenes.edit_folder, scene_id, folder_id, label):
            return _json({"State": 404, "Message": "目录不存在"}, 404)
        return _json({"State": 0, "Message": "修改成功", "Data": True})

    @api.delete("/api/ArSceneTrees/DeleteBySceneIdAndTreesId/{scene_id}/{folder_id}")
    async def delete_scene_tree_folder(scene_id: str, folder_id: str, request: Request):
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None or scenes.get(scene_id) is None:
            return _json({"State": 404, "Message": "场景不存在"}, 404)
        deleted = await asyncio.to_thread(scenes.delete_folder, scene_id, folder_id)
        return _json({"State": 0, "Message": "删除成功", "Data": deleted})

    @api.get("/api/ArSceneTrees/GetResourceIdListBySceneIdAndTreesId/{scene_id}/{folder_id}")
    async def get_custom_folder_resource_ids(scene_id: str, folder_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        if scenes is None or not scenes.has_folder(scene_id, folder_id):
            return _json([])
        return _json([
            row["resource_id"] for row in scenes.resource_rows(scene_id)
            if row.get("custom_tree_id") == folder_id
        ])

    @api.get("/api/cim/CIMScene/GetResourceByCIMScene/{scene_id}")
    @api.get("/api/ArScene/GetResourceByArScene/{scene_id}")
    async def get_scene_resource_ids(scene_id: str, request: Request):
        if account_user(request) is None:
            return unauthorized()
        if scenes is not None and scenes.get(scene_id) is not None:
            return _json(scenes.resource_ids(scene_id))
        return Response(status_code=404)

    async def remove_scene_resource(scene_id: str, resource_id: str, request: Request) -> Response:
        _, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None or scenes.get(scene_id) is None:
            return _json({"State": 404, "Message": "场景不存在"}, 404)
        removed = await asyncio.to_thread(scenes.remove_resource, scene_id, resource_id)
        if not removed:
            return _json({"State": 1, "Message": "场景中不存在该资源"})
        return _json({"State": 0, "Message": "删除成功", "Data": True})

    @api.delete("/api/cim/CIMScene/DeleteCIMSceneTreeLayer/{scene_id}/{resource_id}")
    async def delete_cim_scene_resource(scene_id: str, resource_id: str, request: Request):
        return await remove_scene_resource(scene_id, resource_id, request)

    @api.delete("/api/ArScene/DeleteTreeLayer/{scene_id}/{resource_id}")
    async def delete_ar_scene_resource(scene_id: str, resource_id: str, request: Request):
        return await remove_scene_resource(scene_id, resource_id, request)

    @api.get("/api/ArScene/GetResourceTreeForSceneWithVisitor/{scene_id}")
    async def visitor_scene_tree(scene_id: str, request: Request):
        return Response(status_code=404)

    @api.get("/api/ArScene/GetResourceTreeForSceneWithShare/{share_key}")
    async def shared_scene_tree(share_key: str, request: Request):
        return Response(status_code=404)

    @api.api_route("/geoserver", methods=["GET"])
    @api.api_route("/geoserver/{legacy_path:path}", methods=["GET"])
    async def cached_geoserver_read(request: Request, legacy_path: str = ""):
        if cache is None:
            return Response(status_code=404)
        raw_path = request.scope.get("raw_path", b"").decode("ascii")
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        target = raw_path + ("?" + raw_query if raw_query else "")
        mirrored = await asyncio.to_thread(cache.get_cached, "GET", target)
        return mirrored.response()

    @api.get("/api/cim/CIMScene/GetCIMSceneResourceBigTypeList")
    async def resource_big_types(request: Request):
        if not authorized(request):
            return unauthorized()
        if store.big_types is not None:
            return _json(store.big_types)
        labels = {1: "地形", 2: "矢量", 4: "倾斜", 10: "视频", 12: "栅格服务", 13: "点云"}
        present = {row["BigResourceType"] for row in store.resources}
        return _json([{"Key": key, "Value": value} for key, value in labels.items() if key in present])

    @api.post("/api/cim/CIMScene/GetCIMSceneResourceTypeList")
    async def resource_types(request: Request):
        if not authorized(request):
            return unauthorized()
        raw = await request.body()
        if raw and "json" not in request.headers.get("content-type", "").lower():
            return _json({"State": 415, "Message": "请求体不合法"}, 415)
        try:
            big_types = json.loads(raw.decode("utf-8-sig")) if raw else []
            if not isinstance(big_types, list):
                raise ValueError("expected array")
            selected = {int(value) for value in big_types}
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            return _json({"State": 1, "Message": "资源类型参数不合法"})
        if selected and cache is not None:
            mirrored = await asyncio.to_thread(
                cache.get_cached, "POST",
                "/api/cim/CIMScene/GetCIMSceneResourceTypeList",
                raw, request.headers.get("content-type", ""),
            )
            if mirrored.status == 200:
                return mirrored.response()
        if store.resource_types is not None:
            if selected:
                type_to_big = {row["Type"]: row["BigResourceType"] for row in store.resources}
                return _json([row for row in store.resource_types if type_to_big.get(row["Key"]) in selected])
            return _json(store.resource_types)
        types = {}
        for row in store.resources:
            if not selected or row["BigResourceType"] in selected:
                types[row["Type"]] = row["TypeName"]
        return _json([{"Key": key, "Value": types[key]} for key in sorted(types)])

    @api.get("/api/ArScene/GetDateSetDropdownList")
    async def dataset_dropdown(request: Request):
        if not authorized(request):
            return unauthorized()
        return _json(store.dataset_dropdown if store.dataset_dropdown is not None else store.datasets)

    @api.post("/api/Login/Login")
    async def login(request: Request):
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        user = await asyncio.to_thread(
            authenticate_user, auth["user_db_path"], str(body.get("UserName") or ""),
            str(body.get("Password") or ""),
        )
        if user is None:
            return _json({"State": 1, "Message": "账号或密码错误"})
        token = make_jwt(auth["jwt_secret"], user["id"])
        create_session(auth["user_db_path"], user["id"], token, int(time.time()) + 259200)
        return _json({
            "State": 0, "Message": "登录成功",
            "Data": {"Token": token, "UserName": user["username"],
                     "RealName": user["real_name"], "AuthCodes": user["auth_codes"], "Role": user["role"]},
        })

    @api.post("/api/Login/Logout")
    async def logout(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        user_id = jwt_user_id(token, auth["jwt_secret"]) if token else None
        # Allow a repeated logout to return success while the signed token remains unexpired.
        if user_id is None or get_user_by_id(auth["user_db_path"], user_id) is None:
            return unauthorized()
        revoke_session(auth["user_db_path"], user_id, token)
        return _json({"State": 0, "Message": "退出成功"})

    @api.post("/api/Login/Register")
    @api.post("/api/Register/Register")
    async def register(request: Request):
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        try:
            user = register_user(
                auth["user_db_path"], str(body.get("UserName") or ""),
                str(body.get("RealName") or ""), str(body.get("Password") or ""),
            )
        except ValueError as error:
            return _json({"State": 1, "Message": str(error)})
        return _json({
            "State": 0, "Message": "注册成功",
            "Data": {"UserName": user["username"], "RealName": user["real_name"],
                     "AuthCodes": [], "Role": "reader"},
        })

    @api.get("/api/User/GetByToken")
    async def get_user_by_token(request: Request):
        header = request.headers.get("authorization", "")
        user = authenticated_user(auth, header[7:].strip()) if header.startswith("Bearer ") else None
        if user is None:
            return unauthorized()
        return _json({
            "State": 0, "Message": "获取成功",
            "Data": {"Id": user["id"], "UserName": user["username"], "RealName": user["real_name"],
                     "AuthCodes": user["auth_codes"], "Role": user["role"]},
        })

    @api.post("/api/cim/CIMScene/GetCIMSceneResourceByPage")
    @api.post("/api/ArScene/GetSceneResourceByPage")
    async def resource_page(request: Request):
        if not authorized(request):
            return unauthorized()
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        rows = store.resources
        try:
            resource_type = int(body.get("Type") or 0)
            page_index = max(1, int(body.get("PageIndex") or 1))
            page_size = max(0, int(body.get("PageSize", 20)))
            big_types = body.get("BigResourceType") or []
            datasets = body.get("DataSetIds") or []
            if not isinstance(big_types, list) or not isinstance(datasets, list):
                raise ValueError("filters must be arrays")
        except (TypeError, ValueError):
            return _json({"State": 1, "Message": "分页参数不合法"})
        if resource_type:
            rows = [row for row in rows if row.get("Type") == resource_type]
        if big_types:
            rows = [row for row in rows if row.get("BigResourceType") in big_types]
        if datasets:
            rows = [row for row in rows if row.get("DataSetId") in datasets]
        label = str(body.get("Label") or "").strip()
        if label:
            rows = [row for row in rows if label in (row.get("Label") or "")]
        for field, comparison in (("StartTime", lambda value, bound: value >= bound), ("EndTime", lambda value, bound: value <= bound)):
            bound = body.get(field)
            if bound:
                rows = [row for row in rows if row.get("UploadTime") and comparison(row["UploadTime"], bound)]
        start = (page_index - 1) * page_size
        return _json({"Total": len(rows), "Rows": rows[start:start + page_size]})

    @api.post("/spatial/resource-subsets/preview")
    async def preview_resource_subset(request: Request):
        if account_user(request) is None:
            return unauthorized()
        if subsets is None:
            return _json({"detail": "区域子资源功能未配置"}, 503)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"detail": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            return _json(await asyncio.to_thread(subsets.preview, str(body.get("source_resource_id") or ""),
                                                 body.get("geometry"), str(body.get("crs") or "EPSG:4326")))
        except SubsetError as exc:
            return _json({"detail": str(exc), "code": "unsupported_source"}, 422)
        except (sqlite3.Error, OSError):
            return _json({"detail": "区域预览暂不可用"}, 503)

    @api.post("/spatial/oblique-image-subsets/preview")
    async def preview_oblique_image_subset(request: Request):
        if account_user(request) is None:
            return unauthorized()
        if oblique_subsets is None:
            return _json({"detail": "倾斜影像区域预览未配置"}, 503)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"detail": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            return _json(await asyncio.to_thread(oblique_subsets.preview,
                str(body.get("source_resource_id") or ""), body.get("geometry"),
                str(body.get("crs") or "EPSG:4326")))
        except SubsetError as exc:
            return _json({"detail": str(exc), "code": "unsupported_source"}, 422)
        except (sqlite3.Error, OSError):
            return _json({"detail": "倾斜影像区域预览暂不可用"}, 503)

    @api.post("/spatial/resource-subsets", status_code=201)
    async def create_resource_subset(request: Request):
        user, error = admin_required(request)
        if error is not None:
            return error
        if subsets is None:
            return _json({"detail": "区域子资源功能未配置"}, 503)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"detail": "请求体必须是 JSON 对象"}, status if body is None else 400)
        try:
            result = await asyncio.to_thread(
                subsets.create,
                str(body.get("source_resource_id") or ""), body.get("geometry"),
                str(body.get("crs") or "EPSG:4326"),
                str(body.get("mode") or "intersecting_tiles"),
                str(body.get("name") or "区域子资源"), user["id"],
            )
            resource = result.get("resource")
            if isinstance(resource, dict) and not any(str(row.get("Id")) == str(resource.get("Id")) for row in store.resources):
                store.resources.append(resource)
            result.pop("resource", None)
            return _json(result, 200 if result.get("reused_existing") else 201)
        except SubsetError as exc:
            return _json({"detail": str(exc), "code": "unsupported_source"}, 422)
        except (sqlite3.Error, OSError):
            return _json({"detail": "无法持久化区域子资源"}, 503)

    @api.get("/spatial/resource-subsets/{resource_id}")
    async def get_resource_subset(resource_id: str, request: Request):
        if not authorized(request):
            return unauthorized()
        if subsets is None:
            return _json({"detail": "区域子资源功能未配置"}, 503)
        try:
            subset = await asyncio.to_thread(subsets.get, resource_id)
        except (sqlite3.Error, OSError):
            return _json({"detail": "区域子资源暂时不可用"}, 503)
        if subset is None:
            return _json({"detail": "区域子资源不存在"}, 404)
        result = LegacySubsetStore._creation_result(subset, reused=True)
        result.update({"geometry": subset["geometry"], "source_version": subset["source_version"],
                       "created_at": subset["created_at"]})
        result.pop("resource", None)
        return _json(result)

    @api.post("/spatial/oblique-image-subsets", status_code=201)
    async def create_oblique_image_subset(request: Request):
        user, error = admin_required(request)
        if error is not None:
            return error
        if oblique_subsets is None:
            return _json({"detail": "倾斜影像区域子资源功能未配置"}, 503)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"detail": "请求体必须是 JSON 对象"}, status if body is None else 400)
        if body.get("mode", "camera_centers_inside") != "camera_centers_inside":
            return _json({"detail": "仅支持 camera_centers_inside 筛选模式"}, 422)
        try:
            result = await asyncio.to_thread(oblique_subsets.create,
                str(body.get("source_resource_id") or ""), body.get("geometry"),
                str(body.get("crs") or "EPSG:4326"), str(body.get("name") or "倾斜影像区域子资源"), user["id"])
            resource = result.pop("resource")
            if not any(str(row.get("Id")) == resource["Id"] for row in store.resources):
                store.resources.append(resource)
            result["status"] = "completed"
            result["precision_notice"] = "按相机位置筛选照片；相机在范围外但拍到范围内的照片不会入选。"
            return _json(result, 200 if result["reused_existing"] else 201)
        except SubsetError as exc:
            return _json({"detail": str(exc)}, 422)
        except (sqlite3.Error, OSError):
            return _json({"detail": "无法持久化倾斜影像区域子资源"}, 503)

    @api.get("/spatial/oblique-image-subsets/{resource_id}")
    async def get_oblique_image_subset(resource_id: str, request: Request):
        if not authorized(request):
            return unauthorized()
        if oblique_subsets is None:
            return _json({"detail": "倾斜影像区域子资源功能未配置"}, 503)
        result = await asyncio.to_thread(oblique_subsets.get, resource_id)
        if result is None:
            return _json({"detail": "倾斜影像区域子资源不存在"}, 404)
        result.pop("resource", None)
        return _json(result)

    @api.put("/api/cim/CIMScene/EditEffectsConfig")
    async def edit_effects_config(request: Request):
        user, error = admin_required(request)
        if error is not None:
            return error
        if scenes is None:
            return _json({"State": 503, "Message": "场景配置功能未启用"}, 503)
        body, status = await _body(request)
        if body is None or not isinstance(body, dict):
            return _json({"State": 400, "Message": "请求体必须是 JSON 对象"}, status if body is None else 400)
        scene_id = str(body.get("SceneId") or "")
        effects = body.get("EffectsConfig")
        if not isinstance(effects, dict) or len(json.dumps(effects, ensure_ascii=False)) > 65536:
            return _json({"State": 400, "Message": "EffectsConfig 必须是配置对象且不超过 64 KiB"}, 400)
        required = {"lightConfig", "windConfig", "rainConfig", "snowConfig", "fogConfig", "atmosphereConfig"}
        if not required.issubset(effects) or any(not isinstance(effects[key], dict) for key in required):
            return _json({"State": 400, "Message": "环境特效配置不完整"}, 400)
        if not await asyncio.to_thread(scenes.update_effects_config, scene_id, effects, user["id"]):
            return _json({"State": 404, "Message": "场景不存在或不支持修改"}, 404)
        return _json({"State": 0, "Message": "环境特效配置已保存"})

    @api.post("/api/Resource/GetByPage")
    async def lite_page(request: Request):
        if not authorized(request):
            return unauthorized()
        body, status = await _body(request)
        if body is None:
            return _json({"State": status, "Message": "请求体不合法"}, status)
        if store.lite_snapshot is None and cache is not None:
            raw_path = request.scope.get("raw_path", b"/api/Resource/GetByPage").decode("ascii")
            mirrored = await asyncio.to_thread(
                cache.get_cached, "POST", raw_path, await request.body(),
                request.headers.get("content-type", ""),
            )
            if mirrored.status == 200:
                return mirrored.response()
        try:
            page_index = max(1, int(body.get("PageIndex") or 1))
            page_size = max(0, int(body.get("PageSize", 20)))
        except (TypeError, ValueError):
            return _json({"State": 1, "Message": "分页参数不合法"})
        rows = store.lite_resources()
        start = (page_index - 1) * page_size
        return _json({"Total": len(rows), "Rows": rows[start:start + page_size]})

    @api.get("/api/DownLoad/GetFileStream")
    async def file_stream(request: Request):
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        key = unquote(raw_query)
        path = store.path_of(key)
        if path is None:
            if cache is not None:
                target = request.scope.get("raw_path", b"/api/DownLoad/GetFileStream").decode("ascii")
                target += "?" + raw_query
                mirrored = await asyncio.to_thread(cache.get_cached, "GET", target)
                return mirrored.response()
            return Response(status_code=204)

        def chunks():
            with path.open("rb") as stream:
                while data := stream.read(1024 * 1024):
                    yield data

        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        return StreamingResponse(chunks(), media_type=content_type, headers={"Content-Length": str(path.stat().st_size)})

    @api.get("/api/ARLayer/GetNumARNote/{resource_id}")
    async def arlayer_photo_count(resource_id: str):
        if oblique_subsets is not None:
            count = await asyncio.to_thread(oblique_subsets.count, resource_id)
            if count is not None:
                return _json(count)
        if store.arlayer_db is None:
            return Response(status_code=404)
        with sqlite3.connect(store.arlayer_db) as db:
            row = db.execute(
                "SELECT remote_total FROM arlayer_resource WHERE resource_id=? AND complete=1",
                (resource_id,),
            ).fetchone()
        return _json(row[0] if row is not None else 0)

    @api.get("/api/ARLayer/FetchARNote")
    async def arlayer_photo_page(request: Request):
        if store.arlayer_db is None:
            return Response(status_code=404)
        resource_id = request.query_params.get("arlayerUrl", "")
        try:
            start = int(request.query_params.get("startPos", "0"))
            amount = int(request.query_params.get("num", "0"))
            if start < 0 or amount < 0 or amount > 20000:
                raise ValueError
        except ValueError:
            return Response(status_code=400)
        if oblique_subsets is not None:
            page = await asyncio.to_thread(oblique_subsets.page, resource_id, start, amount)
            if page is not None:
                return _json(page)
        with sqlite3.connect(store.arlayer_db) as db:
            records = db.execute(
                "SELECT record_json FROM arlayer_photo WHERE resource_id=? "
                "ORDER BY ordinal LIMIT ? OFFSET ?", (resource_id, amount, start),
            ).fetchall()
        return _json([json.loads(row[0]) for row in records])

    @api.get("/api/ARLayer/GetImageFileByImageId/{image_id}")
    async def arlayer_photo_file(image_id: str):
        if store.arlayer_db is None:
            return Response(status_code=404)
        with sqlite3.connect(store.arlayer_db) as db:
            row = db.execute(
                "SELECT fastdfs_id FROM arlayer_photo WHERE image_id=?", (image_id,),
            ).fetchone()
        if row is None or not row[0]:
            return Response(status_code=404)
        key = row[0]
        path = store.path_of(key)
        if path is None and cache is not None:
            mirrored = await asyncio.to_thread(cache.get_cached, "GET", "/api/DownLoad/GetFileStream?" + key)
            path = mirrored.file if mirrored.status == 200 else None
        if path is None:
            return Response(status_code=204)
        return FileResponse(path, media_type="application/octet-stream")

    @api.get("/api/Video/VideoFile")
    async def video_file(request: Request):
        if cache is None:
            return Response(status_code=204)
        target = "/api/Video/VideoFile?" + request.scope.get("query_string", b"").decode("latin-1")
        mirrored = await asyncio.to_thread(cache.get_cached, "GET", target)
        return mirrored.response() if mirrored.status == 200 else Response(status_code=204)

    for route in ("User", "Role", "Organization"):
        async def empty_page(request: Request):
            if not authorized(request):
                return unauthorized()
            body, status = await _body(request)
            if body is None:
                return _json({"State": status, "Message": "请求体不合法"}, status)
            return _json({"Total": 0, "Rows": None})
        api.add_api_route(f"/api/{route}/GetByPage", empty_page, methods=["POST"], name=f"{route}GetByPage")

    @api.get("/api/Layer/GetFile")
    async def layer_file(request: Request):
        if not authorized(request):
            return unauthorized()
        return Response(status_code=200, content=b"")

    async def legacy_tiles_file(request: Request, resource_id: str, relative_path: str, resource_type: int):
        if subsets is not None:
            derived = await asyncio.to_thread(subsets.file_response, resource_id, relative_path)
            if derived is not None:
                return derived
        if pointcloud_bridge is not None:
            bridged = await asyncio.to_thread(
                pointcloud_bridge.file_response, resource_id, relative_path, resource_type,
            )
            if bridged is not None:
                return bridged
        if cache is None:
            return Response(status_code=404)
        raw_path = request.scope.get("raw_path", b"").decode("ascii")
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        target = raw_path + ("?" + raw_query if raw_query else "")
        mirrored = await asyncio.to_thread(cache.get_cached, "GET", target)
        return mirrored.response()

    @api.get("/api/Mesh/GetMesh3DTilesFile/{resource_id}/{relative_path:path}")
    async def mesh_tiles_file(resource_id: str, relative_path: str, request: Request):
        return await legacy_tiles_file(request, resource_id, relative_path, 1000012)

    @api.get("/api/PointCload/GetPointCloud3DTilesFile/{resource_id}/{relative_path:path}")
    async def pointcloud_tiles_file(resource_id: str, relative_path: str, request: Request):
        return await legacy_tiles_file(request, resource_id, relative_path, 13000006)

    @api.api_route("/api/{legacy_path:path}", methods=["GET", "POST"])
    async def cached_read_only(request: Request, legacy_path: str):
        raw_path = request.scope.get("raw_path", b"").decode("ascii")
        if raw_path.startswith((
            "/api/cim/CIMScene/GetResourceTreeForCIMSceneWithShare/",
            "/api/cim/CIMScene/GetResourceTreeForCIMLargeScreenScene/",
            "/api/ArScene/GetArSceneTools/",
            "/api/ArScene/GetArSceneToolsWithVisitor/",
            "/api/ArScene/GetArSceneToolsWithShare/",
            "/api/ArScene/GetShortcutTools/",
            "/api/ArScene/GetShortcutToolsWithVisitor/",
            "/api/ArScene/GetShortcutToolsWithShare/",
            "/api/ArScene/GetArSceneForWebAR/",
        )):
            return Response(status_code=404)
        public_reads = (
            "/api/Mesh/GetMesh3DTilesFile/", "/api/Raster/GetTmsFile/",
            "/api/Raster/GetTSMCTBFile/", "/api/VectorTiles/GetFile",
            "/api/PointCload/GetPointCloud3DTilesFile/",
            "/api/EntityModel/GetGlbFile/", "/api/BIMData/GetBIM3DTilesFile/",
            "/api/Video/GetDirectionalInfoList/", "/api/Video/GetInitDirectionalInfoList/",
            "/api/Video/GetLiveMediaDirectionalInfo/", "/api/Video/FetchVideoDirectionalInfo/",
        )
        is_public_read = request.method == "GET" and any(
            raw_path == prefix.rstrip("/")
            or raw_path.startswith(prefix if prefix.endswith("/") else prefix + "/")
            for prefix in public_reads
        )
        if not is_public_read and not authorized(request):
            return unauthorized()
        if cache is None:
            return Response(status_code=404)
        segments = legacy_path.split("/")
        if not is_public_read and not any(segment.startswith(("Get", "Query", "Check")) for segment in segments[1:]):
            return Response(status_code=405)
        raw_query = request.scope.get("query_string", b"").decode("latin-1")
        target = raw_path + ("?" + raw_query if raw_query else "")
        mirrored = await asyncio.to_thread(
            cache.get_cached, request.method, target, await request.body(),
            request.headers.get("content-type", ""),
        )
        return mirrored.response()

    return api
