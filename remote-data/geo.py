#!/usr/bin/env python3
"""纯标准库的地理计算：横轴墨卡托(高斯-克吕格)反算、ENU↔大地坐标、WKT 多边形、点在多边形内。

用于把资源范围(WKT/各种 EPSG)与空三位姿统一到 WGS84 经纬度，并计算影像地面覆盖范围。
"""
import math
import re

# ---- 椭球 ----
WGS84 = (6378137.0, 1 / 298.257223563)
CGCS2000 = (6378137.0, 1 / 298.257222101)


def _ppi(f):
    """第二偏心率平方 e'^2"""
    e2 = f * (2 - f)
    return e2 / (1 - e2)


def geodetic_to_ecef(lat, lon, h=0.0, ell=WGS84):
    a, f = ell
    e2 = f * (2 - f)
    p, l = math.radians(lat), math.radians(lon)
    n = a / math.sqrt(1 - e2 * math.sin(p) ** 2)
    return ((n + h) * math.cos(p) * math.cos(l), (n + h) * math.cos(p) * math.sin(l),
            (n * (1 - e2) + h) * math.sin(p))


def ecef_to_geodetic(x, y, z, ell=WGS84):
    a, f = ell
    e2 = f * (2 - f)
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(10):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n
    return math.degrees(lat), math.degrees(lon), h


def enu_to_ecef(x, y, z, lat0, lon0, h0=0.0, ell=WGS84):
    """站心 ENU(x=东, y=北, z=天) → ECEF。"""
    p0, l0 = math.radians(lat0), math.radians(lon0)
    x0, y0, z0 = geodetic_to_ecef(lat0, lon0, h0, ell)
    sp, cp, sl, cl = math.sin(p0), math.cos(p0), math.sin(l0), math.cos(l0)
    return (x0 - sp * cl * x - sl * y + cp * cl * z,
            y0 - sp * sl * x + cl * y + cp * sl * z,
            z0 + cp * x + sp * z)


def ecef_to_enu(x, y, z, lat0, lon0, h0=0.0, ell=WGS84):
    x0, y0, z0 = geodetic_to_ecef(lat0, lon0, h0, ell)
    dx, dy, dz = x - x0, y - y0, z - z0
    p0, l0 = math.radians(lat0), math.radians(lon0)
    sp, cp, sl, cl = math.sin(p0), math.cos(p0), math.sin(l0), math.cos(l0)
    return (-sp * cl * dx - sp * sl * dy + cp * dz,
            -sl * dx + cl * dy,
            cp * cl * dx + cp * sl * dy + sp * dz)


def enu_to_geodetic(x, y, z, lat0, lon0, h0=0.0, ell=WGS84):
    return ecef_to_geodetic(*enu_to_ecef(x, y, z, lat0, lon0, h0, ell), ell)


def geodetic_to_enu(lat, lon, h, lat0, lon0, h0=0.0, ell=WGS84):
    return ecef_to_enu(*geodetic_to_ecef(lat, lon, h, ell), lat0, lon0, h0, ell)


# ---- 横轴墨卡托（高斯-克吕格）反算 ----
def tm_inverse(easting, northing, lon0_deg, k0=1.0, fe=500000.0, fn=0.0, ell=WGS84):
    """高斯-克吕格(横轴墨卡托)反算：平面 E/N → 经纬度(Snyder 级数)。"""
    a, f = ell
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x = easting - fe
    y = northing - fn
    m = y / k0
    mu = m / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    sinp, cosp, tanp = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = ep2 * cosp ** 2
    t1 = tanp ** 2
    n1 = a / math.sqrt(1 - e2 * sinp ** 2)
    r1 = a * (1 - e2) / (1 - e2 * sinp ** 2) ** 1.5
    d = x / (n1 * k0)
    lat = phi1 - (n1 * tanp / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / cosp
    return math.degrees(lat), math.degrees(lon) + lon0_deg


def tm_forward(lat_deg, lon_deg, lon0_deg, k0=1.0, fe=500000.0, fn=0.0, ell=WGS84):
    a, f = ell
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    p = math.radians(lat_deg)
    dl = math.radians(lon_deg - lon0_deg)
    n = a / math.sqrt(1 - e2 * math.sin(p) ** 2)
    t = math.tan(p) ** 2
    c = ep2 * math.cos(p) ** 2
    aa = math.cos(p) * dl
    m = a * ((1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * p
             - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * p)
             + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * p)
             - (35 * e2 ** 3 / 3072) * math.sin(6 * p))
    x = k0 * n * (aa + (1 - t + c) * aa ** 3 / 6
                  + (5 - 18 * t + t ** 2 + 72 * c - 58 * ep2) * aa ** 5 / 120)
    y = k0 * (m + n * math.tan(p) * (aa ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * aa ** 4 / 24
                                     + (61 - 58 * t + t ** 2 + 600 * c - 330 * ep2) * aa ** 6 / 720))
    return x + fe, y + fn


def webmercator_inverse(x, y):
    R = 6378137.0
    lon = math.degrees(x / R)
    lat = math.degrees(math.atan(math.sinh(y / R)))
    return lat, lon


def webmercator_forward(lat, lon):
    R = 6378137.0
    return math.radians(lon) * R, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R


# ---- 坐标参考系抽象 ----
class CRS:
    """支持 4326 / 900913(3857) / CGCS2000 三度带高斯-克吕格(k0=1, fe=500000) / ENU。"""

    def __init__(self, epsg=None, ell=CGCS2000, lon0=None, enu_origin=None):
        self.epsg = epsg
        self.ell = ell
        self.lon0 = lon0
        self.enu_origin = enu_origin  # (lat, lon)

    def to_lonlat(self, x, y):
        if self.enu_origin is not None:
            lat, lon, _ = enu_to_geodetic(x, y, 0.0, self.enu_origin[0], self.enu_origin[1], ell=self.ell)
            return lon, lat
        if self.epsg in (4326, 4490, None):
            return x, y
        if self.epsg in (900913, 3857, 3785):
            lat, lon = webmercator_inverse(x, y)
            return lon, lat
        if self.lon0 is not None:
            lat, lon = tm_inverse(x, y, self.lon0, ell=self.ell)
            return lon, lat
        raise ValueError(f"不支持的 CRS: {self.epsg}")

    def from_lonlat(self, lon, lat):
        if self.enu_origin is not None:
            e, n, _ = geodetic_to_enu(lat, lon, 0.0, self.enu_origin[0], self.enu_origin[1], ell=self.ell)
            return e, n
        if self.epsg in (4326, 4490, None):
            return lon, lat
        if self.epsg in (900913, 3857, 3785):
            return webmercator_forward(lat, lon)
        if self.lon0 is not None:
            return tm_forward(lat, lon, self.lon0, ell=self.ell)
        raise ValueError(f"不支持的 CRS: {self.epsg}")


def crs_from_xml_srs(definition):
    """解析空三 XML 的 SRS 定义字符串。"""
    d = definition.strip()
    m = re.match(r"EPSG:(\d+)", d)
    if m:
        epsg = int(m.group(1))
        if epsg in (4326, 4490):
            return CRS(epsg=epsg)
        if epsg in (900913, 3857):
            return CRS(epsg=epsg)
        # CGCS2000 三度带高斯-克吕格: 4547=CM114E ... 每带 +1
        if 4534 <= epsg <= 4554:
            cm = 75 + (epsg - 4534) * 3
            return CRS(epsg=epsg, lon0=cm)
        if 4513 <= epsg <= 4533:  # CGCS2000 6度带
            cm = 75 + (epsg - 4513) * 6
            return CRS(epsg=epsg, lon0=cm)
        raise ValueError(f"未支持 EPSG:{epsg}")
    m = re.match(r"ENU:\s*([-\d.]+)\s*,\s*([-\d.]+)", d)
    if m:
        return CRS(epsg=None, enu_origin=(float(m.group(1)), float(m.group(2))))
    raise ValueError(f"未识别 SRS 定义: {d}")


# ---- WKT ----
def parse_wkt_points(wkt):
    """从 POLYGON / POINT 字符串里取出平面坐标点列表 [(x, y), ...]。"""
    if not wkt:
        return []
    body = wkt[wkt.find("("):]
    pts = []
    for pair in re.findall(r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)\s+(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)", body):
        pts.append((float(pair[0]), float(pair[1])))
    return pts


def polygon_area(poly):
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2


def point_in_polygon(x, y, poly):
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside


def bbox_of(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def bbox_intersects(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# ---- 空三 XML ----
def parse_camera_xml(path):
    """解析 ContextCapture BlocksExchange XML，返回相机参数与每张影像的位姿。"""
    s = open(path, encoding="utf-8-sig").read()
    srs = {}
    for m in re.finditer(r"<SRS>(.*?)</SRS>", s, re.S):
        blk = m.group(1)
        i = re.search(r"<Id>(.*?)</Id>", blk)
        d = re.search(r"<Definition>(.*?)</Definition>", blk)
        if i and d:
            srs[i.group(1).strip()] = d.group(1).strip()
    srsid = re.search(r"<SRSId>(.*?)</SRSId>", s)
    crs = crs_from_xml_srs(srs[srsid.group(1).strip()]) if srsid else CRS(epsg=4326)

    groups = []
    for gm in re.finditer(r"<Photogroup>(.*?)</Photogroup>", s, re.S):
        g = gm.group(1)
        def one(tag, blk=g):
            m = re.search(rf"<{tag}>(.*?)</{tag}>", blk, re.S)
            return m.group(1).strip() if m else None
        w = int(one("Width")); h = int(one("Height"))
        fl = float(one("FocalLength")); on1 = one("SensorSize")
        sensor = float(on1) if on1 else None
        pp = re.search(r"<PrincipalPoint>(.*?)</PrincipalPoint>", g, re.S).group(1)
        ppx = float(re.search(r"<x>(.*?)</x>", pp).group(1))
        ppy = float(re.search(r"<y>(.*?)</y>", pp).group(1))
        f_px = fl * w / sensor if sensor else None
        photos = []
        for pm in re.finditer(r"<Photo>(.*?)</Photo>", g, re.S):
            p = pm.group(1)
            rid = re.search(r"<Id>(.*?)</Id>", p)
            ip = re.search(r"<ImagePath>(.*?)</ImagePath>", p)
            rot = re.search(r"<Rotation>(.*?)</Rotation>", p, re.S)
            cen = re.search(r"<Center>(.*?)</Center>", p, re.S)
            if not (rid and rot and cen):
                continue
            vals = {k: float(v) for k, v in re.findall(r"<M_(\d\d)>([-\d.eE+]+)</M_", rot.group(1))}
            if len(vals) < 9:
                continue
            M = [[vals[f"{r}{c}"] for c in range(3)] for r in range(3)]
            cx = float(re.search(r"<x>(.*?)</x>", cen.group(1)).group(1))
            cy = float(re.search(r"<y>(.*?)</y>", cen.group(1)).group(1))
            cz = float(re.search(r"<z>(.*?)</z>", cen.group(1)).group(1))
            photos.append({"xml_id": int(rid.group(1)), "image_path": ip.group(1) if ip else None,
                           "R": M, "C": (cx, cy, cz)})
        groups.append({"width": w, "height": h, "focal_mm": fl, "sensor_mm": sensor, "f_px": f_px,
                       "ppx": ppx, "ppy": ppy, "photos": photos})
    return {"crs": crs, "srs_defs": srs, "srs_def": srs.get(srsid.group(1).strip()) if srsid else None,
            "groups": groups}


def photo_footprint(photo, grp, cam_crs, ground_z, aoi_crs, margin_px=0.0):
    """计算一张影像在 ground_z 平面上的地面覆盖四边形（返回 aoi_crs 平面坐标）。"""
    w, h, f = grp["width"], grp["height"], grp["f_px"]
    ppx, ppy = grp["ppx"], grp["ppy"]
    R, (cx, cy, cz) = photo["R"], photo["C"]
    corners = []
    for px, py in ((0 - margin_px, 0 - margin_px), (w + margin_px, 0 - margin_px),
                   (w + margin_px, h + margin_px), (0 - margin_px, h + margin_px)):
        dc = (px - ppx, py - ppy, f)  # 相机系: x 右, y 下, z 前
        dw = tuple(sum(R[i][j] * dc[j] for j in range(3)) for i in range(3))
        if abs(dw[2]) < 1e-9:
            return None
        t = (ground_z - cz) / dw[2]
        if t <= 0:
            return None
        gx, gy = cx + t * dw[0], cy + t * dw[1]
        lon, lat = cam_crs.to_lonlat(gx, gy)
        corners.append(aoi_crs.from_lonlat(lon, lat))
    return corners


def aoi_from_args(bbox=None, center=None, radius=None, polygon=None):
    """把用户输入统一成 WGS84 上的一个多边形与 bbox。"""
    if bbox:
        minlon, minlat, maxlon, maxlat = bbox
        poly = [(minlon, minlat), (maxlon, minlat), (maxlon, maxlat), (minlon, maxlat)]
        return poly, (minlon, minlat, maxlon, maxlat)
    if polygon:
        pts = parse_wkt_points(polygon)
        return pts, bbox_of(pts)
    if center and radius:
        lat0, lon0 = center
        m_per_deg_lat = 111132.0
        m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
        dlat = radius / m_per_deg_lat
        dlon = radius / m_per_deg_lon
        poly = [(lon0 - dlon, lat0 - dlat), (lon0 + dlon, lat0 - dlat),
                (lon0 + dlon, lat0 + dlat), (lon0 - dlon, lat0 + dlat)]
        return poly, (lon0 - dlon, lat0 - dlat, lon0 + dlon, lat0 + dlat)
    raise ValueError("需要 --bbox 或 --center+--radius 或 --aoi-wkt")
