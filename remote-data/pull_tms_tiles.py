#!/usr/bin/env python3
"""Discover and cache nonempty TMS PNGs through the old front-end URL.

The XML bounding box defines candidate tile coordinates. Small levels are
probed completely; larger levels descend from nonempty parent tiles. Empty
HTTP 200 responses are recorded as absent and never published as PNGs.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import math
import os
import sqlite3
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def child(root, name):
    return next((item for item in root if item.tag.lower() == name.lower()), None)


def attributes(element):
    return {key.lower(): value for key, value in element.attrib.items()}


def levels(raw):
    root = ET.fromstring(raw)
    box = attributes(child(root, "BoundingBox"))
    origin = attributes(child(root, "Origin"))
    image = attributes(child(root, "TileFormat"))
    sets = child(root, "TileSets")
    width, height = int(image["width"]), int(image["height"])
    result = []
    for tile_set in sets:
        item = attributes(tile_set)
        scale = float(item["units-per-pixel"])
        dx, dy = width * scale, height * scale
        bounds = (
            math.floor((float(box["minx"]) - float(origin["x"])) / dx),
            math.ceil((float(box["maxx"]) - float(origin["x"])) / dx) - 1,
            math.floor((float(box["miny"]) - float(origin["y"])) / dy),
            math.ceil((float(box["maxy"]) - float(origin["y"])) / dy) - 1,
        )
        result.append((int(item["order"]), bounds))
    return result


def candidates(bounds, parents, file_count):
    x0, x1, y0, y1 = bounds
    if x1 < x0 or y1 < y0:
        return set()
    total = (x1 - x0 + 1) * (y1 - y0 + 1)
    if total <= max(200, 2 * file_count):
        return {(x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)}
    if parents:
        return {(x, y) for px, py in parents for x in (2 * px, 2 * px + 1)
                for y in (2 * py, 2 * py + 1) if x0 <= x <= x1 and y0 <= y <= y1}
    points = set()
    for xf in (0.05, 0.5, 0.95):
        for yf in (0.05, 0.5, 0.95):
            cx = round(x0 + (x1 - x0) * xf)
            cy = round(y0 + (y1 - y0) * yf)
            for x in (cx - 1, cx, cx + 1):
                for y in (cy - 1, cy, cy + 1):
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        points.add((x, y))
    return points


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--resource-id", action="append")
    parser.add_argument("--retry-empty", action="store_true")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--interface", default="wlp0s20f3")
    parser.add_argument("--address", default="175.27.226.195")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    objects = args.out / "objects"
    temporary = args.out / "temporary"
    objects.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    db = sqlite3.connect(args.out / "mirror.sqlite3")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS http_cache (
            cache_key TEXT PRIMARY KEY, method TEXT NOT NULL, target TEXT NOT NULL,
            request_json TEXT, status INTEGER NOT NULL, content_type TEXT NOT NULL,
            object_name TEXT NOT NULL, size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL, fetched_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS probe (
            resource_id TEXT NOT NULL, level INTEGER NOT NULL,
            x INTEGER NOT NULL, y INTEGER NOT NULL, state TEXT NOT NULL,
            PRIMARY KEY (resource_id, level, x, y)
        );
    """)
    upstream = sqlite3.connect(args.source / "mirror.sqlite3")
    resources = {item["Id"]: item for item in
                 json.loads(args.resources.read_text(encoding="utf-8"))["Rows"]}
    selected = set(args.resource_id or [])

    def save(target, data, content_type="application/octet-stream"):
        key = hashlib.sha256(b"GET\0" + target.encode() + b"\0").hexdigest()
        destination = objects / key
        destination.write_bytes(data)
        db.execute("INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?,?,?,?)", (
            key, "GET", target, None, 200, content_type, key, len(data),
            hashlib.sha256(data).hexdigest(), datetime.datetime.now(datetime.timezone.utc).isoformat(),
        ))

    def fetch(rid, level, x, y):
        target = f"/api/Raster/GetTmsFile/{rid}/{level}/{x}/{y}.png"
        handle, filename = tempfile.mkstemp(prefix="tms-", dir=temporary)
        os.close(handle)
        result = subprocess.run([
            "curl", "--interface", args.interface, "--resolve",
            "1jk412ww3207.vicp.fun:16761:" + args.address,
            "--connect-timeout", "15", "--max-time", "90", "--retry", "2",
            "--retry-all-errors", "-sS", "-o", filename,
            "-w", "%{http_code}|%{content_type}",
            "http://1jk412ww3207.vicp.fun:16761" + target,
        ], capture_output=True)
        try:
            status, content_type = result.stdout.decode().split("|", 1)
            status = int(status)
        except (UnicodeError, ValueError):
            status, content_type = 0, ""
        return level, x, y, target, status, content_type, result.returncode, Path(filename)

    for rid in sorted(resources):
        if selected and rid not in selected:
            continue
        root_target = f"/api/Raster/GetTmsFile/{rid}/tilemapresource.xml"
        row = upstream.execute("SELECT object_name FROM http_cache WHERE target=?", (root_target,)).fetchone()
        if row is None:
            continue
        raw = (args.source / "objects" / row[0]).read_bytes()
        save(root_target, raw)
        count_hint = int(resources[rid].get("FileCount") or 0)
        previous = set()
        empty_levels = 0
        for level, bounds in levels(raw):
            points = candidates(bounds, previous, count_hint)
            if not points:
                previous = set()
                empty_levels += 1
                if empty_levels >= 3:
                    break
                continue
            cached = {(x, y): state for x, y, state in db.execute(
                "SELECT x,y,state FROM probe WHERE resource_id=? AND level=?", (rid, level))}
            previous = {point for point in points if cached.get(point) == "present"}
            retry_states = {"error", "empty"} if args.retry_empty else {"error"}
            todo = sorted(point for point in points if point not in cached or cached[point] in retry_states)
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                replies = [pool.submit(fetch, rid, level, x, y) for x, y in todo]
                for future in concurrent.futures.as_completed(replies):
                    z, x, y, target, status, content_type, exit_code, filename = future.result()
                    data = filename.read_bytes()
                    filename.unlink(missing_ok=True)
                    if exit_code:
                        state = "error"
                    elif status == 200 and data:
                        state = "present"
                        save(target, data, content_type or "application/octet-stream")
                        previous.add((x, y))
                    else:
                        state = "empty"
                    db.execute("INSERT OR REPLACE INTO probe VALUES (?,?,?,?,?)", (rid, z, x, y, state))
            db.commit()
            print(rid, "level", level, "probed", len(points), "present", len(previous), flush=True)
            empty_levels = 0 if previous else empty_levels + 1
            if empty_levels >= 3:
                break
        print("resource", rid, "cached", db.execute(
            "SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM http_cache WHERE target LIKE ?",
            (f"/api/Raster/GetTmsFile/{rid}/%",)).fetchone(), flush=True)


if __name__ == "__main__":
    main()
