"""Unified spatial reads over original, versioned resources."""

import base64
import copy
import json
import posixpath
import uuid
from pathlib import Path
from typing import Annotated, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from .legacy_subsets import SPATIAL_DATASET_QUERY_MAX_AREA_KM2, SubsetError, canonical_geometry
from .poi_identity import UNIFIED_POI_ID
from .resource_index import ensure_schema, file_path, safe_path


TILE_NOTICE = "按瓦片包围体相交筛选；边界瓦片可能含区域外内容，不执行几何裁切。"


class ResourceQuery(BaseModel):
    source_kind: str
    source_resource_id: str
    source_version_id: str | None = None
    geometry: dict
    crs: str = "EPSG:4326"
    selection_mode: str
    limit: int = Field(default=200, ge=1, le=1000)
    cursor: str | None = None


def _problem(code: str, detail: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"code": code, "detail": detail}, status_code=status)


def _cursor_encode(version: str, key: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"v": version, "k": key},
        separators=(",", ":")).encode()).rstrip(b"=").decode()


def _cursor_decode(raw: str | None, version: str) -> str:
    if raw is None:
        return ""
    if len(raw) > 4096:
        raise ValueError("cursor is too large")
    try:
        item = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if not isinstance(item, dict) or item.get("v") != version or not isinstance(item.get("k"), str):
            raise ValueError
        return item["k"]
    except (UnicodeError, ValueError, TypeError):
        raise ValueError("cursor is invalid or belongs to another source version") from None


def _uuid(raw: str, name: str) -> str:
    try:
        return str(uuid.UUID(str(raw)))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a UUID") from None


def _geometry(raw: dict, crs: str, source_kind: str, source_id: str) -> str:
    value = canonical_geometry(raw, crs, maximum_area_km2=(
        SPATIAL_DATASET_QUERY_MAX_AREA_KM2 if source_kind == "spatial_dataset" else None
    ), unlimited_area=(source_kind == "spatial_dataset" and source_id == str(UNIFIED_POI_ID)))
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _entry(kind: str, source_id: str, version: str, path: str) -> str:
    return (f"/spatial/resources/{quote(source_id)}/versions/{quote(version)}/files/"
            f"{quote(path, safe='/')}")


def _scoped_content_uri(document_path: str, target: str, layer_id: str) -> str:
    """Keep child requests under the same URL prefix as the Tileset document."""
    if document_path == "tileset.json":
        relative = f"files/{target}"
    else:
        relative = posixpath.relpath(target, posixpath.dirname(document_path) or ".")
    return f"{quote(relative, safe='/')}?scene_layer_id={quote(layer_id)}"


def _version(store, kind: str, source_id: str, requested: str | None) -> tuple[str, str, str]:
    """Return catalog kind and immutable version; never silently upgrade a pin."""
    if kind == "pointcloud_version":
        with store.connect() as db:
            row = db.execute("SELECT resource_id FROM pointcloud_version WHERE id=%s AND status='published'",
                             (source_id,)).fetchone()
        if row is None:
            raise LookupError("source_unavailable")
        requested = requested or source_id
        source_id = str(row["resource_id"])
        kind = "pointcloud_resource"
    if kind in {"pointcloud_resource", "legacy_resource"}:
        with store.connect() as db:
            row = db.execute("""SELECT id,resource_id FROM pointcloud_version
                WHERE resource_id=%s AND status='published' AND format='3dtiles'
                AND (%s::uuid IS NULL OR id=%s::uuid)
                ORDER BY published_at DESC NULLS LAST,created_at DESC,id DESC LIMIT 1""",
                (source_id, requested, requested)).fetchone()
        if row is None:
            if kind == "legacy_resource" and requested is None:
                raise LookupError("index_unavailable")
            raise LookupError("version_unavailable" if requested else "source_unavailable")
        return "pointcloud_resource", source_id, str(row["id"])
    if kind == "raster_collection":
        with store.connect() as db:
            row = db.execute("""SELECT acquisition_date::text AS version FROM raster_epoch
                WHERE collection_id=%s AND status='published'
                AND (%s::text IS NULL OR acquisition_date::text=%s)
                ORDER BY acquisition_date DESC LIMIT 1""",
                (source_id, requested, requested)).fetchone()
        if row is None:
            raise LookupError("version_unavailable" if requested else "source_unavailable")
        return kind, source_id, row["version"]
    if kind in {"spatial_dataset", "oblique_resource", "video_group"}:
        table = {"spatial_dataset": "spatial_dataset", "oblique_resource": "oblique_resource",
                 "video_group": "video_group"}[kind]
        with store.connect() as db:
            row = db.execute(f"SELECT id FROM {table} WHERE id=%s", (source_id,)).fetchone()
        if row is None:
            raise LookupError("source_unavailable")
        version = source_id  # These native catalogs currently have one published record per ID.
        if requested and requested != version:
            raise LookupError("version_unavailable")
        return kind, source_id, version
    raise LookupError("unsupported_source")


def run_query(store, body: ResourceQuery) -> dict | JSONResponse:
    ensure_schema(store)
    try:
        source_id = _uuid(body.source_resource_id, "source_resource_id")
        geom = _geometry(body.geometry, body.crs, body.source_kind, source_id)
        actual_kind, source_id, version = _version(store, body.source_kind, source_id, body.source_version_id)
        cursor = _cursor_decode(body.cursor, version)
    except (ValueError, SubsetError) as error:
        return _problem("invalid_query", str(error))
    except LookupError as error:
        code=str(error)
        return _problem(code, "published spatial index or pinned version is unavailable",
                        409 if code == "index_unavailable" else 404)
    if actual_kind in {"oblique_resource","video_group"}:
        with store.connect() as db:
            native_state=db.execute("""SELECT status,detail FROM resource_index_version
                WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s""",
                (actual_kind,source_id,version)).fetchone()
        if native_state is None or native_state["status"] != "ready":
            return _problem("index_unavailable",
                            native_state["detail"] if native_state else "published asset catalog is not ready",409)
    if actual_kind in {"pointcloud_resource", "raster_collection"}:
        if body.selection_mode != "intersecting_tiles":
            return _problem("unsupported_selection_mode", "tile resources require intersecting_tiles")
        with store.connect() as db:
            state = db.execute("""SELECT status,detail FROM resource_index_version
                WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s""",
                (actual_kind,source_id,version)).fetchone()
        if state is None or state["status"] != "ready":
            code = "index_unavailable" if state is None or state["status"] == "building" else state["status"]
            return _problem(code, state["detail"] if state and state["detail"] else "published file index is not ready", 409)
        with store.connect() as db:
            rows = db.execute("""SELECT relative_path,size_bytes,sha256,file_role,
                         raster_zoom,raster_x,raster_y,acquisition_date
                FROM resource_file WHERE source_kind=%s AND source_resource_id=%s
                  AND source_version_id=%s AND relative_path>%s
                  AND file_role IN ('tile_content','raster_tile')
                  AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                ORDER BY relative_path LIMIT %s""",
                (actual_kind,source_id,version,cursor,geom,geom,body.limit+1)).fetchall()
        has_more = len(rows) > body.limit
        rows = rows[:body.limit]
        items = [{"item_kind": row["file_role"], "relative_path": row["relative_path"],
                  "file_path": _entry(actual_kind, source_id, version, row["relative_path"]),
                  "size_bytes": row["size_bytes"], "sha256": row["sha256"],
                  "match_basis": "tileset_bounding_volume" if actual_kind == "pointcloud_resource" else "raster_tile_extent",
                  **({"z":row["raster_zoom"],"x":row["raster_x"],"y":row["raster_y"],
                      "acquisition_date":str(row["acquisition_date"])} if actual_kind == "raster_collection" else {})}
                 for row in rows]
        notice = TILE_NOTICE if actual_kind == "pointcloud_resource" else "按实际存在的栅格瓦片范围相交筛选；不进行像素裁切。"
        return {"source_kind":actual_kind,"source_resource_id":source_id,
                "source_version_id":version,"selection_mode":body.selection_mode,
                "items":items,"has_more":has_more,
                "next_cursor":_cursor_encode(version,rows[-1]["relative_path"]) if has_more else None,
                "precision_notice":notice}
    if actual_kind == "spatial_dataset":
        if body.selection_mode not in {"feature_intersects", "intersects"}:
            return _problem("unsupported_selection_mode", "vector datasets require feature_intersects")
        if cursor and not cursor.isdigit():
            return _problem("invalid_query", "feature cursor must contain a numeric ID")
        with store.connect() as db:
            dataset=db.execute("SELECT source_filename,shape_type,fields FROM spatial_dataset WHERE id=%s",(source_id,)).fetchone()
            names={field.get("name") for field in dataset["fields"] if isinstance(field,dict)} if dataset else set()
            poi=bool(dataset and dataset["shape_type"] in (1,11) and {"id","name","type","adcode"} <= names)
            supplement=bool(dataset and dataset["shape_type"] in (1,11) and {"POI_ID","NAME","TYPE","ADCODE"} <= names)
            consolidated=db.execute("SELECT EXISTS(SELECT 1 FROM poi_feature LIMIT 1) AS yes").fetchone()["yes"]
            if consolidated and (supplement or (poi and source_id != str(UNIFIED_POI_ID))):
                return _problem("consolidated_poi_source", "POI source dataset is consolidated; use /spatial/pois/query", 410)
            table="poi_feature" if consolidated and poi else "spatial_feature"
            identifier="feature_id" if table == "poi_feature" else "id"
            rows = db.execute(f"""SELECT {identifier} AS id,source_fid,properties,ST_AsGeoJSON(geom)::json AS geometry
                FROM {table} WHERE dataset_id=%s AND {identifier}>%s AND geom IS NOT NULL
                  AND geom && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND ST_Intersects(geom,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))
                ORDER BY {identifier} LIMIT %s""",
                (source_id,int(cursor or 0),geom,geom,body.limit+1)).fetchall()
        more = len(rows)>body.limit
        rows=rows[:body.limit]
        return {"source_kind":actual_kind,"source_resource_id":source_id,
                "source_version_id":version,"selection_mode":body.selection_mode,
                "items":[{"item_kind":"feature","feature_id":r["id"],"source_fid":r["source_fid"],
                          "properties":r["properties"],"geometry":r["geometry"],
                          "source_file_reference":dataset["source_filename"] if dataset else None,
                          "relative_path":None,"file_path":None,"match_basis":"feature_geometry"} for r in rows],
                "has_more":more,"next_cursor":_cursor_encode(version,str(rows[-1]["id"])) if more else None,
                "precision_notice":"按 PostGIS 要素几何相交筛选。"}
    if actual_kind == "oblique_resource":
        if body.selection_mode not in {"camera_centers_inside", "candidate_footprint_intersects"}:
            return _problem("unsupported_selection_mode", "ODM mode must specify camera centers or candidate footprint")
        column = "camera_center" if body.selection_mode == "camera_centers_inside" else "candidate_footprint"
        predicate = (f"ST_Contains(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),{column})"
                     if body.selection_mode == "camera_centers_inside" else
                     f"ST_Intersects({column},ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))")
        with store.connect() as db:
            coverage = db.execute(f"SELECT count(*) FILTER (WHERE {column} IS NULL) AS missing,count(*) AS total FROM oblique_image WHERE resource_id=%s",
                                  (source_id,)).fetchone()
            rows = db.execute(f"""SELECT id,source_photo_id,storage_key,size_bytes,sha256,file_status,coverage_status
                FROM oblique_image WHERE resource_id=%s AND id::text>%s AND {column} IS NOT NULL
                  AND {column} && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND {predicate}
                ORDER BY id LIMIT %s""",
                (source_id,cursor,geom,geom,body.limit+1)).fetchall()
        if coverage["total"] and coverage["missing"] == coverage["total"]:
            return _problem("extent_unavailable", "requested ODM spatial basis is unavailable", 409)
        more=len(rows)>body.limit
        rows=rows[:body.limit]
        items=[{"item_kind":"oblique_image","image_id":str(r["id"]),
                "source_photo_id":r["source_photo_id"],"relative_path":r["storage_key"],
                "file_path":f"/spatial/oblique-images/{r['id']}/file", "size_bytes":r["size_bytes"],
                "sha256":r["sha256"],"file_status":r["file_status"],
                "coverage_status":r["coverage_status"],"match_basis":body.selection_mode} for r in rows]
        return {"source_kind":actual_kind,"source_resource_id":source_id,
                "source_version_id":version,"selection_mode":body.selection_mode,
                "items":items,"has_more":more,
                "next_cursor":_cursor_encode(version,str(rows[-1]["id"])) if more else None,
                "precision_notice":f"{coverage['missing']} 张影像缺少该空间依据；候选足迹基于地面高度假设。"}
    if actual_kind == "video_group":
        if body.selection_mode not in {"footprint_intersects", "camera_center_inside"}:
            return _problem("unsupported_selection_mode", "video mode must specify footprint or camera center")
        column = "spatial_extent" if body.selection_mode == "footprint_intersects" else "camera_center"
        predicate=(f"ST_Contains(ST_SetSRID(ST_GeomFromGeoJSON(%s),4326),{column})"
                   if body.selection_mode == "camera_center_inside" else
                   f"ST_Intersects({column},ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))")
        with store.connect() as db:
            group = db.execute(f"""SELECT id,{column} IS NOT NULL AS located FROM video_group
                WHERE id=%s AND status='published'""",(source_id,)).fetchone()
            if group is None:
                return _problem("source_unavailable", "video group is not published",404)
            if not group["located"]:
                return _problem("extent_unavailable", "video group lacks the requested spatial basis",409)
            matched = db.execute(f"""SELECT {predicate} AS hit
                FROM video_group WHERE id=%s""",(geom,source_id)).fetchone()["hit"]
            rows = db.execute("""SELECT relative_path,role,size_bytes,sha256 FROM video_group_asset
                WHERE group_id=%s AND status='complete' AND relative_path>%s
                ORDER BY relative_path LIMIT %s""",(source_id,cursor,body.limit+1)).fetchall() if matched else []
        more=len(rows)>body.limit
        rows=rows[:body.limit]
        return {"source_kind":actual_kind,"source_resource_id":source_id,
                "source_version_id":version,"selection_mode":body.selection_mode,
                "items":[{"item_kind":"video_asset","relative_path":r["relative_path"],
                          "file_path":f"/videos/groups/{source_id}/files/{quote(r['relative_path'],safe='/')}",
                          "file_role":r["role"],"size_bytes":r["size_bytes"],"sha256":r["sha256"],
                          "match_basis":body.selection_mode} for r in rows],
                "has_more":more,"next_cursor":_cursor_encode(version,rows[-1]["relative_path"]) if more else None,
                "precision_notice":"视频按资料组筛选；当前没有逐帧相机轨迹。"}
    return _problem("unsupported_source", "source type is not indexed")


def router(store, account_identity: Callable, scenes=None) -> APIRouter:
    api = APIRouter(prefix="/spatial/resources", tags=["unified-spatial-resources"])

    @api.post("/query")
    def query_resources(body: ResourceQuery, _: Annotated[dict, Depends(account_identity)]):
        return run_query(store, body)

    def scoped_layer(layer_id: str, source_id: str, version_id: str, principal: dict):
        if scenes is None:
            raise HTTPException(503, {"code":"scene_unavailable"})
        try:
            layer_id = _uuid(layer_id, "scene_layer_id")
        except ValueError as error:
            raise HTTPException(422, {"code":"invalid_scene_layer","detail":str(error)}) from error
        layer = scenes.find_layer(layer_id)
        if (layer is None or layer["source_id"] != source_id or layer.get("source_version_id") != version_id
                or layer.get("scope_geometry") is None or layer.get("selection_mode") != "intersecting_tiles"):
            raise HTTPException(404, {"code":"scene_layer_unavailable"})
        # Scene read authorization follows the existing 7110 rule: authenticated
        # accounts may read published scenes; only admin may edit them.
        return layer

    def indexed_file(source_id: str, version_id: str, relative_path: str):
        try:
            source_id = _uuid(source_id,"source_id")
            relative_path = safe_path(relative_path)
        except ValueError as error:
            raise HTTPException(400,{"code":"invalid_path","detail":str(error)}) from error
        ensure_schema(store)
        with store.connect() as db:
            row = db.execute("""SELECT f.*,v.status AS index_status FROM resource_file f
                JOIN resource_index_version v USING(source_kind,source_resource_id,source_version_id)
                WHERE f.source_kind IN ('pointcloud_resource','raster_collection') AND f.source_resource_id=%s
                  AND f.source_version_id=%s AND f.relative_path=%s""",
                (source_id,version_id,relative_path)).fetchone()
        if row is None or row["index_status"] != "ready":
            raise HTTPException(404,{"code":"asset_unavailable"})
        path = file_path(store.data_dir / row["storage_namespace"], relative_path)
        if not path.is_file() or path.stat().st_size != row["size_bytes"]:
            raise HTTPException(503,{"code":"asset_unavailable","detail":"indexed file is missing or size differs"})
        return row,path

    def dynamic_document(source_id: str, version_id: str, document_path: str, layer: dict):
        row,path = indexed_file(source_id,version_id,document_path)
        if row["file_role"] != "tileset":
            raise HTTPException(404,{"code":"tileset_unavailable"})
        try:
            original=json.loads(path.read_text())
        except (OSError,UnicodeError,json.JSONDecodeError) as error:
            raise HTTPException(503,{"code":"asset_unavailable","detail":"tileset JSON cannot be read"}) from error
        geom=json.dumps(layer["scope_geometry"],separators=(",",":"))
        with store.connect() as db:
            matched=db.execute("""SELECT node_path,content_relative_path FROM tileset_node
                WHERE source_kind='pointcloud_resource' AND source_resource_id=%s
                  AND source_version_id=%s AND document_path=%s
                  AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))""",
                (source_id,version_id,document_path,geom,geom)).fetchall()
            candidates=sorted({r["content_relative_path"] for r in matched
                               if r["content_relative_path"] and r["content_relative_path"].lower().endswith(".json")})
            available_documents={r["document_path"] for r in db.execute("""
                SELECT DISTINCT document_path FROM tileset_node
                WHERE source_kind='pointcloud_resource' AND source_resource_id=%s
                  AND source_version_id=%s AND document_path=ANY(%s::text[])
                  AND content_relative_path IS NOT NULL
                  AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                  AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))""",
                (source_id,version_id,candidates,geom,geom)).fetchall()} if candidates else set()
            referenced=sorted({r["content_relative_path"] for r in matched if r["content_relative_path"]})
            catalog={r["relative_path"]:r for r in db.execute("""
                SELECT relative_path,storage_namespace,size_bytes FROM resource_file
                WHERE source_kind='pointcloud_resource' AND source_resource_id=%s
                  AND source_version_id=%s AND relative_path=ANY(%s::text[])""",
                (source_id,version_id,referenced)).fetchall()} if referenced else {}
        if len(catalog) != len(referenced):
            raise HTTPException(503,{"code":"asset_unavailable","detail":"tileset refers to an uncataloged file"})
        for asset in catalog.values():
            actual=file_path(store.data_dir / asset["storage_namespace"],asset["relative_path"])
            if not actual.is_file() or actual.stat().st_size != asset["size_bytes"]:
                raise HTTPException(503,{"code":"asset_unavailable","detail":"tileset refers to a missing file"})
        if not any(r["content_relative_path"] for r in matched):
            raise HTTPException(404,{"code":"empty","detail":"tileset has no content in this scene scope"})
        paths={r["node_path"] for r in matched}
        if not paths:
            raise HTTPException(404,{"code":"empty","detail":"no tile intersects this scene layer"})
        keep=set()
        for value in paths:
            parts=value.split("/")
            for i in range(1,len(parts)+1):
                keep.add("/".join(parts[:i]))
        def visit(node, node_path):
            if node_path not in keep:
                return None
            result=copy.deepcopy(node)
            children=[]
            for i,child in enumerate(node.get("children") or []):
                retained=visit(child,f"{node_path}/{i}")
                if retained is not None:
                    children.append(retained)
            result["children"]=children
            if node_path not in paths:
                result.pop("content",None)
                result.pop("contents",None)
            else:
                content_items=([result["content"]] if "content" in result else [])+list(result.get("contents") or [])
                retained_items=[]
                for item in content_items:
                    uri=item.get("uri",item.get("url"))
                    if not isinstance(uri,str):
                        raise HTTPException(422,{"code":"unsupported_source","detail":"tile URI is invalid"})
                    from .legacy_subsets import _normalize_relative_path
                    target=_normalize_relative_path(posixpath.join(posixpath.dirname(document_path),uri))
                    if target.lower().endswith(".json") and target not in available_documents:
                        continue
                    if target not in catalog:
                        raise HTTPException(503,{"code":"asset_unavailable","detail":"tileset URI is not indexed"})
                    target_url=_scoped_content_uri(document_path,target,layer["layer_id"])
                    if "uri" in item: item["uri"]=target_url
                    else: item["url"]=target_url
                    retained_items.append(item)
                if "content" in result and not retained_items:
                    result.pop("content",None)
                if "contents" in result:
                    result["contents"]=retained_items
            return result
        result=copy.deepcopy(original)
        result["root"]=visit(original["root"],"0")
        return Response(json.dumps(result,ensure_ascii=False,separators=(",",":")),
                        media_type="application/json",headers={"Cache-Control":"private, no-store"})

    @api.get("/{source_id}/versions/{version_id}/tileset.json")
    def root_tileset(source_id: str,version_id: str, scene_layer_id: str,
                     principal: Annotated[dict,Depends(account_identity)]):
        layer=scoped_layer(scene_layer_id,source_id,version_id,principal)
        return dynamic_document(source_id,version_id,"tileset.json",layer)

    @api.get("/{source_id}/versions/{version_id}/files/{relative_path:path}")
    def source_file(source_id: str,version_id: str,relative_path: str,
                    principal: Annotated[dict,Depends(account_identity)],
                    scene_layer_id: str | None = Query(default=None)):
        if scene_layer_id is None:
            raise HTTPException(422,{"code":"scene_layer_required"})
        row,path=indexed_file(source_id,version_id,relative_path)
        layer=scoped_layer(scene_layer_id,source_id,version_id,principal)
        if row["source_kind"] != layer["source_kind"]:
            raise HTTPException(404,{"code":"scene_layer_unavailable"})
        if layer:
            if row["file_role"] == "tileset":
                return dynamic_document(source_id,version_id,relative_path,layer)
            geom=json.dumps(layer["scope_geometry"],separators=(",",":"))
            with store.connect() as db:
                if row["source_kind"] == "raster_collection":
                    hit=db.execute("""SELECT 1 FROM resource_file WHERE source_kind='raster_collection'
                        AND source_resource_id=%s AND source_version_id=%s AND relative_path=%s
                        AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                        AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) LIMIT 1""",
                        (source_id,version_id,relative_path,geom,geom)).fetchone()
                else:
                    hit=db.execute("""SELECT 1 FROM tileset_node WHERE source_kind='pointcloud_resource'
                        AND source_resource_id=%s AND source_version_id=%s AND content_relative_path=%s
                        AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                        AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)) LIMIT 1""",
                        (source_id,version_id,relative_path,geom,geom)).fetchone()
            if hit is None:
                raise HTTPException(404,{"code":"outside_scene_scope"})
        return FileResponse(path,media_type=row["media_type"],headers={"Cache-Control":"private, no-store"})

    @api.get("/{source_id}/versions/{version_id}/tiles/{z}/{x}/{y}")
    def raster_tile(source_id: str,version_id: str,z: int,x: int,y: int,scene_layer_id: str,
                    principal: Annotated[dict,Depends(account_identity)]):
        layer=scoped_layer(scene_layer_id,source_id,version_id,principal)
        if layer["source_kind"] != "raster_collection":
            raise HTTPException(404,{"code":"scene_layer_unavailable"})
        geom=json.dumps(layer["scope_geometry"],separators=(",",":"))
        ensure_schema(store)
        with store.connect() as db:
            row=db.execute("""SELECT relative_path FROM resource_file WHERE
                source_kind='raster_collection' AND source_resource_id=%s AND source_version_id=%s
                AND raster_zoom=%s AND raster_x=%s AND raster_y=%s
                AND geom_wgs84 && ST_SetSRID(ST_GeomFromGeoJSON(%s),4326)
                AND ST_Intersects(geom_wgs84,ST_SetSRID(ST_GeomFromGeoJSON(%s),4326))""",
                (source_id,version_id,z,x,y,geom,geom)).fetchone()
        if row is None:
            raise HTTPException(404,{"code":"outside_scene_scope"})
        asset,path=indexed_file(source_id,version_id,row["relative_path"])
        return FileResponse(path,media_type=asset["media_type"],headers={"Cache-Control":"private, no-store"})

    return api
