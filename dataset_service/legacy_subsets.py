"""Virtual, spatially filtered views over legacy 3D Tiles resources.

The source tile payloads remain in the legacy mirror.  A subset stores only its
source identity, AOI and catalog row; tileset JSON is filtered as it is read.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import posixpath
import sqlite3
import threading
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from pyproj import Geod, Transformer


MESH_TYPE = 1000012
POINTCLOUD_TYPE = 13000006
SUPPORTED_TYPES = {MESH_TYPE, POINTCLOUD_TYPE}
IDENTITY = (1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0)
PRECISION_NOTICE = (
    "按 3D Tiles 瓦片包围体筛选；边界瓦片仍可能包含多边形范围外的点或模型，"
    "这不是几何裁切。"
)


class SubsetError(ValueError):
    """A request cannot be represented as a safe intersecting-tile subset."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _canonical_ring(raw) -> list[list[float]]:
    if not isinstance(raw, list) or len(raw) < 4:
        raise SubsetError("每个多边形环至少需要4个坐标")
    points = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            raise SubsetError("坐标必须是 [longitude, latitude] 数组")
        try:
            lon, lat = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            raise SubsetError("坐标必须为数字") from None
        if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise SubsetError("坐标超出 EPSG:4326 范围")
        point = [round(lon, 9), round(lat, 9)]
        if not points or point != points[-1]:
            points.append(point)
    # Polygon sources may repeat the closing vertex more than once. Strip
    # consecutive duplicates before checking ring topology, then close it once.
    while len(points) > 1 and points[-1] == points[0]:
        points.pop()
    if len(points) < 3 or len(set(map(tuple, points))) < 3:
        raise SubsetError("多边形环无效")
    points.append(points[0][:])
    open_ring = points[:-1]
    rotations = []
    for candidate in (open_ring, list(reversed(open_ring))):
        smallest = min(range(len(candidate)), key=lambda i: tuple(candidate[i]))
        rotated = candidate[smallest:] + candidate[:smallest]
        rotations.append(rotated)
    normalized = min(rotations, key=lambda ring: tuple(tuple(point) for point in ring))
    return normalized + [normalized[0][:]]


SPATIAL_DATASET_QUERY_MAX_AREA_KM2 = 2000.0


def canonical_geometry(value: dict, crs: str, *, maximum_area_km2: float | None = None,
                       unlimited_area: bool = False) -> dict:
    if crs.upper().replace(" ", "") not in {"EPSG:4326", "OGC:CRS84"}:
        raise SubsetError("当前只接受 EPSG:4326 / WGS84 坐标")
    if not isinstance(value, dict):
        raise SubsetError("geometry 必须是 GeoJSON 对象")
    kind = value.get("type")
    raw = value.get("coordinates")
    if kind == "Polygon":
        polygons = [raw]
    elif kind == "MultiPolygon":
        polygons = raw
    else:
        raise SubsetError("geometry 仅支持 Polygon 或 MultiPolygon")
    if not isinstance(polygons, list) or not polygons or len(polygons) > 100:
        raise SubsetError("多边形数量无效")
    total_vertices = 0
    normalized = []
    all_lons = []
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise SubsetError("多边形缺少外环")
        rings = [_canonical_ring(ring) for ring in polygon]
        if any(_ring_self_intersects(ring) for ring in rings):
            raise SubsetError("多边形环存在自相交")
        if any(not _point_in_ring(hole[0], rings[0]) for hole in rings[1:]):
            raise SubsetError("多边形内环必须位于外环内")
        for hole_index, hole in enumerate(rings[1:]):
            if _rings_intersect(rings[0], hole) or any(
                _rings_intersect(hole, other) or _point_in_ring(hole[0], other) or _point_in_ring(other[0], hole)
                for other in rings[1:hole_index + 1]
            ):
                raise SubsetError("多边形内环不能相交或重叠")
        total_vertices += sum(len(ring) for ring in rings)
        all_lons.extend(point[0] for point in rings[0])
        holes = sorted(rings[1:], key=lambda ring: _json_bytes(ring))
        normalized.append([rings[0], *holes])
    if total_vertices > 10000:
        raise SubsetError("坐标点过多，单次最多10000个")
    if not unlimited_area and max(all_lons) - min(all_lons) > 180:
        raise SubsetError("当前区域子集接口不支持跨越日期变更线的多边形")
    normalized.sort(key=lambda polygon: _json_bytes(polygon))
    if kind == "Polygon" and len(normalized) == 1:
        geometry = {"type": "Polygon", "coordinates": normalized[0]}
    else:
        geometry = {"type": "MultiPolygon", "coordinates": normalized}
    polygons_to_measure = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    if unlimited_area:
        # A full-world WGS84 rectangle has zero geodesic polygon area; validate
        # nonzero planar geometry without imposing a geographic size ceiling.
        def planar_area(ring):
            x0, y0 = ring[0]
            return abs(sum((ring[i][0] - x0) * (ring[i + 1][1] - y0)
                           - (ring[i + 1][0] - x0) * (ring[i][1] - y0)
                           for i in range(len(ring) - 1))) / 2

        for polygon in polygons_to_measure:
            if planar_area(polygon[0]) - sum(planar_area(hole) for hole in polygon[1:]) <= 0:
                raise SubsetError("多边形面积无效")
        return geometry
    if maximum_area_km2 is None:
        try:
            maximum_area = float(os.getenv("DEQING_RESOURCE_SUBSET_MAX_AREA_KM2", "1000"))
        except ValueError:
            maximum_area = 1000.0
        if maximum_area <= 0:
            maximum_area = 1000.0
    else:
        maximum_area = maximum_area_km2
    geod = Geod(ellps="WGS84")
    area_m2 = 0.0
    for polygon in polygons_to_measure:
        polygon_area = 0.0
        for index, ring in enumerate(polygon):
            area, _ = geod.polygon_area_perimeter([point[0] for point in ring], [point[1] for point in ring])
            polygon_area += abs(area) * (1 if index == 0 else -1)
        if polygon_area <= 0:
            raise SubsetError("多边形面积无效")
        area_m2 += polygon_area
    if area_m2 > maximum_area * 1_000_000:
        raise SubsetError(f"查询范围过大，当前上限为 {maximum_area:g} 平方千米")
    return geometry


def _segments_intersect(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    def on_segment(p, q, r):
        return (min(p[0], r[0]) - 1e-8 <= q[0] <= max(p[0], r[0]) + 1e-8
                and min(p[1], r[1]) - 1e-8 <= q[1] <= max(p[1], r[1]) + 1e-8)

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if ((o1 > 1e-8 and o2 < -1e-8) or (o1 < -1e-8 and o2 > 1e-8)) and \
       ((o3 > 1e-8 and o4 < -1e-8) or (o3 < -1e-8 and o4 > 1e-8)):
        return True
    return ((abs(o1) <= 1e-8 and on_segment(a, c, b))
            or (abs(o2) <= 1e-8 and on_segment(a, d, b))
            or (abs(o3) <= 1e-8 and on_segment(c, a, d))
            or (abs(o4) <= 1e-8 and on_segment(c, b, d)))


def _rings_intersect(left, right) -> bool:
    return any(_segments_intersect(left[i], left[i + 1], right[j], right[j + 1])
               for i in range(len(left) - 1) for j in range(len(right) - 1))


def _ring_self_intersects(ring) -> bool:
    edge_count = len(ring) - 1
    for first in range(edge_count):
        for second in range(first + 1, edge_count):
            if second == first + 1 or (first == 0 and second == edge_count - 1):
                continue
            if _segments_intersect(ring[first], ring[first + 1], ring[second], ring[second + 1]):
                return True
    return False


def _point_in_ring(point, ring) -> bool:
    inside = False
    x, y = point
    for index in range(len(ring) - 1):
        a, b = ring[index], ring[index + 1]
        if abs((b[0] - a[0]) * (y - a[1]) - (b[1] - a[1]) * (x - a[0])) <= 1e-8 and \
           min(a[0], b[0]) - 1e-8 <= x <= max(a[0], b[0]) + 1e-8 and \
           min(a[1], b[1]) - 1e-8 <= y <= max(a[1], b[1]) + 1e-8:
            return True
        if (a[1] > y) != (b[1] > y):
            crossing_x = (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]
            if x < crossing_x:
                inside = not inside
    return inside


def _point_in_polygon(point, polygon) -> bool:
    return _point_in_ring(point, polygon[0]) and not any(_point_in_ring(point, hole) for hole in polygon[1:])


def _convex_hull(points):
    points = sorted(set((float(x), float(y)) for x, y in points))
    if len(points) <= 2:
        return points

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower = []
    for point in points:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(points):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    hull = lower[:-1] + upper[:-1]
    return hull + [hull[0]] if len(hull) > 2 else hull


class _AreaOfInterest:
    def __init__(self, geometry: dict):
        self.geometry = geometry
        polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
        outer = [point for polygon in polygons for point in polygon[0][:-1]]
        center_lon = math.degrees(math.atan2(
            sum(math.sin(math.radians(point[0])) for point in outer),
            sum(math.cos(math.radians(point[0])) for point in outer),
        ))
        center_lat = sum(point[1] for point in outer) / len(outer)
        local_crs = f"+proj=aeqd +lat_0={center_lat} +lon_0={center_lon} +datum=WGS84 +units=m +no_defs"
        self.to_local = Transformer.from_crs("EPSG:4326", local_crs, always_xy=True)
        self.from_ecef = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True)
        self.polygons = [
            [[self.to_local.transform(lon, lat) for lon, lat in ring] for ring in polygon]
            for polygon in polygons
        ]

    def intersects_projected(self, tile_ring) -> bool:
        if len(tile_ring) < 4:
            return False
        for polygon in self.polygons:
            if any(_point_in_polygon(point, polygon) for point in tile_ring[:-1]):
                return True
            if any(_point_in_ring(point, tile_ring) for point in polygon[0][:-1]):
                return True
            for a_index in range(len(tile_ring) - 1):
                a, b = tile_ring[a_index], tile_ring[a_index + 1]
                for ring in polygon:
                    for edge_index in range(len(ring) - 1):
                        if _segments_intersect(a, b, ring[edge_index], ring[edge_index + 1]):
                            return True
        return False

    def bounding_volume_intersects(self, volume: dict, matrix) -> bool:
        if not isinstance(volume, dict):
            raise SubsetError("源 tileset 存在缺少 boundingVolume 的瓦片")
        if "region" in volume:
            values = volume["region"]
            if not isinstance(values, list) or len(values) != 6:
                raise SubsetError("源 tileset 的 region boundingVolume 格式无效")
            west, south, east, north, min_height, max_height = map(float, values)
            if west > east:
                east += 2 * math.pi
            longitudes = [math.degrees(west), math.degrees(east)]
            latitudes = [math.degrees(south), math.degrees(north)]
            geo_points = [(lon, lat) for lon in longitudes for lat in latitudes]
        elif "box" in volume:
            values = volume["box"]
            if not isinstance(values, list) or len(values) != 12:
                raise SubsetError("源 tileset 的 box boundingVolume 格式无效")
            center = values[:3]
            axes = [values[3:6], values[6:9], values[9:12]]
            corners = []
            for sx in (-1, 1):
                for sy in (-1, 1):
                    for sz in (-1, 1):
                        corners.append(tuple(center[i] + sx * axes[0][i] + sy * axes[1][i] + sz * axes[2][i]
                                             for i in range(3)))
            geo_points = [self.from_ecef.transform(*_transform_point(matrix, point))[:2] for point in corners]
        elif "sphere" in volume:
            values = volume["sphere"]
            if not isinstance(values, list) or len(values) != 4 or float(values[3]) < 0:
                raise SubsetError("源 tileset 的 sphere boundingVolume 格式无效")
            center, radius = values[:3], float(values[3])
            corners = [tuple(center[i] + sign[i] * radius for i in range(3))
                       for sign in ((-1, -1, -1), (-1, -1, 1), (-1, 1, -1), (-1, 1, 1),
                                    (1, -1, -1), (1, -1, 1), (1, 1, -1), (1, 1, 1))]
            geo_points = [self.from_ecef.transform(*_transform_point(matrix, point))[:2] for point in corners]
        else:
            raise SubsetError("源 tileset 使用了不支持的 boundingVolume 类型")
        projected = [self.to_local.transform(lon, lat) for lon, lat in geo_points]
        hull = _convex_hull(projected)
        if len(hull) == 2:
            tile_ring = [hull[0], hull[1], hull[1], hull[0]]
        else:
            tile_ring = hull
        return self.intersects_projected(tile_ring)


def _transform_point(matrix, point):
    x, y, z = point
    values = [matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12],
              matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13],
              matrix[2] * x + matrix[6] * y + matrix[10] * z + matrix[14],
              matrix[3] * x + matrix[7] * y + matrix[11] * z + matrix[15]]
    if abs(values[3]) < 1e-15:
        raise SubsetError("源 tileset transform 无效")
    return tuple(value / values[3] for value in values[:3])


def _multiply_matrix(left, right):
    return tuple(sum(left[k * 4 + row] * right[col * 4 + k] for k in range(4))
                 for col in range(4) for row in range(4))


def _normalize_relative_path(value: str) -> str:
    if not value or "\\" in value or "?" in value or "#" in value or value.startswith("/"):
        raise SubsetError("tileset 引用了无效的相对路径")
    # Some OSGB exporters emit .//Data/... references. They address the same
    # package-relative file after collapsing dot and empty segments.
    if any(part == ".." for part in value.split("/")):
        raise SubsetError("tileset 引用了包外路径")
    normalized = posixpath.normpath(value)
    if normalized == "." or normalized.startswith("../") or normalized.startswith("/"):
        raise SubsetError("tileset 引用了包外路径")
    return normalized


def _format_wkt(geometry):
    def ring_wkt(ring):
        return "(" + ",".join(f"{x:.9f} {y:.9f}" for x, y in ring) + ")"
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    pieces = ["(" + ",".join(ring_wkt(ring) for ring in polygon) + ")" for polygon in polygons]
    return f"MULTIPOLYGON({','.join(pieces)})" if geometry["type"] == "MultiPolygon" else f"POLYGON({','.join(ring_wkt(ring) for ring in polygons[0])})"


class LegacySubsetStore:
    def __init__(self, data_dir: Path, legacy_store, cache, pointcloud_bridge=None):
        self.data_dir = data_dir.resolve()
        self.path = self.data_dir / "legacy_resource_subsets.sqlite3"
        self.legacy_store = legacy_store
        self.cache = cache
        self.pointcloud_bridge = pointcloud_bridge
        self._json_cache = OrderedDict()
        self._cache_lock = threading.RLock()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS legacy_resource_subset (
                id TEXT PRIMARY KEY,
                source_resource_id TEXT NOT NULL,
                source_version TEXT NOT NULL,
                geometry_hash TEXT NOT NULL,
                geometry_json TEXT NOT NULL,
                mode TEXT NOT NULL,
                resource_json TEXT NOT NULL,
                selected_top_level_tile_count INTEGER NOT NULL,
                source_top_level_tile_count INTEGER NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(source_resource_id,source_version,geometry_hash,mode)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS legacy_subset_file_context (
                resource_id TEXT NOT NULL REFERENCES legacy_resource_subset(id) ON DELETE CASCADE,
                relative_path TEXT NOT NULL,
                transform_json TEXT NOT NULL,
                PRIMARY KEY(resource_id,relative_path)
            )""")
            db.execute("CREATE INDEX IF NOT EXISTS legacy_subset_source_idx ON legacy_resource_subset(source_resource_id)")
        self.path.chmod(0o600)

    @contextmanager
    def _db(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def rows(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT resource_json FROM legacy_resource_subset ORDER BY created_at,id").fetchall()
        return [json.loads(row["resource_json"]) for row in rows]

    def get(self, resource_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM legacy_resource_subset WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"], "source_resource_id": row["source_resource_id"],
            "source_version": row["source_version"], "geometry": json.loads(row["geometry_json"]),
            "mode": row["mode"], "resource": json.loads(row["resource_json"]),
            "selected_top_level_tile_count": row["selected_top_level_tile_count"],
            "source_top_level_tile_count": row["source_top_level_tile_count"],
            "created_by": row["created_by"], "created_at": row["created_at"],
        }

    def rename(self, resource_id: str, name: str) -> bool:
        with self._db() as db:
            row = db.execute("SELECT resource_json FROM legacy_resource_subset WHERE id=?", (resource_id,)).fetchone()
            if row is None:
                return False
            resource = json.loads(row["resource_json"])
            resource["Label"] = name
            resource["Name"] = name
            db.execute("UPDATE legacy_resource_subset SET resource_json=? WHERE id=?",
                       (json.dumps(resource, ensure_ascii=False, separators=(",", ":")), resource_id))
        return True

    def ids_for_source(self, source_resource_id: str) -> list[str]:
        with self._db() as db:
            rows = db.execute(
                "SELECT id FROM legacy_resource_subset WHERE source_resource_id=? ORDER BY id",
                (source_resource_id,),
            ).fetchall()
        return [str(row["id"]) for row in rows]

    def delete(self, resource_id: str) -> bool:
        with self._db() as db:
            cursor = db.execute("DELETE FROM legacy_resource_subset WHERE id=?", (resource_id,))
        with self._cache_lock:
            self._json_cache.clear()
        return cursor.rowcount > 0

    def delete_for_source(self, source_resource_id: str) -> list[str]:
        resource_ids = self.ids_for_source(source_resource_id)
        if not resource_ids:
            return []
        with self._db() as db:
            db.executemany("DELETE FROM legacy_resource_subset WHERE id=?", [(item,) for item in resource_ids])
        with self._cache_lock:
            self._json_cache.clear()
        return resource_ids

    def _source(self, source_resource_id: str) -> dict:
        row = next((item for item in self.legacy_store.resources if str(item.get("Id")) == source_resource_id), None)
        if row is None or row.get("IsDeleted") or not bool(row.get("IsUploadCompleted")):
            raise SubsetError("源资源不存在、已删除或尚未完成上传")
        try:
            kind = int(row.get("Type"))
        except (TypeError, ValueError):
            kind = -1
        if kind not in SUPPORTED_TYPES:
            raise SubsetError("当前只支持 Terrain3dtiles 与 PointCloud3Dtiles 目录资源")
        try:
            metadata = json.loads(row.get("MetaData") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict) and metadata.get("ResourceSubset"):
            raise SubsetError("暂不支持从另一个区域子资源继续派生")
        return row

    @staticmethod
    def _route_prefix(resource_type: int) -> str:
        return "/api/Mesh/GetMesh3DTilesFile/" if resource_type == MESH_TYPE else "/api/PointCload/GetPointCloud3DTilesFile/"

    def _source_root(self, source: dict) -> bytes:
        key = str(source.get("OriginalRootFileId") or "")
        path = self.legacy_store.path_of(key) if key else None
        if path is not None:
            try:
                return path.read_bytes()
            except OSError:
                pass
        if self.pointcloud_bridge is not None:
            bridged = self.pointcloud_bridge.file_response(
                str(source["Id"]), "tileset.json", int(source["Type"]),
            )
            if bridged is not None:
                return Path(bridged.path).read_bytes()
        prefix = self._route_prefix(int(source["Type"]))
        result = self.cache.get_cached("GET", f"{prefix}{source['Id']}/tileset.json") if self.cache else None
        if result is None or result.status != 200:
            raise SubsetError("源资源入口 tileset 未在 7110 本地镜像中找到")
        return result.file.read_bytes() if result.file else result.body

    def _source_bytes(self, subset: dict, relative_path: str) -> tuple[bytes | None, object | None]:
        source = subset["source_resource_id"]
        resource = next((item for item in self.legacy_store.resources if str(item.get("Id")) == source), None)
        if resource is None:
            raise SubsetError("派生资源的源资源已不存在")
        if relative_path == "tileset.json":
            return self._source_root(resource), None
        path = _normalize_relative_path(relative_path)
        if self.pointcloud_bridge is not None:
            bridged = self.pointcloud_bridge.file_response(
                source, path, int(resource["Type"]),
            )
            if bridged is not None:
                if path.lower().endswith(".json"):
                    return Path(bridged.path).read_bytes(), None
                return None, bridged
        quoted_path = quote(path, safe="/:@+-._~")
        target = f"{self._route_prefix(int(resource['Type']))}{source}/{quoted_path}"
        cached = self.cache.get_cached("GET", target) if self.cache else None
        if cached is None or cached.status != 200:
            return None, cached
        if cached.file:
            try:
                return cached.file.read_bytes(), cached
            except OSError:
                return None, cached
        return cached.body, cached

    def create(self, source_resource_id: str, geometry_value: dict, crs: str, mode: str, name: str, creator: str) -> dict:
        try:
            source_resource_id = str(uuid.UUID(source_resource_id))
        except (ValueError, TypeError, AttributeError):
            raise SubsetError("source_resource_id 必须是资源目录 UUID") from None
        if mode != "intersecting_tiles":
            raise SubsetError("当前仅支持 mode=intersecting_tiles；严格裁切尚未开放")
        name = name.strip()
        if not 1 <= len(name) <= 100:
            raise SubsetError("name 长度应为1至100个字符")
        source = self._source(source_resource_id)
        geometry = canonical_geometry(geometry_value, crs)
        geometry_hash = hashlib.sha256(_json_bytes(geometry)).hexdigest()
        root_bytes = self._source_root(source)
        source_fingerprint = _json_bytes({
            "resource_id": source_resource_id,
            "upload_time": source.get("UploadTime"),
            "file_count": source.get("FileCount"),
            "file_size": source.get("FileSize"),
            "root_sha256": hashlib.sha256(root_bytes).hexdigest(),
        })
        source_version = hashlib.sha256(source_fingerprint).hexdigest()
        with self._db() as db:
            existing = db.execute("""SELECT * FROM legacy_resource_subset
                WHERE source_resource_id=? AND source_version=? AND geometry_hash=? AND mode=?""",
                (source_resource_id, source_version, geometry_hash, mode)).fetchone()
        if existing:
            return self._creation_result(self.get(existing["id"]), reused=True)
        try:
            document = json.loads(root_bytes)
        except (UnicodeError, json.JSONDecodeError):
            raise SubsetError("源资源 tileset.json 无法解析") from None
        projected = _AreaOfInterest(geometry)
        resource_type = int(source["Type"])
        source_top_count = len((document.get("root") or {}).get("children") or [])
        resource_id = str(uuid.uuid4())
        contexts = {"tileset.json": IDENTITY}
        filtered, matched = self._filter_document(document, projected, IDENTITY, "tileset.json", contexts)
        selected_top_count = len((filtered.get("root") or {}).get("children") or [])
        root_content = (filtered.get("root") or {}).get("content")
        if selected_top_count == 0 and root_content is None:
            raise SubsetError("该范围与源资源没有相交瓦片")
        resource = copy.deepcopy(source)
        resource["Id"] = resource_id
        resource["Label"] = name
        resource["Description"] = f"区域子集；来源 {source_resource_id}；{PRECISION_NOTICE}"
        resource["Range"] = _format_wkt(geometry)
        resource["FileCount"] = selected_top_count
        resource["FileSize"] = 0
        resource["num"] = max((int(row.get("num") or 0) for row in self.legacy_store.resources), default=0) + 1
        try:
            metadata = json.loads(resource.get("MetaData") or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata["ResourceSubset"] = {
            "source_resource_id": source_resource_id,
            "source_version": source_version,
            "mode": mode,
            "selected_top_level_tile_count": selected_top_count,
            "source_top_level_tile_count": source_top_count,
            "precision_notice": PRECISION_NOTICE,
        }
        resource["MetaData"] = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        created_at = _now()
        try:
            with self._db() as db:
                db.execute("""INSERT INTO legacy_resource_subset
                    (id,source_resource_id,source_version,geometry_hash,geometry_json,mode,resource_json,
                     selected_top_level_tile_count,source_top_level_tile_count,created_by,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (resource_id, source_resource_id, source_version, geometry_hash,
                     json.dumps(geometry, ensure_ascii=False, separators=(",", ":")), mode,
                     json.dumps(resource, ensure_ascii=False, separators=(",", ":")),
                     selected_top_count, source_top_count, creator, created_at))
                db.executemany("INSERT INTO legacy_subset_file_context(resource_id,relative_path,transform_json) VALUES(?,?,?)",
                               [(resource_id, path, json.dumps(list(matrix), separators=(",", ":")))
                                for path, matrix in contexts.items()])
        except sqlite3.IntegrityError:
            with self._db() as db:
                existing = db.execute("""SELECT id FROM legacy_resource_subset
                    WHERE source_resource_id=? AND source_version=? AND geometry_hash=? AND mode=?""",
                    (source_resource_id, source_version, geometry_hash, mode)).fetchone()
            if existing is None:
                raise
            return self._creation_result(self.get(existing["id"]), reused=True)
        return self._creation_result(self.get(resource_id), reused=False)

    def preview(self, source_resource_id: str, geometry_value: dict, crs: str = "EPSG:4326") -> dict:
        """Inspect root tile intersections without creating a subset or DB records."""
        try:
            source_resource_id = str(uuid.UUID(source_resource_id))
        except (ValueError, TypeError, AttributeError):
            raise SubsetError("source_resource_id 必须是资源目录 UUID") from None
        source = self._source(source_resource_id)
        geometry = canonical_geometry(geometry_value, crs)
        raw = self._source_root(source)
        try:
            document = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            raise SubsetError("源资源 tileset.json 无法解析") from None
        filtered, _ = self._filter_document(document, _AreaOfInterest(geometry), IDENTITY, "tileset.json", {})
        selected = len((filtered.get("root") or {}).get("children") or [])
        source_count = len((document.get("root") or {}).get("children") or [])
        root_content_hit = (filtered.get("root") or {}).get("content") is not None
        return {"source_resource_id": source_resource_id, "geometry": geometry,
                "intersects": selected > 0 or root_content_hit,
                "selected_top_level_tile_count": selected,
                "source_top_level_tile_count": source_count,
                "root_content_intersects": root_content_hit,
                "entrypoint_available": True, "referenced_files_checked": False,
                "precision_notice": PRECISION_NOTICE,
                "selection_basis": "root_tileset_bounding_volume"}

    @staticmethod
    def _creation_result(subset: dict, reused: bool) -> dict:
        resource = subset["resource"]
        resource_type = int(resource["Type"])
        return {
            "status": "completed", "subset_resource_id": subset["id"],
            "source_resource_id": subset["source_resource_id"],
            "resource_type": "Terrain3dtiles" if resource_type == MESH_TYPE else "PointCloud3Dtiles",
            "resource_type_name": resource.get("TypeName"), "type_id": resource_type,
            "mode": subset["mode"],
            "tileset_url": f"{LegacySubsetStore._route_prefix(resource_type)}{subset['id']}/tileset.json",
            "selected_top_level_tile_count": subset["selected_top_level_tile_count"],
            "source_top_level_tile_count": subset["source_top_level_tile_count"],
            "bounds": _geometry_bounds(subset["geometry"]),
            "precision_notice": PRECISION_NOTICE, "reused_existing": reused,
            "resource": resource,
        }

    def _context(self, resource_id: str, relative_path: str):
        with self._db() as db:
            row = db.execute("SELECT transform_json FROM legacy_subset_file_context WHERE resource_id=? AND relative_path=?",
                             (resource_id, relative_path)).fetchone()
        if row is None:
            return IDENTITY
        values = json.loads(row["transform_json"])
        return tuple(float(value) for value in values)

    def _register_contexts(self, resource_id: str, contexts: dict):
        if not contexts:
            return
        with self._db() as db:
            for path, matrix in contexts.items():
                prior = db.execute("SELECT transform_json FROM legacy_subset_file_context WHERE resource_id=? AND relative_path=?",
                                   (resource_id, path)).fetchone()
                value = json.dumps(list(matrix), separators=(",", ":"))
                if prior is None:
                    db.execute("INSERT INTO legacy_subset_file_context(resource_id,relative_path,transform_json) VALUES(?,?,?)",
                               (resource_id, path, value))
                else:
                    old = json.loads(prior["transform_json"])
                    if any(abs(float(a) - float(b)) > 1e-7 for a, b in zip(old, matrix)):
                        raise SubsetError("同一子瓦片路径由不同坐标变换引用，当前无法安全过滤")

    def _filter_document(self, document, aoi: _AreaOfInterest, inherited_matrix, document_path: str, contexts: dict):
        if not isinstance(document, dict) or not isinstance(document.get("root"), dict):
            raise SubsetError("源文件不是有效的 3D Tiles tileset")
        if "3DTILES_implicit_tiling" in document.get("extensions", {}):
            raise SubsetError("源资源使用隐式瓦片，当前筛选器尚不支持")
        filtered = copy.deepcopy(document)
        root, matched = self._filter_tile(filtered["root"], aoi, inherited_matrix, document_path, contexts)
        filtered["root"] = root
        return filtered, matched

    def _filter_tile(self, node, aoi: _AreaOfInterest, inherited_matrix, document_path: str, contexts: dict):
        if not isinstance(node, dict):
            raise SubsetError("源 tileset 的瓦片节点格式无效")
        if "implicitTiling" in node or "3DTILES_implicit_tiling" in node.get("extensions", {}):
            raise SubsetError("源资源使用隐式瓦片，当前筛选器尚不支持")
        local = node.get("transform")
        if local is not None and (not isinstance(local, list) or len(local) != 16):
            raise SubsetError("源 tileset transform 格式无效")
        matrix = _multiply_matrix(inherited_matrix, tuple(map(float, local))) if local is not None else inherited_matrix
        volume = node.get("boundingVolume")
        aoi.bounding_volume_intersects(volume, matrix)
        result = copy.deepcopy(node)
        content = node.get("content")
        content_hit = False
        if content is not None:
            content_volume = content.get("boundingVolume") if isinstance(content, dict) else None
            content_hit = aoi.bounding_volume_intersects(content_volume or volume, matrix)
            if content_hit:
                uri = content.get("uri", content.get("url")) if isinstance(content, dict) else content
                if isinstance(uri, str):
                    if uri.startswith(("http://", "https://", "//", "data:")):
                        raise SubsetError("源 tileset 含外部内容 URL，当前无法保证子集读取范围")
                    safe_uri = _normalize_relative_path(uri)
                    if isinstance(content, dict):
                        if "uri" in content:
                            result["content"]["uri"] = safe_uri
                        elif "url" in content:
                            result["content"]["url"] = safe_uri
                    if safe_uri.lower().endswith(".json"):
                        child_document_path = _normalize_relative_path(posixpath.join(posixpath.dirname(document_path), safe_uri))
                        previous = contexts.get(child_document_path)
                        if previous is not None and any(abs(float(a) - float(b)) > 1e-7 for a, b in zip(previous, matrix)):
                            raise SubsetError("同一子瓦片路径由不同坐标变换引用，当前无法安全过滤")
                        contexts[child_document_path] = matrix
        kept_children = []
        child_match = False
        for child in node.get("children", []) or []:
            filtered_child, child_hit = self._filter_tile(child, aoi, matrix, document_path, contexts)
            if child_hit:
                kept_children.append(filtered_child)
                child_match = True
        result["children"] = kept_children
        if not content_hit:
            result.pop("content", None)
        matched = content_hit or child_match
        if not matched:
            return result, False
        return result, True

    def file_response(self, resource_id: str, relative_path: str):
        subset = self.get(resource_id)
        if subset is None:
            return None
        try:
            relative_path = _normalize_relative_path(relative_path)
            with self._cache_lock:
                cached_json = self._json_cache.get((resource_id, relative_path))
                if cached_json is not None:
                    from fastapi.responses import Response
                    self._json_cache.move_to_end((resource_id, relative_path))
                    return Response(cached_json, media_type="application/json; charset=utf-8",
                                    headers={"Cache-Control": "private, max-age=60", "X-Legacy-Cache": "spatial-subset"})
            raw, cache_result = self._source_bytes(subset, relative_path)
            if raw is None:
                from fastapi.responses import Response
                if isinstance(cache_result, Response):
                    return cache_result
                return cache_result.response() if cache_result is not None else None
            if relative_path.lower().endswith(".json"):
                try:
                    document = json.loads(raw)
                except (UnicodeError, json.JSONDecodeError):
                    return cache_result.response() if cache_result is not None else None
                if isinstance(document, dict) and isinstance(document.get("root"), dict):
                    aoi = _AreaOfInterest(subset["geometry"])
                    contexts = {}
                    inherited = self._context(resource_id, relative_path)
                    filtered, _ = self._filter_document(document, aoi, inherited, relative_path, contexts)
                    self._register_contexts(resource_id, contexts)
                    with self._cache_lock:
                        cache_key = (resource_id, relative_path)
                        encoded = _json_bytes(filtered)
                        self._json_cache[cache_key] = encoded
                        self._json_cache.move_to_end(cache_key)
                        while len(self._json_cache) > 256:
                            self._json_cache.popitem(last=False)
                    from fastapi.responses import Response
                    return Response(encoded, media_type="application/json; charset=utf-8",
                                    headers={"Cache-Control": "private, max-age=60", "X-Legacy-Cache": "spatial-subset"})
            return cache_result.response() if cache_result is not None else None
        except SubsetError as error:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": str(error), "code": "unsupported_source"}, status_code=422)
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "区域子资源暂时不可用"}, status_code=503)


def _geometry_bounds(geometry):
    polygons = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    points = [point for polygon in polygons for ring in polygon for point in ring]
    return [min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points)]
