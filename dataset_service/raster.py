"""Spatial discovery of locally mirrored legacy TMS tiles."""

import math
import sqlite3
import xml.etree.ElementTree as ET
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError

from .legacy import LegacyStore
from .legacy_cache import LegacyCache


TMS_RESOURCE_TYPE = 12000008
MAX_TILE_CANDIDATES = 25_000


def _child(element: ET.Element, name: str) -> ET.Element | None:
    return next((item for item in element if item.tag.rsplit("}", 1)[-1].casefold() == name.casefold()), None)


def _attributes(element: ET.Element | None) -> dict[str, str]:
    if element is None:
        return {}
    return {key.rsplit("}", 1)[-1].casefold(): value for key, value in element.attrib.items()}


def _number(attributes: dict[str, str], key: str) -> float:
    try:
        value = float(attributes[key.casefold()])
    except (KeyError, ValueError) as error:
        raise ValueError(f"TMS tilemap is missing a valid {key}") from error
    if not math.isfinite(value):
        raise ValueError(f"TMS tilemap has an invalid {key}")
    return value


def _bbox(value: str) -> list[float]:
    try:
        values = [float(part) for part in value.split(",")]
    except ValueError as error:
        raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat") from error
    if (len(values) != 4 or not all(math.isfinite(part) for part in values)
            or not (-180 <= values[0] < values[2] <= 180)
            or not (-90 <= values[1] < values[3] <= 90)):
        raise HTTPException(422, "bbox must be a valid EPSG:4326 extent")
    return values


def _tile_index_range(minimum: float, maximum: float, origin: float, span: float) -> tuple[int, int]:
    return math.floor((minimum - origin) / span), math.ceil((maximum - origin) / span) - 1


def _read_tilemap(cache: LegacyCache, resource_id: str) -> dict:
    target = f"/api/Raster/GetTmsFile/{resource_id}/tilemapresource.xml"
    result = cache.get_cached("GET", target)
    if result.status == 503:
        raise HTTPException(503, "TMS 本地镜像暂不可用")
    if result.status != 200:
        raise HTTPException(404, "本地镜像中没有该 TMS 资源的 tilemapresource.xml")
    try:
        raw = result.file.read_bytes() if result.file is not None else result.body
        if not raw or len(raw) > 1024 * 1024:
            raise ValueError("TMS tilemap XML is empty or too large")
        root = ET.fromstring(raw)
        srs_element = _child(root, "SRS")
        srs_text = (srs_element.text or "").strip() if srs_element is not None else ""
        if not srs_text:
            raise ValueError("TMS tilemap has no SRS")
        source_crs = CRS.from_user_input(srs_text)
        box = _attributes(_child(root, "BoundingBox"))
        origin = _attributes(_child(root, "Origin"))
        image = _attributes(_child(root, "TileFormat"))
        sets = _child(root, "TileSets")
        if sets is None:
            raise ValueError("TMS tilemap has no TileSets")
        width, height = int(image["width"]), int(image["height"])
        if width <= 0 or height <= 0:
            raise ValueError("TMS tilemap has invalid tile dimensions")
        minx, miny, maxx, maxy = (_number(box, key) for key in ("minx", "miny", "maxx", "maxy"))
        origin_x, origin_y = _number(origin, "x"), _number(origin, "y")
        if minx >= maxx or miny >= maxy:
            raise ValueError("TMS tilemap has invalid bounds")
        extension = image.get("extension", "png").strip().lstrip(".")
        if not extension.isalnum():
            raise ValueError("TMS tilemap has an invalid tile extension")
        tile_levels = []
        for tile_set in sets:
            attrs = _attributes(tile_set)
            order = int(attrs["order"])
            units_per_pixel = _number(attrs, "units-per-pixel")
            if order < 0 or units_per_pixel <= 0:
                raise ValueError("TMS tilemap has an invalid tile level")
            span_x, span_y = width * units_per_pixel, height * units_per_pixel
            tile_levels.append({
                "order": order, "span_x": span_x, "span_y": span_y,
                "x_bounds": _tile_index_range(minx, maxx, origin_x, span_x),
                "y_bounds": _tile_index_range(miny, maxy, origin_y, span_y),
            })
        if not tile_levels:
            raise ValueError("TMS tilemap has no tile levels")
        return {
            "crs": source_crs, "crs_name": source_crs.to_string(),
            "bounds": (minx, miny, maxx, maxy), "origin": (origin_x, origin_y),
            "width": width, "height": height, "extension": extension,
            "profile": _attributes(sets).get("profile"), "levels": tile_levels,
        }
    except HTTPException:
        raise
    except (ET.ParseError, ValueError, KeyError, TypeError, OverflowError, ProjError) as error:
        raise HTTPException(422, f"无法解析 TMS 空间索引：{error}") from error


def router(legacy_store: LegacyStore, cache: LegacyCache, reader: Callable) -> APIRouter:
    api = APIRouter(prefix="/spatial/rasters", tags=["spatial-rasters"])

    @api.get("/{resource_id}/tiles")
    def query_raster_tiles(
        resource_id: str,
        _: Annotated[str, Depends(reader)],
        bbox: str,
        zoom: int = Query(ge=0, le=30),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
    ):
        resource = next((row for row in legacy_store.resources if str(row.get("Id")) == resource_id), None)
        if resource is None:
            raise HTTPException(404, "raster resource not found")
        try:
            resource_type = int(resource.get("Type") or 0)
        except (TypeError, ValueError):
            resource_type = 0
        if resource_type != TMS_RESOURCE_TYPE:
            raise HTTPException(422, "该接口目前只支持 Raster TMS 资源")

        bounds = _bbox(bbox)
        tilemap = _read_tilemap(cache, resource_id)
        level = next((item for item in tilemap["levels"] if item["order"] == zoom), None)
        if level is None:
            supported = sorted(item["order"] for item in tilemap["levels"])
            raise HTTPException(422, {"message": "requested zoom is not defined by this TMS", "supported_zooms": supported})

        try:
            to_source = Transformer.from_crs(CRS.from_epsg(4326), tilemap["crs"], always_xy=True)
            projected = to_source.transform_bounds(*bounds, densify_pts=21)
            if not all(math.isfinite(value) for value in projected):
                raise ValueError("bbox could not be projected")
            xmin, ymin, xmax, ymax = projected
        except Exception as error:
            raise HTTPException(422, "bbox cannot be transformed into the TMS coordinate system") from error
        data_minx, data_miny, data_maxx, data_maxy = tilemap["bounds"]
        if xmax <= data_minx or xmin >= data_maxx or ymax <= data_miny or ymin >= data_maxy:
            return {
                "resource_id": resource_id, "label": resource.get("Label"), "bbox": bounds,
                "tile_scheme": "tms", "zoom": zoom, "tile_crs": tilemap["crs_name"], "total_candidates": 0,
                "available_count": 0, "missing_count": 0, "items": [], "limit": limit,
                "offset": offset, "has_more": False,
                "notice": "仅返回本地镜像中实际存在的 TMS 瓦片；不执行远端回源。",
            }

        origin_x, origin_y = tilemap["origin"]
        x0, x1 = _tile_index_range(xmin, xmax, origin_x, level["span_x"])
        y0, y1 = _tile_index_range(ymin, ymax, origin_y, level["span_y"])
        data_x0, data_x1 = level["x_bounds"]
        data_y0, data_y1 = level["y_bounds"]
        x0, x1 = max(x0, data_x0), min(x1, data_x1)
        y0, y1 = max(y0, data_y0), min(y1, data_y1)
        if x1 < x0 or y1 < y0:
            candidates: list[tuple[int, int]] = []
        else:
            candidate_count = (x1 - x0 + 1) * (y1 - y0 + 1)
            if candidate_count > MAX_TILE_CANDIDATES:
                raise HTTPException(413, f"bbox 在 zoom={zoom} 下命中 {candidate_count} 个瓦片，请缩小范围或降低 zoom")
            candidates = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]

        suffix = tilemap["extension"]
        targets = [f"/api/Raster/GetTmsFile/{resource_id}/{zoom}/{x}/{y}.{suffix}" for x, y in candidates]
        try:
            available = cache.cached_targets("GET", targets)
        except (OSError, sqlite3.Error) as error:
            raise HTTPException(503, "TMS 本地镜像暂不可用") from error
        to_lonlat = Transformer.from_crs(tilemap["crs"], CRS.from_epsg(4326), always_xy=True)
        items = []
        for x, y in candidates[offset:offset + limit]:
            tile_minx = origin_x + x * level["span_x"]
            tile_miny = origin_y + y * level["span_y"]
            tile_maxx = tile_minx + level["span_x"]
            tile_maxy = tile_miny + level["span_y"]
            try:
                tile_bounds = to_lonlat.transform_bounds(tile_minx, tile_miny, tile_maxx, tile_maxy, densify_pts=11)
                tile_bounds = [round(value, 9) for value in tile_bounds]
            except Exception:
                tile_bounds = None
            target = f"/api/Raster/GetTmsFile/{resource_id}/{zoom}/{x}/{y}.{suffix}"
            items.append({
                "z": zoom, "x": x, "y": y, "bounds": tile_bounds,
                "available": target in available, "file_url": target,
            })

        return {
            "resource_id": resource_id, "label": resource.get("Label"), "bbox": bounds,
            "tile_scheme": "tms", "zoom": zoom, "tile_crs": tilemap["crs_name"], "tile_profile": tilemap["profile"],
            "total_candidates": len(candidates), "available_count": len(available),
            "missing_count": len(candidates) - len(available), "items": items,
            "limit": limit, "offset": offset, "has_more": offset + limit < len(candidates),
            "notice": "候选瓦片按 tilemapresource.xml 的范围和分辨率计算；仅返回本地镜像命中状态，不执行远端回源。",
        }

    return api
