"""Time-aware storage and spatial access for local XYZ/TMS raster pyramids."""

import json
import math
import re
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated, Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pyproj import Transformer

from .spatial import SpatialStore
from .location_review import confirmed_location_review

WEB_MERCATOR_HALF_WORLD = 20037508.342789244
DATE_NAME = re.compile(r"^imagery_(\d{8})$")


def _epochs(collection: dict, spatial_store: SpatialStore) -> list[dict]:
    with spatial_store.connect() as db:
        rows = db.execute("""SELECT acquisition_date,storage_path,tile_count,total_bytes,status,metadata
                             FROM raster_epoch WHERE collection_id=%s AND status='published'
                             ORDER BY acquisition_date""", (collection["id"],)).fetchall()
    return [{**dict(row), "acquisition_date": row["acquisition_date"].isoformat(),
             "metadata": row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])} for row in rows]


def router(spatial_store: SpatialStore, data_dir: Path, reader: Callable) -> APIRouter:
    api = APIRouter(prefix="/spatial/raster-collections", tags=["temporal-rasters"])
    raster_root = (data_dir / "rasters").resolve()

    def published_collection(collection_id: str) -> dict:
        spatial_store.ensure_schema()
        try:
            normalized = str(uuid.UUID(collection_id))
        except ValueError:
            raise HTTPException(422, "invalid raster collection id") from None
        with spatial_store.connect() as db:
            row = db.execute("""SELECT id,name,description,crs_epsg,tile_scheme,tile_size,tile_format,
                                      min_zoom,max_zoom,ST_AsGeoJSON(bounds)::json AS bounds,metadata
                               FROM raster_collection WHERE id=%s""", (normalized,)).fetchone()
        if row is None:
            raise HTTPException(404, "raster collection not found")
        result = dict(row)
        result["id"] = str(result["id"])
        if not isinstance(result["bounds"], dict):
            result["bounds"] = json.loads(result["bounds"])
        if not isinstance(result["metadata"], dict):
            result["metadata"] = json.loads(result["metadata"])
        return result

    @api.get("")
    def list_collections(_: Annotated[str, Depends(reader)]):
        spatial_store.ensure_schema()
        with spatial_store.connect() as db:
            rows = db.execute("""SELECT c.id,c.name,c.description,c.crs_epsg,c.tile_scheme,c.tile_size,
                    c.tile_format,c.min_zoom,c.max_zoom,ST_AsGeoJSON(c.bounds)::json AS bounds,
                    c.created_at,
                    count(e.acquisition_date) FILTER (WHERE e.status='published') AS epoch_count,
                    COALESCE(sum(e.tile_count) FILTER (WHERE e.status='published'),0) AS tile_count,
                    COALESCE(sum(e.total_bytes) FILTER (WHERE e.status='published'),0) AS total_bytes
                FROM raster_collection c LEFT JOIN raster_epoch e ON e.collection_id=c.id
                GROUP BY c.id ORDER BY c.name,c.id""").fetchall()
        return {"collections": [{**dict(row), "id": str(row["id"]),
                    "bounds": row["bounds"] if isinstance(row["bounds"], dict) else json.loads(row["bounds"])} for row in rows]}

    @api.get("/{collection_id}")
    def get_collection(collection_id: str, _: Annotated[str, Depends(reader)]):
        collection = published_collection(collection_id)
        epochs = _epochs(collection, spatial_store)
        for epoch in epochs:
            epoch["tile_url_template"] = (
                f"/spatial/raster-collections/{collection['id']}/epochs/"
                f"{epoch['acquisition_date']}/tiles/{{z}}/{{x}}/{{y}}.{collection['tile_format']}"
            )
        return {**collection, "epochs": epochs}

    @api.get("/{collection_id}/tiles")
    def query_tiles(collection_id: str, _: Annotated[str, Depends(reader)],
                    acquisition_date: date, bbox: str, zoom: int = Query(ge=0, le=30),
                    limit: int = Query(1000, ge=1, le=10000), offset: int = Query(0, ge=0)):
        collection = published_collection(collection_id)
        if not collection["min_zoom"] <= zoom <= collection["max_zoom"]:
            raise HTTPException(422, {"message": "zoom outside collection range",
                                      "min_zoom": collection["min_zoom"], "max_zoom": collection["max_zoom"]})
        try:
            bounds = [float(value) for value in bbox.split(",")]
            if (len(bounds) != 4 or not all(math.isfinite(v) for v in bounds)
                    or not (-180 <= bounds[0] < bounds[2] <= 180)
                    or not (-90 <= bounds[1] < bounds[3] <= 90)):
                raise ValueError
        except ValueError:
            raise HTTPException(422, "bbox must be minLon,minLat,maxLon,maxLat in EPSG:4326") from None
        west, south, east, north = bounds
        polygon = collection["bounds"]["coordinates"][0]
        collection_west = min(point[0] for point in polygon)
        collection_south = min(point[1] for point in polygon)
        collection_east = max(point[0] for point in polygon)
        collection_north = max(point[1] for point in polygon)
        west, south = max(west, collection_west), max(south, collection_south)
        east, north = min(east, collection_east), min(north, collection_north)
        if west >= east or south >= north:
            return {"collection_id": collection["id"], "acquisition_date": acquisition_date.isoformat(),
                    "bbox": bounds, "crs_epsg": collection["crs_epsg"], "tile_scheme": collection["tile_scheme"],
                    "zoom": zoom, "total_candidates": 0, "available_count": 0,
                    "missing_count": 0, "items": [], "limit": limit, "offset": offset, "has_more": False}
        with spatial_store.connect() as db:
            epoch = db.execute("""SELECT storage_path,tile_count,total_bytes FROM raster_epoch
                                  WHERE collection_id=%s AND acquisition_date=%s AND status='published'""",
                               (collection["id"], acquisition_date)).fetchone()
        if epoch is None:
            raise HTTPException(404, "raster epoch not found")
        level_path = (raster_root / epoch["storage_path"] / str(zoom)).resolve()
        try:
            level_path.relative_to(raster_root)
        except ValueError:
            raise HTTPException(503, "stored raster path is invalid") from None
        if not level_path.is_dir():
            raise HTTPException(503, "stored raster epoch is unavailable")

        if collection["crs_epsg"] == 3857:
            project = Transformer.from_crs(4326, 3857, always_xy=True)
            xmin, ymin = project.transform(west, south)
            xmax, ymax = project.transform(east, north)
            span = 2 * WEB_MERCATOR_HALF_WORLD / (2 ** zoom)
            x0 = max(0, math.floor((xmin + WEB_MERCATOR_HALF_WORLD) / span))
            x1 = min(2 ** zoom - 1, math.floor((xmax + WEB_MERCATOR_HALF_WORLD) / span))
            y0 = max(0, math.floor((ymin + WEB_MERCATOR_HALF_WORLD) / span))
            y1 = min(2 ** zoom - 1, math.floor((ymax + WEB_MERCATOR_HALF_WORLD) / span))
        else:
            span_x = 360.0 / (2 ** zoom)
            # The common EPSG:4326 geodetic matrix has twice as many columns as rows at zoom 0.
            span_y = 180.0 / (2 ** zoom)
            x0 = max(0, math.floor((west + 180.0) / span_x))
            x1 = min(2 * (2 ** zoom) - 1, math.floor((east + 180.0) / span_x))
            y0 = max(0, math.floor((south + 90.0) / span_y))
            y1 = min((2 ** zoom) - 1, math.floor((north + 90.0) / span_y))
        if x1 < x0 or y1 < y0:
            candidates = []
        else:
            candidate_count = (x1 - x0 + 1) * (y1 - y0 + 1)
            if candidate_count > 100000:
                raise HTTPException(413, "bbox selects too many tiles; narrow the query")
            candidates = [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]
        tile_dir = level_path
        ext = str(collection["tile_format"]).lstrip(".")
        items = []
        for x, y in candidates[offset:offset + limit]:
            file_path = (tile_dir / str(x) / f"{y}.{ext}").resolve()
            try:
                file_path.relative_to(tile_dir)
            except ValueError:
                continue
            relative_url = (f"/spatial/raster-collections/{collection['id']}/epochs/"
                            f"{acquisition_date.isoformat()}/tiles/{zoom}/{x}/{y}.{ext}")
            items.append({"z": zoom, "x": x, "y": y, "available": file_path.is_file(),
                          "file_url": relative_url})
        available = sum(1 for item in items if item["available"])
        return {"collection_id": collection["id"], "acquisition_date": acquisition_date.isoformat(),
                "bbox": bounds, "crs_epsg": collection["crs_epsg"], "tile_scheme": collection["tile_scheme"],
                "zoom": zoom, "total_candidates": len(candidates), "available_count": available,
                "missing_count": len(items) - available, "items": items,
                "limit": limit, "offset": offset, "has_more": offset + limit < len(candidates)}

    @api.get("/{collection_id}/epochs/{acquisition_date}/tiles/{zoom}/{x}/{y}.{extension}")
    def get_tile(collection_id: str, acquisition_date: date, zoom: int, x: int, y: int, extension: str,
                 _: Annotated[str, Depends(reader)]):
        collection = published_collection(collection_id)
        if extension.lower().lstrip(".") != str(collection["tile_format"]).lower().lstrip("."):
            raise HTTPException(404, "tile not found")
        if not collection["min_zoom"] <= zoom <= collection["max_zoom"] or x < 0 or y < 0:
            raise HTTPException(404, "tile not found")
        with spatial_store.connect() as db:
            epoch = db.execute("""SELECT storage_path FROM raster_epoch WHERE collection_id=%s
                                  AND acquisition_date=%s AND status='published'""",
                               (collection["id"], acquisition_date)).fetchone()
        if epoch is None:
            raise HTTPException(404, "raster epoch not found")
        tile_root = (raster_root / epoch["storage_path"] / str(zoom)).resolve()
        tile_path = (tile_root / str(x) / f"{y}.{extension}").resolve()
        try:
            tile_path.relative_to(tile_root)
        except ValueError:
            raise HTTPException(404, "tile not found") from None
        if not tile_path.is_file():
            raise HTTPException(404, "tile not found")
        media_type = "image/png" if extension.lower() == "png" else "image/jpeg" if extension.lower() in {"jpg", "jpeg"} else "application/octet-stream"
        return FileResponse(tile_path, media_type=media_type, headers={"Cache-Control": "private, max-age=86400"})

    return api


def register_local_collection(spatial_store: SpatialStore, data_dir: Path, collection_id: str,
                              root: Path, *, name: str, location_review_confirmed: bool,
                              location_review_source: str, reviewer: str) -> dict:
    """Validate and register a completed on-volume import; never deletes source files."""
    root = root.resolve()
    raster_root = (data_dir / "rasters").resolve()
    try:
        relative_root = root.relative_to(raster_root)
    except ValueError:
        raise ValueError("raster import root must be inside the persistent raster volume") from None
    if not root.is_dir():
        raise ValueError("raster import root does not exist")
    if location_review_confirmed is not True or len(location_review_source.strip()) < 5:
        raise ValueError("operator must confirm and cite the raster CRS and WGS84 bounds review")
    try:
        collection_id = str(uuid.UUID(collection_id))
    except ValueError:
        raise ValueError("invalid collection_id") from None
    epoch_docs = []
    for period in sorted(path for path in root.iterdir() if path.is_dir()):
        match = DATE_NAME.fullmatch(period.name)
        meta_file = period / "meta.json"
        if not match or not meta_file.is_file():
            raise ValueError(f"period directory must be imagery_YYYYMMDD and include meta.json: {period.name}")
        day = date(int(match.group(1)[:4]), int(match.group(1)[4:6]), int(match.group(1)[6:8]))
        metadata = json.loads(meta_file.read_text(encoding="utf-8"))
        if metadata.get("scheme") != "tms" or metadata.get("crs") not in {"EPSG:3857", "EPSG:4326"}:
            raise ValueError(f"unsupported scheme or CRS in {meta_file}")
        if metadata.get("scheme") != "tms" or metadata.get("tileSize") not in {256, 512}:
            raise ValueError(f"unsupported tile scheme or tile size in {meta_file}")
        if not isinstance(metadata.get("bounds"), list) or len(metadata["bounds"]) != 4:
            raise ValueError(f"invalid bounds in {meta_file}")
        counts: dict[int, int] = {}
        sizes = 0
        for z_path in period.iterdir():
            if not z_path.is_dir():
                continue
            try:
                zoom = int(z_path.name)
            except ValueError:
                continue
            counts[zoom] = 0
            for x_path in z_path.iterdir():
                if not x_path.is_dir() or not x_path.name.isdecimal():
                    raise ValueError(f"invalid tile column directory: {x_path}")
                for tile_path in x_path.iterdir():
                    if tile_path.suffix.lower() != ".png" or not tile_path.stem.isdecimal() or not tile_path.is_file():
                        raise ValueError(f"invalid tile file: {tile_path}")
                    counts[zoom] += 1
                    sizes += tile_path.stat().st_size
        expected = set(range(int(metadata["minzoom"]), int(metadata["maxzoom"]) + 1))
        if set(counts) != expected or any(value == 0 for value in counts.values()):
            raise ValueError(f"zoom levels do not match metadata for {period.name}")
        if metadata.get("tiles") != ["{z}/{x}/{y}.png"]:
            raise ValueError(f"unexpected tile path template in {meta_file}")
        bounds = [float(v) for v in metadata["bounds"]]
        if not (-180 <= bounds[0] < bounds[2] <= 180 and -90 <= bounds[1] < bounds[3] <= 90):
            raise ValueError(f"bounds must be WGS84 longitude/latitude in {meta_file}")
        epoch_docs.append({"date": day, "path": (relative_root / period.name).as_posix(),
                           "count": sum(counts.values()), "size": sizes,
                           "metadata": {"zoom_counts": {str(k): v for k, v in sorted(counts.items())},
                                        "source_name": metadata.get("name"),
                                        "source_format": metadata.get("sourceFormat"),
                                        "content_type": metadata.get("contentType"),
                                        "boundary_processing": metadata.get("boundaryProcessing")}})
    if not epoch_docs:
        raise ValueError("no dated raster epochs found")
    metas = [json.loads((root / f"imagery_{item['date'].strftime('%Y%m%d')}" / "meta.json").read_text()) for item in epoch_docs]
    shared = [(m.get("crs"), m.get("scheme"), m.get("tileSize"), m.get("minzoom"), m.get("maxzoom"), m.get("bounds"), m.get("tiles")) for m in metas]
    if any(item != shared[0] for item in shared[1:]):
        raise ValueError("all epochs in one collection must use the same CRS, scheme, matrix, and bounds")
    crs, scheme, tile_size, min_zoom, max_zoom, bounds, _ = shared[0]
    west, south, east, north = map(float, bounds)
    location_review = confirmed_location_review(
        source_crs=crs, evidence=location_review_source,
        scope=["raster_crs", "wgs84_bounds", "tile_matrix", "tile_scheme"], reviewer=reviewer,
    ) | {"extent_method": "operator_reviewed_tms_bounds_from_period_metadata"}
    with spatial_store.connect() as db:
        db.execute("""INSERT INTO raster_collection
            (id,name,description,crs_epsg,tile_scheme,tile_size,tile_format,min_zoom,max_zoom,bounds,metadata)
            VALUES (%s,%s,%s,%s,%s,%s,'png',%s,%s,ST_MakeEnvelope(%s,%s,%s,%s,4326),%s::jsonb)
            ON CONFLICT (id) DO UPDATE SET name=EXCLUDED.name,description=EXCLUDED.description,
                crs_epsg=EXCLUDED.crs_epsg,tile_scheme=EXCLUDED.tile_scheme,tile_size=EXCLUDED.tile_size,
                tile_format=EXCLUDED.tile_format,min_zoom=EXCLUDED.min_zoom,max_zoom=EXCLUDED.max_zoom,
                bounds=EXCLUDED.bounds,metadata=EXCLUDED.metadata,updated_at=now()""",
            (collection_id, name, f"{len(epoch_docs)} dated TMS image epochs", int(crs.split(":")[1]),
             scheme, tile_size, min_zoom, max_zoom, west, south, east, north,
             json.dumps({"path_template": "{z}/{x}/{y}.png", "time_field": "acquisition_date",
                         "location_review": location_review})))
        for item in epoch_docs:
            existing = db.execute("""SELECT storage_path,tile_count,total_bytes,status FROM raster_epoch
                WHERE collection_id=%s AND acquisition_date=%s""",(collection_id,item["date"])).fetchone()
            if existing and existing["status"] == "published":
                raise ValueError("published raster epoch is immutable; import a new collection or dated version")
            db.execute("""INSERT INTO raster_epoch
                (collection_id,acquisition_date,storage_path,tile_count,total_bytes,status,metadata)
                VALUES (%s,%s,%s,%s,%s,'staging',%s::jsonb)
                ON CONFLICT (collection_id,acquisition_date) DO UPDATE SET
                    storage_path=EXCLUDED.storage_path,tile_count=EXCLUDED.tile_count,
                    total_bytes=EXCLUDED.total_bytes,
                    status='staging',metadata=EXCLUDED.metadata""",
                (collection_id, item["date"], item["path"], item["count"], item["size"], json.dumps(item["metadata"])))
    from .resource_index import index_raster_epoch
    for item in epoch_docs:
        index_raster_epoch(spatial_store,collection_id,item["date"].isoformat())
        with spatial_store.connect() as db:
            db.execute("""UPDATE raster_epoch SET status='published' WHERE collection_id=%s
                AND acquisition_date=%s AND status='staging'""",(collection_id,item["date"]))
    return {"collection_id": collection_id, "name": name, "epoch_count": len(epoch_docs),
            "epochs": [{"acquisition_date": item["date"].isoformat(), "tile_count": item["count"], "bytes": item["size"]} for item in epoch_docs],
            "crs": crs, "scheme": scheme, "tile_size": tile_size,
            "zoom_levels": [min_zoom, max_zoom], "bounds": bounds}
