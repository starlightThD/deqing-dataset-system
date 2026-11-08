#!/usr/bin/env python3
"""Mirror the legacy ARLayer photo index and bind its image IDs to cached FastDFS files.

The remote API does not publish its image-ID-to-FastDFS-key mapping. The camera
positions in its ARLayer index match the positions in the downloaded DB3 catalog,
so this script joins them without downloading the JPEGs again. Pages are committed
one at a time and a rerun resumes at the first missing page.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import urlsplit


def request(base: str, interface: str, address: str, path: str, token: str = "", body=None):
    parsed = urlsplit(base)
    command = [
        "curl", "--interface", interface, "--resolve",
        f"{parsed.hostname}:{parsed.port or 80}:{address}",
        "--connect-timeout", "15", "--max-time", "90", "--retry", "3",
        "--retry-all-errors", "--retry-delay", "2", "-sS",
        "-H", "Accept-Encoding: identity",
    ]
    if token:
        command.extend(["-H", f"Authorization: Bearer {token}"])
    if body is not None:
        command.extend([
            "-H", "Content-Type: application/json;charset=UTF-8", "--data-binary",
            json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        ])
    command.extend(["-w", "\n%{http_code}", base.rstrip("/") + path])
    result = subprocess.run(command, check=True, capture_output=True)
    raw, status = result.stdout.rsplit(b"\n", 1)
    if int(status) != 200:
        raise RuntimeError(f"{path}: HTTP {status.decode()}")
    return json.loads(raw.decode("utf-8-sig"))


def position(row):
    return tuple(round(float(row[name]), 9) for name in ("X", "Y", "Z"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", default="http://1jk412ww3207.vicp.fun:16761")
    parser.add_argument("--interface", default="wlp0s20f3")
    parser.add_argument("--address", default="175.27.226.195")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--page-size", type=int, default=500)
    args = parser.parse_args()
    if not 1 <= args.page_size <= 1000:
        parser.error("--page-size must be between 1 and 1000")
    password = os.environ.get("REMOTE_PASSWORD")
    if not password:
        parser.error("set REMOTE_PASSWORD in the environment")

    login = request(args.base, args.interface, args.address, "/api/Login/Login", body={
        "UserName": args.username,
        "Password": hashlib.md5(password.encode("utf-8")).hexdigest(),
    })
    token = login["Data"]["Token"]
    resources = json.loads(args.resources.read_text(encoding="utf-8"))["Rows"]
    resource_ids = [row["Id"] for row in resources if row.get("Type") == 4000001]
    catalog = sqlite3.connect(args.catalog)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mirror = sqlite3.connect(args.out)
    mirror.executescript("""
        CREATE TABLE IF NOT EXISTS arlayer_resource (
            resource_id TEXT PRIMARY KEY, remote_total INTEGER NOT NULL,
            complete INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS arlayer_photo (
            resource_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            image_id TEXT NOT NULL UNIQUE, fastdfs_id TEXT,
            record_json TEXT NOT NULL,
            PRIMARY KEY (resource_id, ordinal)
        );
        CREATE INDEX IF NOT EXISTS idx_arlayer_photo_image ON arlayer_photo(image_id);
    """)

    for resource_id in resource_ids:
        remote_total = request(args.base, args.interface, args.address,
                               f"/api/ARLayer/GetNumARNote/{resource_id}", token)
        if type(remote_total) is not int or remote_total < 0:
            raise RuntimeError(f"{resource_id}: invalid count {remote_total!r}")
        local_rows = catalog.execute(
            "SELECT x,y,z,fastdfs_id FROM photo WHERE resource_id=?", (resource_id,)
        ).fetchall()
        if len(local_rows) != remote_total:
            raise RuntimeError(f"{resource_id}: local DB3 count {len(local_rows)} != remote {remote_total}")
        positions = {}
        for x, y, z, key in local_rows:
            coordinate = tuple(round(float(v), 9) for v in (x, y, z))
            if coordinate in positions:
                raise RuntimeError(f"{resource_id}: repeated camera position {coordinate}")
            positions[coordinate] = key

        mirror.execute("INSERT INTO arlayer_resource(resource_id,remote_total,complete) VALUES (?,?,0) "
                       "ON CONFLICT(resource_id) DO UPDATE SET remote_total=excluded.remote_total,complete=0",
                       (resource_id, remote_total))
        first_missing = 0
        for (ordinal,) in mirror.execute(
            "SELECT ordinal FROM arlayer_photo WHERE resource_id=? ORDER BY ordinal", (resource_id,)
        ):
            if ordinal != first_missing:
                break
            first_missing += 1
        start = (first_missing // args.page_size) * args.page_size
        while start < remote_total:
            amount = min(args.page_size, remote_total - start)
            path = (f"/api/ARLayer/FetchARNote?arlayerUrl={resource_id}"
                    f"&startPos={start}&num={amount}")
            page = request(args.base, args.interface, args.address, path, token)
            if not isinstance(page, list) or len(page) != amount:
                raise RuntimeError(f"{resource_id}: expected {amount} photos at {start}")
            for offset, row in enumerate(page):
                image_id = row.get("nID") if isinstance(row, dict) else None
                if not isinstance(image_id, str) or not image_id:
                    raise RuntimeError(f"{resource_id}: invalid image ID at {start + offset}")
                key = positions.get(position(row))
                if not key:
                    raise RuntimeError(f"{resource_id}: no DB3 match for image {image_id}")
                mirror.execute(
                    "INSERT INTO arlayer_photo(resource_id,ordinal,image_id,fastdfs_id,record_json) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(resource_id,ordinal) DO UPDATE SET "
                    "image_id=excluded.image_id,fastdfs_id=excluded.fastdfs_id,record_json=excluded.record_json",
                    (resource_id, start + offset, image_id, key,
                     json.dumps(row, ensure_ascii=False, separators=(",", ":"))),
                )
            mirror.commit()
            start += amount
            print(f"{resource_id}: {start}/{remote_total}", flush=True)
        stored = mirror.execute("SELECT COUNT(*) FROM arlayer_photo WHERE resource_id=?",
                                (resource_id,)).fetchone()[0]
        if stored != remote_total:
            raise RuntimeError(f"{resource_id}: stored {stored} != remote {remote_total}")
        mirror.execute("UPDATE arlayer_resource SET complete=1 WHERE resource_id=?", (resource_id,))
        mirror.commit()
    print("complete", mirror.execute("SELECT COUNT(*) FROM arlayer_photo").fetchone()[0],
          "photos across", len(resource_ids), "resources")


if __name__ == "__main__":
    main()
