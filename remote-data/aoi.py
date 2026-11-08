#!/usr/bin/env python3
"""按地区（AoI）筛选可下载的倾斜影像：用空三位姿 + 相机内参计算每张影像的地面覆盖范围。

用法示例：
  python3 aoi.py --center 30.5296,114.3557 --radius 300 --name whu_center
  python3 aoi.py --bbox 114.352,30.527,114.359,30.532 --name whu_box
  python3 aoi.py --aoi-wkt "POLYGON ((114.352 30.527, 114.359 30.527, 114.359 30.532, 114.352 30.532))"

参数：
  --ground-z   地面高程(m，与资源自身高程基准一致)；省略则用 --agl 推算
  --agl        相对航高(m)，默认 120；仅用于缺少 --ground-z 时
  --buffer     覆盖范围外扩(m)，默认 15（保证边缘不遗漏）
  --groups     只保留指定相机组（如下视组 2），默认全部
  --max-horiz  斜视相机地面最远投影距离系数（×AGL），默认 8
"""
import argparse
import json
import math
import os
import statistics
import sqlite3
from contextlib import closing

import geo

T_INCLINE = 4000001


def xml_key(unc):
    v = (unc or "").replace("\\", "/")
    if v.startswith("//"):
        parts = v[2:].split("/")
        if len(parts) >= 5:
            v = "/".join(parts[2:])
    return v


def cam_z_stats(cam):
    zs = [p["C"][2] for g in cam["groups"] for p in g["photos"]]
    return min(zs), statistics.median(zs), max(zs)


def footprint_lonlat(photo, grp, cam_crs, ground_z, buffer_m, max_horiz_factor):
    """返回地面覆盖四边形的经纬度点列表；斜视相机射线越过地面平面时按 max_horiz(×航高) 截断。"""
    w, h, f = grp["width"], grp["height"], grp["f_px"]
    ppx, ppy = grp["ppx"], grp["ppy"]
    R, (cx, cy, cz) = photo["R"], photo["C"]
    agl_eff = max(cz - ground_z, 1.0)
    dmax = max_horiz_factor * agl_eff
    pts = []
    for px, py in ((0, 0), (w, 0), (w, h), (0, h)):
        dc = (px - ppx, py - ppy, f)  # 相机系: x 右, y 下, z 前
        dw = [sum(R[i][j] * dc[j] for j in range(3)) for i in range(3)]
        hz = math.hypot(dw[0], dw[1])
        t = None
        if dw[2] < -1e-6:
            t = (ground_z - cz) / dw[2]
            if t <= 0 or t * hz > dmax:
                t = None
        if t is None:  # 射向地平线以上或过远：按最大水平距离截断
            t = dmax / hz if hz > 1e-9 else 0.0
        lon, lat = cam_crs.to_lonlat(cx + t * dw[0], cy + t * dw[1])
        pts.append((lon, lat))

    if buffer_m:
        cxl = sum(p[0] for p in pts) / 4
        cyl = sum(p[1] for p in pts) / 4
        mlon = 111320.0 * math.cos(math.radians(cyl))
        mlat = 111132.0
        out = []
        for p in pts:
            dx, dy = (p[0] - cxl) * mlon, (p[1] - cyl) * mlat
            d = math.hypot(dx, dy)
            if d < 1e-6:
                out.append(p)
                continue
            k = (d + buffer_m) / d
            out.append((cxl + (p[0] - cxl) * k, cyl + (p[1] - cyl) * k))
        pts = out
    return pts


def seg_intersect(a, b, c, d):
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True
    return False


def polys_intersect(p1, p2):
    b1, b2 = geo.bbox_of(p1), geo.bbox_of(p2)
    if not geo.bbox_intersects(b1, b2):
        return False
    for p in p1:
        if geo.point_in_polygon(p[0], p[1], p2):
            return True
    for p in p2:
        if geo.point_in_polygon(p[0], p[1], p1):
            return True
    n1, n2 = len(p1), len(p2)
    for i in range(n1):
        for j in range(n2):
            if seg_intersect(p1[i], p1[(i + 1) % n1], p2[j], p2[(j + 1) % n2]):
                return True
    return False


def _merc(lat, lon):
    R = 6378137.0
    return math.radians(lon) * R, math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) * R


def fetch_basemap(minlon, minlat, maxlon, maxlat, zoom, cachedir):
    """用 Esri World Imagery 拼接卫星底图；失败返回 None。"""
    import urllib.request
    from PIL import Image
    n = 2 ** zoom
    x0, y0 = _merc(minlat, minlon)
    x1, y1 = _merc(maxlat, maxlon)
    ext = math.pi * 6378137.0
    tx0 = int((x0 + ext) / (2 * ext) * n)
    tx1 = int((x1 + ext) / (2 * ext) * n)
    ty0 = int((ext - y1) / (2 * ext) * n)
    ty1 = int((ext - y0) / (2 * ext) * n)
    w, h = (tx1 - tx0 + 1) * 256, (ty1 - ty0 + 1) * 256
    if w * h > 4000 * 4000:
        return None
    mosaic = Image.new("RGB", (w, h), (240, 240, 240))
    os.makedirs(cachedir, exist_ok=True)
    ok = 0
    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            fn = os.path.join(cachedir, f"z{zoom}_{tx}_{ty}.jpg")
            if not os.path.exists(fn):
                url = ("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
                       f"MapServer/tile/{zoom}/{ty}/{tx}")
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "aoi-preview/1.0"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        data = r.read()
                    with open(fn, "wb") as f:
                        f.write(data)
                except Exception:  # noqa: BLE001
                    continue
            try:
                tile = Image.open(fn).convert("RGB")
            except Exception:  # noqa: BLE001
                continue
            mosaic.paste(tile, ((tx - tx0) * 256, (ty - ty0) * 256))
            ok += 1
    if ok == 0:
        return None
    # 该 mosaic 对应的经纬度范围
    gx0 = tx0 / n * 2 * ext - ext
    gx1 = (tx1 + 1) / n * 2 * ext - ext
    gy0 = ext - (ty1 + 1) / n * 2 * ext
    gy1 = ext - ty0 / n * 2 * ext
    R = 6378137.0
    lon0, lon1 = math.degrees(gx0 / R), math.degrees(gx1 / R)
    lat0 = math.degrees(2 * math.atan(math.exp(gy0 / R)) - math.pi / 2)
    lat1 = math.degrees(2 * math.atan(math.exp(gy1 / R)) - math.pi / 2)
    return mosaic, (lon0, lat0, lon1, lat1)


def render_preview(outdir, name, aoi_poly, features, summary, total_sel, zoom=18):
    """把 AoI 与选中影像的地面覆盖范围叠加到卫星底图上，便于和你的截图对照。"""
    from PIL import Image, ImageDraw, ImageFont
    fps = []
    for f in features:
        if f["properties"].get("kind") == "aoi":
            continue
        fps.append(geo.parse_wkt_points(
            "POLYGON ((" + ", ".join(f"{x} {y}" for x, y in f["geometry"]["coordinates"][0]) + "))"))
    b = geo.bbox_of(aoi_poly)
    mx = (b[2] - b[0]) * 0.5
    my = (b[3] - b[1]) * 0.5
    q = (b[0] - mx, b[1] - my, b[2] + mx, b[3] + my)
    base = fetch_basemap(*q, zoom, os.path.join(outdir, "_tiles"))
    if base is None:
        wpx, hpx = 1400, 1000
        base = (Image.new("RGB", (wpx, hpx), (247, 249, 252)), q)
        note = "no basemap"
    else:
        note = "Imagery (c) Esri World Imagery"
    img, (lon0, lat0, lon1, lat1) = base
    img = img.copy()
    wpx, hpx = img.size
    overlay = Image.new("RGBA", (wpx, hpx), (0, 0, 0, 0))
    dr = ImageDraw.Draw(overlay)

    def to_px(lon, lat):
        return ((lon - lon0) / max(lon1 - lon0, 1e-12) * wpx,
                (lat1 - lat) / max(lat1 - lat0, 1e-12) * hpx)

    for fp in fps:
        dr.polygon([to_px(p[0], p[1]) for p in fp], outline=(0, 200, 255, 90), fill=(0, 200, 255, 14))
    dr.polygon([to_px(p[0], p[1]) for p in aoi_poly], outline=(255, 40, 40, 255))
    a = [to_px(p[0], p[1]) for p in aoi_poly]
    for i in range(len(a)):
        dr.line([a[i], a[(i + 1) % len(a)]], fill=(255, 40, 40, 255), width=5)
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 16)
    except Exception:  # noqa: BLE001
        font = small = ImageFont.load_default()
    box = [8, 8, wpx - 8, 92]
    dr.rectangle(box, fill=(255, 255, 255))
    dr.text((18, 14), f"AoI: {name}    selected photos: {total_sel}", fill=(15, 15, 15), font=font)
    dr.text((18, 44), f"bbox {b[0]:.5f},{b[1]:.5f} .. {b[2]:.5f},{b[3]:.5f}   "
                      f"{(b[2]-b[0])*111320*math.cos(math.radians((b[1]+b[3])/2)):.0f} m x "
                      f"{(b[3]-b[1])*111132:.0f} m", fill=(50, 50, 50), font=small)
    dr.text((18, 66), f"red = AoI    cyan = photo ground footprint (approx)    {note}",
            fill=(90, 90, 90), font=small)
    m200 = 200 / (111320 * math.cos(math.radians((b[1] + b[3]) / 2)))
    x1 = wpx - 30
    x0 = x1 - m200 / (lon1 - lon0) * wpx
    dr.rectangle([x0, hpx - 34, x1, hpx - 26], fill=(255, 255, 255))
    dr.rectangle([x0, hpx - 34, x1, hpx - 26], outline=(20, 20, 20))
    dr.text((x0, hpx - 58), "200 m", fill=(255, 255, 255), font=small)
    path = os.path.join(outdir, "preview.png")
    img.save(path)
    print(f"卫星底图预览: {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--name", help="AoI 名称（输出目录/数据库标记）")
    ap.add_argument("--bbox", help="minlon,minlat,maxlon,maxlat")
    ap.add_argument("--center", help="lat,lon")
    ap.add_argument("--radius", type=float, help="半径(米)，与 --center 配合")
    ap.add_argument("--aoi-wkt", help="WGS84 POLYGON WKT")
    ap.add_argument("--aoi-geojson", help="WGS84 GeoJSON 文件（Polygon 或 FeatureCollection 首要素）")
    ap.add_argument("--ground-z", type=float, default=None)
    ap.add_argument("--agl", type=float, default=120.0)
    ap.add_argument("--buffer", type=float, default=15.0)
    ap.add_argument("--groups", help="仅这些相机组，如 2 或 1,3,5")
    ap.add_argument("--max-horiz", type=float, default=8.0)
    ap.add_argument("--resource", help="只处理指定资源 Id")
    ap.add_argument("--no-preview", action="store_true")
    args = ap.parse_args()

    bbox = tuple(float(x) for x in args.bbox.split(",")) if args.bbox else None
    center = tuple(float(x) for x in args.center.split(",")) if args.center else None
    if args.aoi_geojson:
        gj = json.load(open(args.aoi_geojson, encoding="utf-8"))
        if gj.get("type") == "FeatureCollection":
            gj = gj["features"][0]
        if gj.get("type") == "Feature":
            gj = gj["geometry"]
        ring = gj["coordinates"][0] if gj["type"] == "Polygon" else gj["coordinates"][0][0]
        aoi_poly = [(float(x), float(y)) for x, y, *_ in ring]
        if aoi_poly and aoi_poly[0] == aoi_poly[-1]:
            aoi_poly = aoi_poly[:-1]
        aoi_bbox = geo.bbox_of(aoi_poly)
    else:
        aoi_poly, aoi_bbox = geo.aoi_from_args(bbox=bbox, center=center, radius=args.radius,
                                              polygon=args.aoi_wkt)
    name = args.name or f"aoi_{aoi_bbox[0]:.4f}_{aoi_bbox[1]:.4f}"
    outdir = os.path.join(args.out, "aoi", name)
    os.makedirs(outdir, exist_ok=True)
    groups_filter = None
    if args.groups:
        groups_filter = {int(x) for x in args.groups.split(",") if x.strip()}

    conn = sqlite3.connect(os.path.join(args.out, "catalog.db"))
    conn.execute("""CREATE TABLE IF NOT EXISTS photo_selection (
        aoi TEXT NOT NULL, resource_id TEXT NOT NULL, photo_id INTEGER NOT NULL,
        group_id INTEGER, footprint_wkt TEXT, agl REAL, PRIMARY KEY (aoi, resource_id, photo_id))""")
    conn.execute("DELETE FROM photo_selection WHERE aoi=?", (name,))

    resources = conn.execute("SELECT id, label, file_count, file_size FROM resource WHERE type=?",
                             (T_INCLINE,)).fetchall()
    if args.resource:
        resources = [r for r in resources if r[0] == args.resource]

    summary, features = [], []
    total_sel = 0
    for rid, label, file_count, file_size in resources:
        xml = os.path.join(args.out, "assets", "camera_xml", rid + ".xml")
        if not os.path.exists(xml):
            print(f"[跳过] {label} {rid} 缺少空三 XML")
            continue
        cam = geo.parse_camera_xml(xml)
        zmin, zmed, zmax = cam_z_stats(cam)
        zg = args.ground_z if args.ground_z is not None else zmed - args.agl
        key2pid = {k: pid for pid, k in conn.execute(
            "SELECT photo_id, fastdfs_id FROM photo WHERE resource_id=?", (rid,))}

        per_group = {}
        sel_rows, n_fp = [], 0
        for gi, grp in enumerate(cam["groups"], 1):
            if groups_filter and gi not in groups_filter:
                continue
            n_sel = 0
            for photo in grp["photos"]:
                fp = footprint_lonlat(photo, grp, cam["crs"], zg, args.buffer, args.max_horiz)
                if not polys_intersect(fp, aoi_poly):
                    continue
                key = xml_key(photo["image_path"])
                pid = key2pid.get(key)
                if pid is None:
                    continue
                n_sel += 1
                n_fp += 1
                wkt = "POLYGON ((" + ", ".join(f"{p[0]:.7f} {p[1]:.7f}" for p in fp + [fp[0]]) + "))"
                sel_rows.append((name, rid, pid, gi, wkt, round(photo["C"][2] - zg, 1)))
            per_group[gi] = n_sel

        if sel_rows:
            conn.executemany("""INSERT OR REPLACE INTO photo_selection
                (aoi, resource_id, photo_id, group_id, footprint_wkt, agl) VALUES (?,?,?,?,?,?)""",
                             sel_rows)
            conn.commit()
        total_sel += len(sel_rows)
        est = (file_size / file_count * len(sel_rows) / 2 ** 30) if file_count else 0
        summary.append({"resource_id": rid, "label": label, "crs": cam["srs_def"],
                        "camera_z": [round(zmin, 1), round(zmed, 1), round(zmax, 1)],
                        "ground_z": round(zg, 1), "agl": round(zmed - zg, 1),
                        "photos_total": sum(len(g["photos"]) for g in cam["groups"]),
                        "selected": len(sel_rows), "per_group": per_group,
                        "est_gib": round(est, 2)})
        print(f"{label:<10} {rid[:8]} CRS={cam['srs_def']:<45} 相机Z={zmed:.0f}m 地面Z={zg:.0f}m "
              f"航高≈{zmed-zg:.0f}m 照片={summary[-1]['photos_total']:<6} 选中={len(sel_rows):<6} "
              f"占比={len(sel_rows)/max(1,summary[-1]['photos_total']):.1%} 约 {est:.1f} GiB")

        if not args.no_preview and sel_rows:
            rows = conn.execute("""SELECT ps.photo_id, ps.group_id, ps.footprint_wkt FROM photo_selection ps
                WHERE ps.aoi=? AND ps.resource_id=?""", (name, rid)).fetchall()
            for pid, gid, wkt in rows:
                pts = geo.parse_wkt_points(wkt)
                features.append({"type": "Feature",
                                 "properties": {"resource": rid, "label": label, "photo_id": pid, "group": gid},
                                 "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in pts]]}})

    if not args.no_preview:
        try:
            render_preview(outdir, name, aoi_poly, features, summary, total_sel)
        except Exception as exc:  # noqa: BLE001
            print(f"[预览图生成失败] {exc}")

    features.insert(0, {"type": "Feature", "properties": {"kind": "aoi", "name": name},
                        "geometry": {"type": "Polygon",
                                     "coordinates": [[list(p) for p in aoi_poly + [aoi_poly[0]]]]}})
    with open(os.path.join(outdir, "footprints.geojson"), "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f)
    with open(os.path.join(outdir, "summary.json"), "w", encoding="utf-8") as f:
        json.dump({"aoi": name, "bbox": aoi_bbox, "polygon": aoi_poly,
                   "ground_z": args.ground_z, "agl": args.agl, "buffer": args.buffer,
                   "total_selected": total_sel, "resources": summary}, f, ensure_ascii=False, indent=1)

    print(f"\nAoI「{name}」共选中 {total_sel} 张影像，约 "
          f"{sum(s['est_gib'] for s in summary):.1f} GiB")
    print(f"输出目录: {outdir}/  (footprints.geojson / summary.json)")
    print(f"下载命令: python3 mirror.py images --aoi {name}")
    conn.close()


if __name__ == "__main__":
    main()
