"""Published file and 3D Tiles node catalog for spatial reads.

The index is derived from immutable source files.  It never creates a resource
or copies a tile.  A version becomes queryable only after a complete backfill.
"""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import posixpath
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pyproj import Transformer

from .legacy_subsets import IDENTITY, SubsetError, _multiply_matrix, _normalize_relative_path, _transform_point


SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_index_version (
 source_kind text NOT NULL, source_resource_id text NOT NULL, source_version_id text NOT NULL,
 status text NOT NULL CHECK(status IN ('building','ready','unsupported_source','failed')),
 entrypoint text, storage_namespace text NOT NULL, file_count bigint NOT NULL DEFAULT 0,
 node_count bigint NOT NULL DEFAULT 0, source_fingerprint text NOT NULL,
 detail text NOT NULL DEFAULT '', updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_kind,source_resource_id,source_version_id)
);
CREATE TABLE IF NOT EXISTS resource_file (
 source_kind text NOT NULL, source_resource_id text NOT NULL, source_version_id text NOT NULL,
 relative_path text NOT NULL, storage_namespace text NOT NULL,
 size_bytes bigint NOT NULL CHECK(size_bytes >= 0), sha256 text NOT NULL,
 media_type text NOT NULL, file_role text NOT NULL,
 geom_wgs84 geometry(Geometry,4326), extent_status text NOT NULL DEFAULT 'unlocated',
 raster_zoom integer, raster_x integer, raster_y integer, acquisition_date date,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(source_kind,source_resource_id,source_version_id,relative_path)
);
CREATE INDEX IF NOT EXISTS resource_file_geom_gix ON resource_file USING gist(geom_wgs84);
CREATE UNIQUE INDEX IF NOT EXISTS resource_file_raster_unique_idx ON resource_file
 (source_kind,source_resource_id,source_version_id,acquisition_date,raster_zoom,raster_x,raster_y)
 WHERE raster_zoom IS NOT NULL;
CREATE TABLE IF NOT EXISTS tileset_node (
 source_kind text NOT NULL, source_resource_id text NOT NULL, source_version_id text NOT NULL,
 document_path text NOT NULL, node_path text NOT NULL,
 parent_document_path text, parent_node_path text, content_relative_path text,
 bounding_volume_json jsonb NOT NULL, world_transform_json jsonb NOT NULL,
 geom_wgs84 geometry(Geometry,4326), geometric_error double precision,
 refine text, node_status text NOT NULL,
 PRIMARY KEY(source_kind,source_resource_id,source_version_id,document_path,node_path)
);
CREATE INDEX IF NOT EXISTS tileset_node_geom_gix ON tileset_node USING gist(geom_wgs84);
CREATE INDEX IF NOT EXISTS tileset_node_parent_idx ON tileset_node
 (source_kind,source_resource_id,source_version_id,parent_document_path,parent_node_path);
CREATE INDEX IF NOT EXISTS tileset_node_content_idx ON tileset_node
 (source_kind,source_resource_id,source_version_id,content_relative_path)
 WHERE content_relative_path IS NOT NULL;
"""

ECEF_TO_WGS84 = Transformer.from_crs(4978, 4326, always_xy=True)


def safe_path(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ValueError("invalid relative path")
    decoded = unquote(raw)
    if decoded.startswith("/") or any(part in ("", ".", "..") for part in decoded.split("/")):
        raise ValueError("path must be a canonical relative POSIX path")
    if any(ord(char) < 32 for char in decoded) or decoded != raw:
        raise ValueError("encoded or control-character path is not allowed")
    return decoded


def file_path(root: Path, relative_path: str) -> Path:
    root = root.resolve()
    path = (root / safe_path(relative_path)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("path escapes storage namespace")
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _tile_geometry(volume: dict, matrix: tuple[float, ...]) -> str:
    """Return an expanded WGS84 envelope; over-selection is intentional."""
    if not isinstance(volume, dict):
        raise SubsetError("tile lacks a boundingVolume")
    if "region" in volume:
        v = volume["region"]
        if not isinstance(v, list) or len(v) != 6:
            raise SubsetError("invalid region boundingVolume")
        west, south, east, north = map(math.degrees, map(float, v[:4]))
        if east < west:
            raise SubsetError("antimeridian region is not supported")
        bounds = (west, south, east, north)
    else:
        points = []
        if "box" in volume:
            v = volume["box"]
            if not isinstance(v, list) or len(v) != 12:
                raise SubsetError("invalid box boundingVolume")
            v = list(map(float, v))
            center, axes = v[:3], (v[3:6], v[6:9], v[9:12])
            for sx in (-1, 0, 1):
                for sy in (-1, 0, 1):
                    for sz in (-1, 0, 1):
                        points.append([center[i] + sx*axes[0][i] + sy*axes[1][i] + sz*axes[2][i] for i in range(3)])
        elif "sphere" in volume:
            v = volume["sphere"]
            if not isinstance(v, list) or len(v) != 4 or float(v[3]) < 0:
                raise SubsetError("invalid sphere boundingVolume")
            center, radius = list(map(float, v[:3])), float(v[3])
            for sx in (-1, 0, 1):
                for sy in (-1, 0, 1):
                    for sz in (-1, 0, 1):
                        points.append([center[0] + sx*radius, center[1] + sy*radius, center[2] + sz*radius])
            # Sample corners and face/edge centres; the resulting envelope can only over-select.
        else:
            raise SubsetError("unsupported boundingVolume")
        world_points = [_transform_point(matrix,p) for p in points]
        coords = [ECEF_TO_WGS84.transform(*p)[:2] for p in world_points]
        if any(not math.isfinite(x) or not math.isfinite(y) for x, y in coords):
            raise SubsetError("untransformable tile boundingVolume")
        bounds = (min(x for x, _ in coords), min(y for _, y in coords),
                  max(x for x, _ in coords), max(y for _, y in coords))
        # The box/sphere sits inside the ECEF ball whose radius is the farthest
        # sampled corner.  Expand the geodetic envelope by that ball's angular
        # radius to cover curved longitude/latitude extrema between samples.
        world_center = _transform_point(matrix, center)
        radius = max(math.dist(world_center,p) for p in world_points)
        earth_radius = math.sqrt(sum(v*v for v in world_center))
        if radius >= earth_radius / 2:
            raise SubsetError("tile boundingVolume is too large for a safe local envelope")
        lat_pad = 2 * math.degrees(math.asin(min(1.0,radius/(earth_radius-radius)))) + 1e-6
        center_lon,center_lat = ECEF_TO_WGS84.transform(*world_center)[:2]
        polar = min(89.999,abs(center_lat)+lat_pad)
        lon_pad = min(180.0,lat_pad/max(0.001,math.cos(math.radians(polar))))
        bounds = (min(bounds[0],center_lon-lon_pad),min(bounds[1],center_lat-lat_pad),
                  max(bounds[2],center_lon+lon_pad),max(bounds[3],center_lat+lat_pad))
    west, south, east, north = bounds
    if east - west > 180 or not (-180 <= west <= east <= 180 and -90 <= south <= north <= 90):
        raise SubsetError("tile bounds cross the antimeridian or WGS84 limits")
    # The transformed Euclidean box can have curved extrema between samples.
    # A generous margin keeps this index conservative for local reconstruction tiles.
    pad = 1e-7 if "region" in volume else 1e-6
    return f"POLYGON(({max(-180,west-pad):.10f} {max(-90,south-pad):.10f},{min(180,east+pad):.10f} {max(-90,south-pad):.10f},{min(180,east+pad):.10f} {min(90,north+pad):.10f},{max(-180,west-pad):.10f} {min(90,north+pad):.10f},{max(-180,west-pad):.10f} {max(-90,south-pad):.10f}))"


def _content_paths(node: dict, document_path: str) -> list[str]:
    content = ([node["content"]] if "content" in node else []) + list(node.get("contents") or [])
    paths = []
    for item in content:
        uri = item.get("uri", item.get("url")) if isinstance(item, dict) else None
        if not isinstance(uri, str) or urlsplit(uri).scheme or uri.startswith("//"):
            raise SubsetError("external or missing tile URI")
        path = _normalize_relative_path(posixpath.join(posixpath.dirname(document_path), uri))
        paths.append(path)
    return paths


def scan_tileset(base: Path, assets: dict[str, dict], entrypoint: str):
    """Yield nodes; recurse into child documents and reject unsafe sources."""
    seen_documents: dict[str, tuple[float, ...]] = {}

    def document(path: str, inherited: tuple[float, ...]):
        if path not in assets:
            raise SubsetError(f"missing tileset document: {path}")
        prior = seen_documents.get(path)
        if prior is not None:
            if any(abs(a-b) > 1e-7 for a,b in zip(prior, inherited)):
                raise SubsetError("same child tileset has conflicting transforms")
            return
        seen_documents[path] = inherited
        try:
            root = json.loads(file_path(base, path).read_text())
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise SubsetError(f"invalid tileset document: {path}") from error
        if not isinstance(root, dict) or not isinstance(root.get("root"), dict):
            raise SubsetError("tileset has no root")
        if "3DTILES_implicit_tiling" in (root.get("extensions") or {}):
            raise SubsetError("implicit tiles are not supported")

        def tile(node: dict, node_path: str, parent: tuple[str,str] | None, matrix):
            if not isinstance(node, dict) or "implicitTiling" in node or "3DTILES_implicit_tiling" in (node.get("extensions") or {}):
                raise SubsetError("invalid or implicit tile node")
            transform = node.get("transform")
            if transform is not None:
                if not isinstance(transform, list) or len(transform) != 16:
                    raise SubsetError("invalid tile transform")
                matrix = _multiply_matrix(matrix, tuple(map(float, transform)))
            geom = _tile_geometry(node.get("boundingVolume"), matrix)
            paths = _content_paths(node, path)
            if len(paths) > 1:
                raise SubsetError("multi-content tiles require separate node indexing")
            content_path = paths[0] if paths else None
            if content_path and content_path not in assets:
                raise SubsetError(f"missing tile content: {content_path}")
            yield (path, node_path, parent[0] if parent else None, parent[1] if parent else None,
                   content_path, json.dumps(node["boundingVolume"]), json.dumps(matrix), geom,
                   float(node.get("geometricError", 0)), node.get("refine"), "ready")
            if content_path and content_path.lower().endswith(".json"):
                yield from document(content_path, matrix)
            for i, child in enumerate(node.get("children") or []):
                yield from tile(child, f"{node_path}/{i}", (path,node_path), matrix)

        yield from tile(root["root"], "0", None, inherited)

    yield from document(entrypoint, IDENTITY)


def ensure_schema(store) -> None:
    store.ensure_schema()
    with store.connect() as db:
        db.execute(SCHEMA)


def index_pointcloud_version(store, resource_id: str, version_id: str,
                             *, verify_hashes: bool = True) -> dict:
    """Build or rebuild the immutable 3D Tiles index, then mark it ready.

    Rows are written in batches so an interrupted run leaves a visible
    ``building`` version.  Rerunning the function safely reconstructs it.
    """
    if not verify_hashes:
        raise ValueError("published file index requires SHA-256 verification")
    ensure_schema(store)
    with store.connect() as db:
        version = db.execute("""SELECT id,resource_id,status,format,entrypoint,file_count,total_bytes
                                FROM pointcloud_version WHERE id=%s AND resource_id=%s""",
                             (version_id, resource_id)).fetchone()
        if version is None or version["format"] != "3dtiles" or version["status"] not in {"uploading", "published"}:
            raise ValueError("source is not a 3D Tiles pointcloud version")
        assets = {row["relative_path"]: dict(row) for row in db.execute(
            "SELECT relative_path,size_bytes,sha256,media_type FROM pointcloud_asset WHERE version_id=%s",
            (version_id,)).fetchall()}
    if version["entrypoint"] not in assets:
        raise ValueError("entrypoint is absent from the asset catalog")
    base = (store.data_dir / "pointcloud" / "files" / str(resource_id) / str(version_id)).resolve()
    if not base.is_relative_to((store.data_dir / "pointcloud" / "files").resolve()):
        raise ValueError("invalid source storage directory")
    fingerprint = hashlib.sha256((str(version_id) + ":" + assets[version["entrypoint"]]["sha256"] +
                                  ":" + str(len(assets))).encode()).hexdigest()
    kind = "pointcloud_resource"
    with store.connect() as db:
        db.execute("""INSERT INTO resource_index_version
            (source_kind,source_resource_id,source_version_id,status,entrypoint,storage_namespace,source_fingerprint)
            VALUES (%s,%s,%s,'building',%s,%s,%s)
            ON CONFLICT (source_kind,source_resource_id,source_version_id)
            DO UPDATE SET status='building',entrypoint=EXCLUDED.entrypoint,
                storage_namespace=EXCLUDED.storage_namespace,source_fingerprint=EXCLUDED.source_fingerprint,
                detail='',updated_at=now()""",
            (kind, str(resource_id), str(version_id), version["entrypoint"],
             f"pointcloud/files/{resource_id}/{version_id}", fingerprint))
        db.execute("DELETE FROM resource_file WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s",
                   (kind, str(resource_id), str(version_id)))
        db.execute("DELETE FROM tileset_node WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s",
                   (kind, str(resource_id), str(version_id)))
    file_sql = """INSERT INTO resource_file
        (source_kind,source_resource_id,source_version_id,relative_path,storage_namespace,
         size_bytes,sha256,media_type,file_role,extent_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'unlocated')"""
    rows = []
    total = 0
    for path, asset in assets.items():
        actual = file_path(base, path)
        if not actual.is_file() or actual.stat().st_size != asset["size_bytes"]:
            raise ValueError(f"asset_unavailable: {path}")
        if verify_hashes and sha256_file(actual).lower() != asset["sha256"].lower():
            raise ValueError(f"asset checksum mismatch: {path}")
        total += asset["size_bytes"]
        role = "tileset" if path.lower().endswith(".json") else "tile_content" if path.lower().endswith((".b3dm", ".pnts", ".i3dm", ".cmpt", ".glb")) else "auxiliary"
        rows.append((kind, str(resource_id), str(version_id), path,
                     f"pointcloud/files/{resource_id}/{version_id}", asset["size_bytes"],
                     asset["sha256"], asset["media_type"], role))
        if len(rows) >= 2000:
            with store.connect() as db:
                db.cursor().executemany(file_sql, rows)
            rows.clear()
    if rows:
        with store.connect() as db:
            db.cursor().executemany(file_sql, rows)
    if version["status"] == "published" and (len(assets) != version["file_count"] or total != version["total_bytes"]):
        raise ValueError("published version file count or bytes differ from the asset catalog")
    node_sql = """INSERT INTO tileset_node
        (source_kind,source_resource_id,source_version_id,document_path,node_path,
         parent_document_path,parent_node_path,content_relative_path,bounding_volume_json,
         world_transform_json,geom_wgs84,geometric_error,refine,node_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,ST_GeomFromText(%s,4326),%s,%s,%s)"""
    nodes = []
    node_count = 0
    referenced = set()
    try:
        for row in scan_tileset(base, assets, version["entrypoint"]):
            if row[4]:
                referenced.add(row[4])
            nodes.append((kind, str(resource_id), str(version_id), *row))
            node_count += 1
            if len(nodes) >= 2000:
                with store.connect() as db:
                    db.cursor().executemany(node_sql, nodes)
                nodes.clear()
        if nodes:
            with store.connect() as db:
                db.cursor().executemany(node_sql, nodes)
        # A referenced content file may be used by several nodes.  The envelope
        # of every use is retained, so file-level spatial reads cannot miss one.
        with store.connect() as db:
            db.execute("""UPDATE resource_file f SET
                geom_wgs84=n.geom,extent_status='indexed'
                FROM (SELECT content_relative_path,
                             ST_Envelope(ST_Collect(geom_wgs84)) AS geom
                      FROM tileset_node WHERE source_kind=%s AND source_resource_id=%s
                       AND source_version_id=%s AND content_relative_path IS NOT NULL
                      GROUP BY content_relative_path) n
                WHERE f.source_kind=%s AND f.source_resource_id=%s
                  AND f.source_version_id=%s AND f.relative_path=n.content_relative_path""",
                (kind,str(resource_id),str(version_id),kind,str(resource_id),str(version_id)))
            db.execute("""UPDATE resource_index_version SET status='ready',file_count=%s,node_count=%s,
                          detail='',updated_at=now() WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s""",
                       (len(assets),node_count,kind,str(resource_id),str(version_id)))
    except (OSError, ValueError, SubsetError, TypeError, OverflowError) as error:
        with store.connect() as db:
            db.execute("""UPDATE resource_index_version SET status='unsupported_source',detail=%s,
                          updated_at=now() WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s""",
                       (str(error)[:1000],kind,str(resource_id),str(version_id)))
        raise
    return {"source_kind": kind, "source_resource_id": str(resource_id),
            "source_version_id": str(version_id), "file_count": len(assets),
            "node_count": node_count, "referenced_file_count": len(referenced),
            "total_bytes": total, "status": "ready"}


def _raster_wkt(crs_epsg: int, scheme: str, zoom: int, x: int, y: int) -> str:
    if zoom < 0 or zoom > 30 or x < 0 or y < 0:
        raise ValueError("invalid raster tile coordinates")
    if crs_epsg == 3857:
        half = 20037508.342789244
        span = 2 * half / 2**zoom
        if x >= 2**zoom or y >= 2**zoom:
            raise ValueError("tile coordinate exceeds matrix")
        xmin, xmax = -half + x*span, -half + (x+1)*span
        south_y, north_y = ((-half + y*span, -half + (y+1)*span) if scheme == "tms"
                            else (half - (y+1)*span, half - y*span))
        convert = Transformer.from_crs(3857, 4326, always_xy=True)
        west, south = convert.transform(xmin, south_y)
        east, north = convert.transform(xmax, north_y)
    elif crs_epsg == 4326:
        if x >= 2**(zoom+1) or y >= 2**zoom:
            raise ValueError("tile coordinate exceeds matrix")
        span = 180 / 2**zoom
        west, east = -180+x*span, -180+(x+1)*span
        south, north = ((-90+y*span, -90+(y+1)*span) if scheme == "tms"
                        else (90-(y+1)*span, 90-y*span))
    else:
        raise ValueError("unsupported raster CRS")
    return f"POLYGON(({west:.10f} {south:.10f},{east:.10f} {south:.10f},{east:.10f} {north:.10f},{west:.10f} {north:.10f},{west:.10f} {south:.10f}))"


def index_raster_epoch(store, collection_id: str, acquisition_date: str,
                       *, verify_hashes: bool = True) -> dict:
    if not verify_hashes:
        raise ValueError("published file index requires SHA-256 verification")
    ensure_schema(store)
    with store.connect() as db:
        row = db.execute("""SELECT c.crs_epsg,c.tile_scheme,c.tile_format,e.storage_path,
                           e.tile_count,e.total_bytes,e.status
                           FROM raster_epoch e JOIN raster_collection c ON c.id=e.collection_id
                           WHERE e.collection_id=%s AND e.acquisition_date=%s""",
                         (collection_id, acquisition_date)).fetchone()
    if row is None or row["status"] not in {"staging","published"}:
        raise ValueError("raster epoch is not available for indexing")
    root = (store.data_dir / "rasters").resolve()
    base = (root / row["storage_path"]).resolve()
    if not base.is_relative_to(root) or not base.is_dir():
        raise ValueError("raster storage path is unavailable")
    version = acquisition_date
    kind = "raster_collection"
    extension = "." + str(row["tile_format"]).lstrip(".").lower()
    fingerprint = hashlib.sha256((str(collection_id)+":"+version+":"+str(row["tile_count"])+
                                  ":"+str(row["total_bytes"])).encode()).hexdigest()
    with store.connect() as db:
        db.execute("""INSERT INTO resource_index_version
            (source_kind,source_resource_id,source_version_id,status,storage_namespace,source_fingerprint)
            VALUES (%s,%s,%s,'building',%s,%s)
            ON CONFLICT (source_kind,source_resource_id,source_version_id)
            DO UPDATE SET status='building',storage_namespace=EXCLUDED.storage_namespace,
                source_fingerprint=EXCLUDED.source_fingerprint,updated_at=now()""",
            (kind,str(collection_id),version,f"rasters/{row['storage_path']}",fingerprint))
        db.execute("DELETE FROM resource_file WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s",
                   (kind,str(collection_id),version))
    sql = """INSERT INTO resource_file
        (source_kind,source_resource_id,source_version_id,relative_path,storage_namespace,
         size_bytes,sha256,media_type,file_role,geom_wgs84,extent_status,
         raster_zoom,raster_x,raster_y,acquisition_date)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'raster_tile',ST_GeomFromText(%s,4326),
                'indexed',%s,%s,%s,%s)"""
    pending = []
    total = count = 0
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix.lower() != extension:
            continue
        relative = path.relative_to(base).as_posix()
        pieces = relative.split("/")
        if len(pieces) != 3 or not all(part.isdigit() for part in pieces[:2]) or not pieces[2][:-len(extension)].isdigit():
            raise ValueError(f"unexpected raster tile path: {relative}")
        z, x, y = int(pieces[0]), int(pieces[1]), int(pieces[2][:-len(extension)])
        geom = _raster_wkt(row["crs_epsg"], row["tile_scheme"], z, x, y)
        size = path.stat().st_size
        digest = sha256_file(path) if verify_hashes else ""
        pending.append((kind,str(collection_id),version,relative,
                        f"rasters/{row['storage_path']}",size,digest,
                        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        geom,z,x,y,version))
        count += 1
        total += size
        if len(pending) >= 2000:
            with store.connect() as db:
                db.cursor().executemany(sql,pending)
            pending.clear()
    if pending:
        with store.connect() as db:
            db.cursor().executemany(sql,pending)
    if count != row["tile_count"] or total != row["total_bytes"]:
        raise ValueError(f"raster inventory differs: indexed {count}/{total}, expected {row['tile_count']}/{row['total_bytes']}")
    with store.connect() as db:
        db.execute("""UPDATE resource_index_version SET status='ready',file_count=%s,
                     node_count=0,detail='',updated_at=now() WHERE source_kind=%s
                     AND source_resource_id=%s AND source_version_id=%s""",
                   (count,kind,str(collection_id),version))
    return {"source_kind":kind,"source_resource_id":str(collection_id),
            "source_version_id":version,"file_count":count,"total_bytes":total,"status":"ready"}


def index_native_assets(store, source_kind: str, source_id: str) -> dict:
    """Catalog ODM originals or a published video group's original files."""
    if source_kind not in {"oblique_resource","video_group"}:
        raise ValueError("unsupported native asset catalog")
    ensure_schema(store)
    with store.connect() as db:
        if source_kind == "oblique_resource":
            rows = db.execute("""SELECT storage_key AS relative_path,storage_backend,size_bytes,
                sha256,'image/jpeg' AS media_type,'oblique_image' AS file_role,
                ST_AsText(candidate_footprint) AS geom_wkt,coverage_status AS extent_status
                FROM oblique_image WHERE resource_id=%s AND file_status='present'""",
                (source_id,)).fetchall()
            base_namespace = "oblique-images"
        else:
            group = db.execute("""SELECT ST_AsText(spatial_extent) AS geom_wkt,status,
                asset_count,total_bytes FROM video_group WHERE id=%s""",(source_id,)).fetchone()
            if group is None or group["status"] != "published":
                raise ValueError("video group is not published")
            rows = db.execute("""SELECT relative_path,'data-volume' AS storage_backend,
                size_bytes,sha256,media_type,role AS file_role FROM video_group_asset
                WHERE group_id=%s AND status='complete'""",(source_id,)).fetchall()
            base_namespace = f"videos/groups/{source_id}"
    indexed = []
    total = 0
    for item in rows:
        row = dict(item)
        namespace = base_namespace
        if source_kind == "oblique_resource" and row["storage_backend"] == "legacy-mirror":
            namespace = "legacy-mirror/objects"
        elif row["storage_backend"] != "data-volume":
            raise ValueError("unknown asset storage backend")
        relative = safe_path(row["relative_path"])
        path = file_path(store.data_dir / namespace, relative)
        if not path.is_file() or path.stat().st_size != row["size_bytes"]:
            raise ValueError(f"asset_unavailable: {relative}")
        digest = sha256_file(path)
        if digest.lower() != row["sha256"].lower():
            raise ValueError(f"asset checksum mismatch: {relative}")
        total += row["size_bytes"]
        indexed.append((source_kind,str(source_id),str(source_id),relative,namespace,
                        row["size_bytes"],row["sha256"],row["media_type"],row["file_role"],
                        row.get("geom_wkt") if source_kind == "oblique_resource" else group["geom_wkt"],
                        row.get("extent_status") if source_kind == "oblique_resource" else "indexed"))
    if source_kind == "video_group" and (len(indexed) != group["asset_count"] or total != group["total_bytes"]):
        raise ValueError("video group asset count or bytes differ from publication")
    fingerprint=hashlib.sha256((str(source_id)+":"+str(len(indexed))+":"+str(total)).encode()).hexdigest()
    with store.connect() as db:
        db.execute("""INSERT INTO resource_index_version
            (source_kind,source_resource_id,source_version_id,status,storage_namespace,
             file_count,node_count,source_fingerprint,detail)
            VALUES (%s,%s,%s,'ready',%s,%s,0,%s,'')
            ON CONFLICT (source_kind,source_resource_id,source_version_id)
            DO UPDATE SET status='ready',storage_namespace=EXCLUDED.storage_namespace,
                file_count=EXCLUDED.file_count,source_fingerprint=EXCLUDED.source_fingerprint,
                detail='',updated_at=now()""",
            (source_kind,str(source_id),str(source_id),base_namespace,len(indexed),fingerprint))
        db.execute("DELETE FROM resource_file WHERE source_kind=%s AND source_resource_id=%s AND source_version_id=%s",
                   (source_kind,str(source_id),str(source_id)))
        db.cursor().executemany("""INSERT INTO resource_file
            (source_kind,source_resource_id,source_version_id,relative_path,storage_namespace,
             size_bytes,sha256,media_type,file_role,geom_wgs84,extent_status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    CASE WHEN %s::text IS NULL THEN NULL ELSE ST_GeomFromText(%s::text,4326) END,%s)""",
            [(*row[:9],row[9],row[9],row[10]) for row in indexed])
    return {"source_kind":source_kind,"source_resource_id":str(source_id),
            "source_version_id":str(source_id),"file_count":len(indexed),
            "total_bytes":total,"status":"ready"}
